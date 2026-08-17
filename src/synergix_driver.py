"""Synergix ERP driver: duplicate check + create/fulfil. DRY_RUN aware.

In DRY_RUN (the default) every step runs EXCEPT the final submit/confirm clicks, which are logged
instead of executed. Live submission only happens when DRY_RUN=false is set in .env.

Stages from the workflow doc:
  B — Create quotation: new quotation -> "Copy From" template -> fill ~8 fields -> submit
  C — Schedule board update: MOST FRAGILE. Best-effort; failure marks PARTIAL, not FAILED.
  D — Attach PDF + fulfil service order.

After any WO (success or fail), navigate back to a known home state before the next.
Browser access is serialised by the caller via an asyncio.Lock (writes run one at a time).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from playwright.async_api import Page, async_playwright

from config import selectors as S
from config import settings
from src.models import LineItem, WOPayload, WOStatus, is_jbtc
from src.validator import build_remarks, resolve_project_code

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    status: WOStatus            # PROCESSED, PARTIAL, or FAILED
    detail: str = ""


class DedupResult(str, Enum):
    """Three-state duplicate check. UNCERTAIN is fail-safe: never auto-bill, flag for a human.

    Needed because the JBTC "Un-Invoiced WO" list is maintained by hand, so a WO can still appear
    there after it has actually been invoiced in Synergix. Synergix — not JBTC — is the source of
    truth for whether a WO is already billed. A boolean can't distinguish "confirmed not billed" from
    "couldn't tell" (search error/timeout/ambiguous result); conflating them risks double-billing.
    """

    NOT_DUPLICATE = "not_duplicate"   # confirmed: no existing invoice found in Synergix
    DUPLICATE = "duplicate"           # confirmed: an existing invoice matches -> do NOT bill again
    UNCERTAIN = "uncertain"           # search errored/ambiguous -> human must verify before billing

    @property
    def safe_to_bill(self) -> bool:
        return self is DedupResult.NOT_DUPLICATE


# Item code for adhoc pest control quotation lines. Confirmed IDENTICAL for both councils: seen live
# in a real JBTC Synergix session (screen-recording, 2026-08-03 review) as "SE-400212A / Adhoc-
# Provision of Pest Control Services", and matches the SKTC workflow doc's documented item code
# exactly. Type is always "S" (service).
ITEM_CODE = "SE-400212A"
ITEM_TYPE = "S"

# Payment Method dropdown target. Confirmed live (2026-08-15) that the previously-targeted
# "Cheque" does not appear as an option at all for JALAN BESAR TOWN COUNCIL — the only real
# (non-placeholder) option offered was "GIRO", which the client confirmed is correct.
PAYMENT_METHOD = "GIRO"

# External Remarks picker target. Confirmed live (2026-08-17) that the "External Remarks" field's
# magnifying-glass search panel offers a fixed catalog of boilerplate remark codes (OCBC BANK
# DETAIL, several T&C variants); the client asked for OCBC BANK DETAIL specifically, matching the
# GIRO payment method.
EXTERNAL_REMARK_CODE = "OCBC BANK DETAIL"

# Project Site search term per council, used to find the right autocomplete row when creating a
# quotation from scratch (see _select_autocomplete_row). Searching by the bare numeric code is NOT
# safe — confirmed live that the same code string can match a DIFFERENT council's project (e.g.
# "2000050" matched a Jalan Besar project first, not Sengkang), so we search by council name instead
# and let the resolve_project_code()-derived code disambiguate which of the matches to pick.
_PROJECT_SITE_SEARCH_JBTC = "Jalan Besar"
_PROJECT_SITE_SEARCH_SKTC = "Sengkang"

# TODO(human): Sengkang's real Project Site options (confirmed live, 2026-08-03) are
# "2000073-Sengkang Town Council (Pest control)" and "2000130-Sengkang Town Council (Mosquito)" —
# NOT the 2000050/2000069 Ecocare/Infigo codes resolve_project_code() computes from the job-sheet
# prefix (those are confirmed JBTC-only, per the same live session). Since every SKTC sample we have
# is adhoc PEST CONTROL work (not mosquito-specific), this defaults every SKTC WO to the "Pest
# control" project site (2000073) as the reasonable assumption — CONFIRM with the client whether any
# SKTC WOs should instead map to "Mosquito" (2000130), and whether job_sheet_number's alphabetic/
# numeric prefix means anything for SKTC at all or if it's purely service-type-based.
SKTC_PROJECT_SITE_MATCH = "2000073"


def _dry_guard(action: str) -> bool:
    """Return True if the (final, mutating) action should be SKIPPED due to DRY_RUN.

    Logs the decision either way so the run log clearly shows what was/wasn't submitted.
    """
    if settings.DRY_RUN:
        logger.info("[DRY_RUN] SKIPPING final action: %s", action)
        return True
    logger.info("[LIVE] executing final action: %s", action)
    return False


def synergix_configured() -> bool:
    """True once the client's Synergix env is available (base URL set).

    Until the sandbox is procured this is False, and the driver runs in STUB mode: no browser is
    launched, the duplicate check returns False, and write() reports PROCESSED with a stub note so
    the full ingest -> extract -> validate -> approve loop can be exercised end to end. Filling in
    SYNERGIX_* in .env switches every step to the real browser automation with no code change.
    """
    return bool(settings.SYNERGIX_BASE_URL.strip())


class SynergixDriver:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._logged_in = False

    @property
    def stubbed(self) -> bool:
        """True when Synergix is not configured yet — driver runs without a browser."""
        return not synergix_configured()

    async def start(self) -> None:
        if self.stubbed:
            logger.warning(
                "Synergix not configured (SYNERGIX_BASE_URL empty) — running in STUB mode: "
                "no browser, dedup skipped, writes are simulated. Fill SYNERGIX_* in .env to go live."
            )
            return
        self._pw = await async_playwright().start()
        # Synergix (taskhub.ls2.sg) sits behind Cloudflare bot protection: headless Chromium gets
        # blocked. Run a persistent, non-headless context with a realistic UA and the automation flag
        # disabled so we look like a normal browser. Persisting the profile also reuses the login
        # cookie across runs (Synergix sessions are short-lived).
        settings.SYNERGIX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._context = await self._pw.chromium.launch_persistent_context(
            str(settings.SYNERGIX_SESSION_DIR),
            headless=settings.SYNERGIX_HEADLESS,
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        logger.info("Synergix browser launched (headless=%s, persistent)", settings.SYNERGIX_HEADLESS)

    async def close(self) -> None:
        if self.stubbed:
            return
        # Settle before closing: confirmed live (2026-08-17) that the LAST record touched before a
        # batch ends can silently revert a just-typed value. A field committing in the DOM (what
        # every verify/poll step in this file checks) is the client's own optimistic update, sent
        # to the server via an async PrimeFaces ajax request — it is not proof the server has
        # actually received and saved it yet. Spot-checked 7 records spanning an entire 56-item
        # batch: every one PRIOR to the last-processed record persisted correctly; only the very
        # last one (with no next-item processing to buffer the gap before this close() call) had
        # reverted to its pre-edit value. Closing the browser can kill an in-flight save request
        # before the server ever processes it. This wait gives that final request time to land,
        # for every caller of close() (the main batch, and every ad-hoc script), not just the one
        # instance that already happened to fail.
        if self.page:
            try:
                await self.page.wait_for_timeout(4000)
            except Exception:
                pass
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        if self._logged_in:
            return
        if not settings.SYNERGIX_BASE_URL:
            raise RuntimeError("SYNERGIX_BASE_URL is not set in .env")
        assert self.page is not None
        # JSF/PrimeFaces app keeps connections open, so wait on DOM content, not networkidle.
        await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)

        login_field = self.page.locator(S.require("SYNERGIX_USERNAME_INPUT", S.SYNERGIX_USERNAME_INPUT))
        if await login_field.count():
            # Login form is present -> authenticate.
            await login_field.fill(settings.SYNERGIX_USERNAME)
            await self.page.fill(
                S.require("SYNERGIX_PASSWORD_INPUT", S.SYNERGIX_PASSWORD_INPUT), settings.SYNERGIX_PASSWORD
            )
            await self.page.click(S.require("SYNERGIX_LOGIN_BUTTON", S.SYNERGIX_LOGIN_BUTTON))
        else:
            # Persisted session already logged in — we landed straight on the app.
            logger.info("Synergix session reused (no login form present)")

        # Confirm we're on the app: the header home button is always present once authenticated.
        await self.page.wait_for_selector("#headerToolbarFormLeft\\:homeButton", timeout=30000)
        self._logged_in = True
        logger.info("Synergix login successful")

    # ------------------------------------------------------------------ session guardrails
    async def _is_session_expired(self) -> bool:
        """True if the page shows Synergix's 'your page has expired' screen.

        Synergix has a session timeout; after it fires, every action lands on this screen (with a
        'Reload Page' button and no app chrome), which otherwise causes 30s-per-click timeouts that
        stack for many minutes. Detecting it lets us re-login immediately instead.
        """
        if not self.page:
            return False
        try:
            body = (await self.page.inner_text("body", timeout=3000)) or ""
        except Exception:
            return False
        return "page has expired" in body.lower() or "idle for too long" in body.lower()

    async def _ensure_session(self) -> None:
        """Fail-fast session check + auto-recovery. Call at the start of each Synergix operation.

        If the session has expired, drop the logged-in flag and re-login (fresh session), so a
        mid-batch expiry self-heals instead of stalling. Cheap when the session is healthy.
        """
        assert self.page is not None
        if await self._is_session_expired():
            logger.warning("Synergix session expired — re-logging in")
            self._logged_in = False
            await self.login()

    async def relogin(self) -> None:
        """Force a fresh Synergix session (used proactively between WOs to stay under the timeout)."""
        logger.info("Synergix proactive re-login")
        self._logged_in = False
        await self.login()

    # ------------------------------------------------------------------ duplicate check
    def _dedup_search_value(self, payload: WOPayload) -> str:
        """The WO field to search Synergix on, per SYNERGIX_DEDUP_KEY config."""
        if settings.SYNERGIX_DEDUP_KEY == "job_sheet":
            return payload.job_sheet_number
        return payload.wo_po_number

    async def _open_service_quotation_list(self) -> None:
        """Navigate (logged in) to General Service -> Service Quotation - LS2 list view.

        Re-navigates from the base URL each time so the grid is a fresh, unfiltered instance — calling
        this twice in one session without the reset can leave a stale/filtered datatable. If the
        session has expired, re-login and retry once (self-healing).
        """
        await self.login()
        assert self.page is not None
        for attempt in (1, 2):
            await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(4000)
            if await self._is_session_expired():
                logger.warning("Session expired on nav (attempt %d) — re-logging in", attempt)
                self._logged_in = False
                await self.login()
                continue
            await self.page.get_by_text("General Service", exact=False).first.click()
            await self.page.wait_for_timeout(3000)
            await self.page.get_by_text("Service Quotation - LS2", exact=False).first.click()
            # The JSF datatable renders lazily; wait for the Enquiry/Subject column to exist.
            await self.page.wait_for_selector("th:has-text('Enquiry/Subject')", timeout=30000)
            await self.page.wait_for_timeout(4000)
            return
        raise RuntimeError("could not open Service Quotation list after re-login")

    async def check_duplicate(self, payload: WOPayload) -> DedupResult:
        """Is this WO already invoiced in Synergix? Returns a three-state, fail-safe result.

        The JBTC "Un-Invoiced WO" list is hand-maintained and can be stale, so Synergix is the source
        of truth. Any error, timeout, or ambiguous page yields UNCERTAIN (not NOT_DUPLICATE), so a WO
        is NEVER silently billed when we can't confirm it is unbilled — avoiding double invoicing.

        A confirmed NOT_DUPLICATE requires positive evidence of "no records" (the no-result marker),
        not merely the absence of result rows — otherwise a layout change would read as "safe to bill".
        """
        search_value = self._dedup_search_value(payload)

        if self.stubbed:
            if settings.DEDUP_STUB_ASSUME_SAFE:
                logger.warning("[STUB] DEDUP_STUB_ASSUME_SAFE=true — assuming %s NOT invoiced "
                               "(DEV ONLY; never use with real billing)", search_value)
                return DedupResult.NOT_DUPLICATE
            # No Synergix yet: we CANNOT verify invoiced status, so this is genuinely uncertain.
            logger.warning("[STUB] cannot verify invoiced status for %s (Synergix not configured) "
                           "-> UNCERTAIN (needs human review)", search_value)
            return DedupResult.UNCERTAIN

        try:
            await self._open_service_quotation_list()
            assert self.page is not None

            # Filter the Enquiry/Subject column by the WO-PO. This JSF grid's ids are auto-generated,
            # so target the column by its header text, then the stable filter class within it. The
            # PrimeFaces column filter applies on Enter.
            header = self.page.locator("th", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill("")
            await filter_input.press("Enter")            # clear any prior filter first
            await self.page.wait_for_timeout(2000)
            await filter_input.fill(search_value)
            await filter_input.press("Enter")
            await self.page.wait_for_timeout(6000)        # PrimeFaces ajax re-filter + settle

            # Read the filtered grid body: does it contain the WO-PO, or the "No records found" row?
            grid = await self.page.evaluate(
                "(wo) => { const t = document.querySelector('[id$=\"serviceQuotationTable_data\"]');"
                " const txt = t ? t.innerText : '';"
                " return { present: !!t, empty: /no records found|no data/i.test(txt),"
                " match: txt.includes(wo) }; }",
                search_value,
            )

            if not grid["present"]:
                logger.warning("Dedup %s: UNCERTAIN (quotation grid not found)", search_value)
                return DedupResult.UNCERTAIN
            if grid["match"]:
                logger.info("Dedup %s: DUPLICATE (existing quotation found)", search_value)
                return DedupResult.DUPLICATE
            if grid["empty"]:
                logger.info("Dedup %s: NOT_DUPLICATE (Synergix reports 'No records found')", search_value)
                return DedupResult.NOT_DUPLICATE
            # Filtered but neither a WO match nor the explicit empty marker — can't be sure. Fail safe.
            logger.warning("Dedup %s: UNCERTAIN (no WO match and no 'no records' marker)", search_value)
            return DedupResult.UNCERTAIN
        except Exception:
            logger.exception("Dedup check for %s errored — returning UNCERTAIN (fail-safe)", search_value)
            return DedupResult.UNCERTAIN

    # ------------------------------------------------------------------ write path
    async def write(self, payload: WOPayload) -> WriteResult:
        """Run stages B, C, D for one WO. Returns a WriteResult; never raises for a WO-level error."""
        if self.stubbed:
            project_code = resolve_project_code(payload.job_sheet_number)
            remarks = build_remarks(payload)
            logger.info(
                "[STUB] would write %s to Synergix: project_code=%s\n  remarks=%s",
                payload.wo_po_number, project_code, remarks,
            )
            return WriteResult(
                WOStatus.PROCESSED,
                "Synergix stubbed — no write performed (SYNERGIX_* not configured). "
                f"project_code={project_code}",
            )
        try:
            await self.login()
            await self._stage_b_create_quotation(payload)
            await self._assert_details_filled(payload)
            quo_id = await self._submit_quotation(payload)
            # Schedule Board (C) and Fulfil (D) remain manual — done by the team in Synergix.
            if settings.DRY_RUN:
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"DRY_RUN: quotation draft {quo_id or '(id unread)'} created + filled, NOT submitted.",
                )
            return WriteResult(
                WOStatus.PROCESSED,
                f"Quotation {quo_id or '(id unread)'} created + submitted. "
                "Schedule board + fulfil still manual.",
            )
        except S.MissingSelectorError as exc:
            logger.error("MISSING SELECTOR: %s — fill it in config/selectors.py", exc)
            return WriteResult(WOStatus.FAILED, f"missing selector: {exc}")
        except Exception as exc:
            logger.exception("Synergix write failed for %s", payload.wo_po_number)
            await self._screenshot(payload.wo_po_number)
            return WriteResult(WOStatus.FAILED, str(exc))
        finally:
            await self._back_to_home()

    async def _abort_blank_draft(self, quo_id: str | None, wo_po_number: str) -> None:
        """Best-effort cleanup: abort a just-created draft that failed before Customer was even set,
        so a genuinely empty shell (no customer, no subject, nothing a human could act on) doesn't
        linger in Synergix forever.

        Added 2026-08-17 after an independent audit found QUO0006650 — a fresh blank shell dated
        that same day, created by exactly this failure mode (a Customer-selection crash right after
        the "+" click, from a stuck blockUI overlay). The write() pipeline's normal except handler
        just logs and returns FAILED with no cleanup step, which is how the ORIGINAL 151-empty-
        quotation incident this whole investigation started from actually happened — this closes
        that gap for the specific case where NOTHING useful was captured yet.

        Deliberately only called this early. A failure after Customer/Salesperson/Details are
        already set still leaves a (possibly incomplete) draft that has real review value — e.g.
        the 54 partial drafts from tonight's runs — and must NOT be aborted just because one later
        step failed.
        """
        if not quo_id:
            logger.warning(
                "Stage B failed before Customer was set for %s, and no draft id could be read to "
                "clean up — a blank shell may have been left behind; check Synergix manually",
                wo_po_number)
            return
        try:
            logger.warning("Aborting blank draft %s for %s (failed before Customer was set)",
                            quo_id, wo_po_number)
            await self.abort_quotation(quo_id)
        except Exception:
            logger.exception("Could not abort blank draft %s for %s — may need manual cleanup",
                              quo_id, wo_po_number)

    async def abort_quotation(self, quotation_no: str) -> bool:
        """Admin/cleanup utility, NOT part of the regular write pipeline: open an existing quotation
        by its number and abort (discard) it — for removing bad/orphaned drafts, e.g. the batch of
        empty quotations a full SOP compliance audit found from before the Details-grid fill was
        fixed. Only works on an un-submitted draft (Revision 0) — Abort is Synergix's own action for
        discarding a draft, distinct from Cancel/Delete on a submitted record.

        Returns True if the quotation was found and no longer appears in the list afterward.
        """
        assert self.page is not None
        page = self.page
        await self.login()
        await self._open_service_quotation_list()

        header = page.locator("th", has_text="Quotation No.").first
        filter_input = header.locator("input.ui-column-filter").first
        await filter_input.click()
        await filter_input.fill(quotation_no)
        await filter_input.press("Enter")
        await page.wait_for_timeout(3000)

        link = page.get_by_role("link", name=quotation_no, exact=True)
        if not await link.count():
            logger.warning("abort_quotation: %s not found in the list", quotation_no)
            return False
        await link.first.click(timeout=10000)
        await page.wait_for_timeout(4000)

        abort_btn = page.locator("button.abort-button").first
        if not await abort_btn.count():
            logger.warning("abort_quotation: no Abort button for %s — may already be submitted "
                            "(Abort only applies to un-submitted drafts)", quotation_no)
            return False
        await abort_btn.click(timeout=10000)
        await page.wait_for_timeout(1500)
        yes_btn = page.get_by_role("button", name="Yes")
        if await yes_btn.count() and await yes_btn.first.is_visible():
            await yes_btn.first.click(timeout=10000)
            await page.wait_for_timeout(3000)

        # Verify it's actually gone rather than trusting the click succeeded.
        await self._open_service_quotation_list()
        filter_input2 = page.locator("th", has_text="Quotation No.").first.locator("input.ui-column-filter").first
        await filter_input2.click()
        await filter_input2.fill(quotation_no)
        await filter_input2.press("Enter")
        await page.wait_for_timeout(3000)
        still_there = await page.get_by_text(quotation_no, exact=True).count() > 0
        if still_there:
            logger.warning("abort_quotation: %s still present after Abort+Yes", quotation_no)
            return False
        logger.info("Aborted quotation %s", quotation_no)
        return True

    async def _read_labeled_value(self, label: str) -> str:
        """Read the current value of a labeled form field (see _fill_labeled_input) without
        changing it. Unlike _fill_labeled_input, does not exclude [readonly] — reading should work
        regardless of the field's edit state.

        Falls back to the label's own parent container if it has no `<tr>` ancestor. Confirmed live
        (2026-08-17) that "External Remarks" uses a div-based grid layout (label + content as
        sibling divs under a shared `grid-item-column` parent), not a table row — `closest('tr')`
        found nothing, so this always silently returned '' for that field regardless of its real
        value, masking whether a fix actually worked or not.
        """
        assert self.page is not None
        value = await self.page.evaluate(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return '';
                const scope = host.closest('tr') || host.parentElement;
                const input = scope ? scope.querySelector('input:not([type=hidden]), textarea') : null;
                return input ? input.value : '';
            }""",
            label,
        )
        return value or ""

    async def amend_quotation(self, quotation_no: str, payload: WOPayload) -> dict:
        """Admin/cleanup utility, NOT part of the regular write pipeline: open an existing,
        incomplete quotation (Customer/Subject/GL/Project Site already correct, Details grid empty —
        the shape every quotation from before the 2026-08-14 fill fix came out in) and fill in
        whatever's missing, reusing the same fixed logic _stage_b_create_quotation uses. Does NOT
        touch Customer/Salesperson/Project Site (already correct) or Subject/Reference No. — only
        adds Details rows, sets Payment Method, and fills Project Site if it was left blank.

        `payload` must already have the correct wo_po_number/line_items/job_sheet_number/etc. for
        this quotation (the caller is expected to have matched it up, e.g. via the Subject field).
        Leaves the quotation as an un-submitted draft either way — never submits.

        Returns a dict: {"quo": ..., "wo_po": ..., "assertion": "ok"|<error message>}.
        """
        assert self.page is not None
        page = self.page
        await self.login()
        await self._open_service_quotation_list()

        header = page.locator("th", has_text="Quotation No.").first
        filter_input = header.locator("input.ui-column-filter").first
        await filter_input.click()
        await filter_input.fill(quotation_no)
        await filter_input.press("Enter")
        await page.wait_for_timeout(2500)

        link = page.get_by_role("link", name=quotation_no, exact=True)
        if not await link.count():
            return {"quo": quotation_no, "wo_po": payload.wo_po_number, "assertion": "not_found"}
        await link.first.click(timeout=10000)
        await page.wait_for_timeout(5000)

        subject = await self._read_labeled_value("Enquiry/Subject")
        if payload.wo_po_number not in subject:
            return {"quo": quotation_no, "wo_po": payload.wo_po_number,
                    "assertion": f"subject mismatch: quotation shows {subject!r}"}

        # Project Site, only if currently blank (don't disturb an already-correct value). Must run
        # BEFORE Payment Method: confirmed live (2026-08-17) that Payment Method's tab-activation
        # (see _ensure_tab_active) switches away from the "General" tab Project Site lives on and
        # never switches back, so filling Payment Method first leaves Project Site's own input
        # genuinely not visible — a Locator.click on it then times out for the full 30s rather than
        # failing fast. _stage_b_create_quotation already does Project Site before Payment Method
        # for the same reason; this just matches that proven order.
        project_site_value = await self._read_labeled_value("Project Site")
        if not project_site_value.strip():
            if is_jbtc(payload.town_council):
                search_term = _PROJECT_SITE_SEARCH_JBTC
                match_fragment = resolve_project_code(payload.job_sheet_number)
            else:
                search_term = _PROJECT_SITE_SEARCH_SKTC
                match_fragment = SKTC_PROJECT_SITE_MATCH
            if not await self._select_autocomplete_row("Project Site", search_term, match_fragment):
                logger.warning("amend_quotation: no Project Site match for %s", quotation_no)

        # External Remarks, only if currently blank (don't disturb an already-correct value).
        external_remarks_value = await self._read_labeled_value("External Remarks")
        if not external_remarks_value.strip():
            if not await self._select_external_remark(EXTERNAL_REMARK_CODE):
                logger.warning("amend_quotation: no External Remarks match for %r on %s",
                                EXTERNAL_REMARK_CODE, quotation_no)

        # Payment Method, if still at the placeholder.
        if not await self._select_dropdown_option("Payment Method", PAYMENT_METHOD):
            logger.warning("amend_quotation: could not set Payment Method for %s", quotation_no)

        # Details rows: add rows only up to the target count — idempotent, so re-running this on a
        # quotation from a prior partially-failed attempt doesn't pile on duplicate rows.
        line_items = payload.effective_line_items
        remarks = build_remarks(payload)
        existing_rows = await page.evaluate(
            """() => { const g = [...document.querySelectorAll('.ui-datatable')]
                .find(g => [...g.querySelectorAll('th')].some(t => /unit price/i.test(t.innerText)));
                return g ? g.querySelectorAll('[id$="_data"] tr').length : 0; }"""
        )
        added = existing_rows
        while added < len(line_items):
            if not await self._add_line_item():
                break
            added += 1
        for i in range(min(added, len(line_items))):
            await self._fill_line_item_row(i, line_items[i], remarks)
        if added:
            await self._verify_and_refill_rows(line_items[:min(added, len(line_items))], remarks)

        try:
            await self._assert_details_filled(payload)
            result = {"quo": quotation_no, "wo_po": payload.wo_po_number, "assertion": "ok"}
        except Exception as exc:
            result = {"quo": quotation_no, "wo_po": payload.wo_po_number, "assertion": str(exc)}
        await self._back_to_home()
        return result

    def _subject(self, payload: WOPayload) -> str:
        """Enquiry/Subject string, capped at Synergix's 50-char limit.

        Format `WO-PO/<num> - <Town Council>` (service-type suffix dropped per client, to fit 50).
        """
        council = payload.town_council.strip().title() or "Town Council"
        subject = f"{payload.wo_po_number} - {council}"
        return subject[:50]

    async def _fill_labeled_input(self, label: str, value: str, *, timeout_ms: int = 4000) -> None:
        """Fill the input/textarea belonging to a form field identified by its on-screen label.

        Synergix's JSF ids are auto-generated, so we anchor on the (stable) label text and take the
        input in the same table row. Raises if the field can't be found — the caller marks FAILED.

        Returns a Locator built from the input's own `id` — NOT an ElementHandle from
        `evaluate_handle()`. Confirmed live (2026-08-15) that an ElementHandle is a frozen reference
        to one specific DOM node: Customer selection cascades an ajax update (Address/Contact/
        Currency/Sales Tax/SBU all re-render), and if that cascade replaces this field's node between
        grabbing the handle and clicking it, the click raises "Element is not attached to the DOM" —
        confirmed live on the Customer Contact field, which is filled right after Customer's cascade.
        A Locator re-resolves the id at click time instead of clicking a stale reference, and its
        ids are stable across a PrimeFaces re-render even though the DOM node object is replaced.

        Polls for the host+input to appear (up to timeout_ms) instead of a single check. Confirmed
        live (2026-08-17, per a client SOP review) that Customer Contact was failing with "could not
        locate the input" on effectively every WO all night: it's filled immediately after Customer's
        own cascade, and a one-shot check right after that cascade starts can run before the
        cascade-rendered row exists yet — the same ajax-timing class of bug already fixed elsewhere
        in this file (_click_panel_row_by_text, _select_dropdown_option's option match).
        """
        assert self.page is not None
        page = self.page
        js = """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return null;
                const tr = host.closest('tr');
                const input = tr && tr.querySelector('input:not([type=hidden]):not([readonly]), textarea');
                return input ? input.id : null;
            }"""
        input_id = None
        elapsed = 0
        step_ms = 300
        while elapsed <= timeout_ms:
            input_id = await page.evaluate(js, label)
            if input_id:
                break
            await page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if not input_id:
            raise RuntimeError(f"could not locate the input for field {label!r}")
        field = page.locator(f'[id="{input_id}"]')
        await field.click()
        await field.fill(value)

    async def _select_external_remark(self, remark_code: str, *, timeout_ms: int = 6000) -> bool:
        """Set the 'External Remarks' field via its magnifying-glass search picker, selecting the
        row whose Remark Code matches `remark_code` (e.g. "OCBC BANK DETAIL"). Returns whether a
        match was found and selected; leaves the field untouched otherwise.

        A structurally distinct widget from every other autocomplete in this file: clicking the
        search button opens a real datatable of remark code/description rows, but the row's actual
        click handler (SynFaces.searchPanel.onSearchPanelResultSelect(...), which both sets the
        textarea and fires a PrimeFaces ajax update) lives on an `<a>` INSIDE the row's first cell,
        not on the `<tr>` itself. Confirmed live (2026-08-17) that clicking the row the usual way
        (a Locator built on the `<tr>`, which Playwright clicks at its bounding-box center) landed
        on empty cell space next to the link and left the textarea untouched, with no error — the
        same silent-failure shape documented throughout this file, just with the wrong element
        being the row instead of the link inside it.
        """
        assert self.page is not None
        page = self.page

        button_id = await page.evaluate(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('div.grid-item-label')]
                    .find(e => norm(e.textContent) === label);
                if (!host) return null;
                const container = host.parentElement;
                const btn = container ? container.querySelector('.synfaces-search-button') : null;
                return btn ? btn.id : null;
            }""",
            "External Remarks",
        )
        if not button_id:
            return False
        try:
            await page.locator(f'[id="{button_id}"]').click(timeout=10000)
        except Exception:
            return False

        marker = "data-claude-remark-target"
        js = """([needle, marker]) => {
                const table = document.querySelector('[id$="searchResultTable"]');
                if (!table || table.offsetParent === null) return false;
                const rows = [...table.querySelectorAll('tbody tr')];
                const match = rows.find(r => r.innerText.includes(needle));
                if (!match) return false;
                const link = match.querySelector('a');
                if (!link) return false;
                link.setAttribute(marker, '1');
                return true;
            }"""
        found = False
        elapsed = 0
        step_ms = 300
        while elapsed <= timeout_ms:
            found = await page.evaluate(js, [remark_code, marker])
            if found:
                break
            await page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if not found:
            await page.keyboard.press("Escape")
            return False
        try:
            await page.locator(f'[{marker}="1"]').click(timeout=10000)
        except Exception:
            return False
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        await page.wait_for_timeout(1500)
        return True

    async def _ensure_tab_active(self, element_id: str) -> None:
        """If `element_id` sits inside a hidden PrimeFaces tab panel, click that tab's header to
        activate it first.

        Confirmed live (2026-08-15) via direct DOM inspection that Payment Method/Payment Term
        live on a SEPARATE tab (icon-only header, no visible text — matched by position among
        sibling tabs, not label) from the "General" tab that's active when a quotation draft is
        first created. Every earlier fix attempt at "could not set Payment Method" (coordinate
        click, then marker-Locator click, then polling) missed this because the trigger really was
        `display: none` the whole time — not a timing race or wrong-element click, a genuinely
        inactive tab. `computedStyle(el).display === 'none'` on every ancestor check confirmed this.
        """
        assert self.page is not None
        page = self.page
        marker = "data-claude-tab-target"
        stamped = await page.evaluate(
            """([elementId, marker]) => {
                const el = document.getElementById(elementId);
                if (!el) return false;
                const panel = el.closest('.ui-tabs-panel');
                if (!panel || !panel.classList.contains('ui-helper-hidden')) return false;
                const panelsContainer = panel.parentElement;
                const panels = [...panelsContainer.children];
                const panelIndex = panels.indexOf(panel);
                const tabsRoot = panelsContainer.closest('.ui-tabs');
                const nav = tabsRoot ? tabsRoot.querySelector('ul.ui-tabs-nav') : null;
                const headers = nav ? [...nav.children] : [];
                const header = headers[panelIndex];
                if (!header) return false;
                header.setAttribute(marker, '1');
                return true;
            }""",
            [element_id, marker],
        )
        if not stamped:
            return
        try:
            await page.locator(f'[{marker}="1"]').click(timeout=5000)
        except Exception as exc:
            logger.warning("Could not activate the tab containing %s: %s", element_id, exc)
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        await page.wait_for_timeout(800)

    async def _select_dropdown_option(self, label: str, option_text: str) -> bool:
        """Select an option from a plain PrimeFaces `ui-selectonemenu` dropdown by its label.

        Unlike Customer/Salesperson/Project Site (live-search autocompletes — _select_autocomplete_row)
        this is a closed, fixed-option dropdown (role=combobox, aria-haspopup=listbox): click the
        trigger to open its panel, then click the matching `<li>` by text.

        The option `<li>` is clicked via a real Playwright Locator built from a marker attribute
        stamped onto that exact element, polling for the panel to render — NOT a raw
        `page.mouse.click(x, y)` at a computed bounding-box center. Confirmed live (2026-08-15) that
        this dropdown (used for Payment Method) was failing on almost every WO in a full batch run
        with "could not set Payment Method"; the coordinate click is the same silent-failure class
        documented on _grid_cell_locator and _click_panel_row_by_text.

        Also activates the field's tab first if it's hidden — see _ensure_tab_active's docstring
        for the real root cause this addresses (Payment Method/Term live on a non-default tab).
        """
        assert self.page is not None
        page = self.page
        trigger_id = await page.evaluate(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return null;
                const tr = host.closest('tr');
                const trigger = tr ? tr.querySelector('.ui-selectonemenu') : null;
                return trigger ? trigger.id : null;
            }""",
            label,
        )
        if not trigger_id:
            return False
        await self._ensure_tab_active(trigger_id)
        try:
            await page.locator(f'[id="{trigger_id}"]').click(timeout=10000)
        except Exception:
            return False

        marker = "data-claude-panel-target"
        js = """([needle, marker]) => {
                const panels = [...document.querySelectorAll('[id$="_panel"]')]
                    .filter(p => p.offsetParent !== null);
                for (const panel of panels.slice().reverse()) {
                    const items = [...panel.querySelectorAll('li')];
                    const match = items.find(li => li.textContent.trim() === needle);
                    if (match) { match.setAttribute(marker, '1'); return true; }
                }
                return false;
            }"""
        matched = False
        elapsed = 0
        step_ms = 300
        timeout_ms = 4000
        while elapsed <= timeout_ms:
            matched = await page.evaluate(js, [option_text, marker])
            if matched:
                break
            await page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if not matched:
            await page.keyboard.press("Escape")
            return False
        try:
            await page.locator(f'[{marker}="1"]').click(timeout=10000)
        except Exception:
            return False
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        await page.wait_for_timeout(500)
        return True

    async def _select_autocomplete_row(
        self, label: str, search_text: str, must_contain: str, *, timeout_ms: int = 8000
    ) -> bool:
        """Click a live-autocomplete field by its label, type search_text, and click the first
        visible dropdown row whose text contains must_contain.

        Synergix's Customer/Salesperson/Project Site fields are all the same PrimeFaces pattern: a
        plain input that, once focused and typed into, ajax-populates a floating panel of matching
        rows (NOT a modal). Confirmed live (2026-08-01/03) that clicking via generic text-locator
        matching is unreliable — with many near-identical rows, Playwright's own visibility check on
        the matched text node intermittently reports it as hidden even though it's visibly on screen.

        The row is located via JS (by text match within the visible panel), then clicked through a
        real Playwright Locator built from a marker attribute stamped onto that exact element — NOT
        raw `page.mouse.click(x, y)` at its computed bounding-box coordinates. Confirmed live
        (2026-08-14) that coordinate clicks in this app can silently land on an unrelated overlapping
        element (`document.elementFromPoint()` at the computed point returned a different container
        entirely) with no error and no visible symptom besides the selection never taking — the exact
        bug that left the Details grid empty on 151 production quotations; see _grid_cell_locator's
        docstring for the full story. A stamped-attribute Locator gets Playwright's own actionability
        checks (auto-scroll, and a real thrown error if something intercepts the click).

        The FIELD's own input is now located the same way (host label -> its <tr> -> the real
        <input>), not the offset-coordinate click used here previously. Confirmed live (2026-08-15,
        the full-batch rerun) that the previous `page.mouse.click(box.x + box.width + 80, box.y + 8)`
        guess intermittently missed the actual input — same silent-failure shape as the grid-cell bug
        — showing up as flaky "no Customer match found" / "could not select a Salesperson" despite
        the exact same council/name working moments earlier or later in the same run.

        The input is a Locator built from its own `id`, NOT an ElementHandle from
        `evaluate_handle()`. Confirmed live (2026-08-15, immediately after the fix above) that an
        ElementHandle is a frozen reference to one specific DOM node — Customer's own selection
        cascades an ajax update that re-renders several nearby fields (including Salesperson/Project
        Site), and clicking a handle grabbed just before that cascade lands can raise "Element is not
        attached to the DOM". A Locator re-resolves the id at click time instead.
        """
        assert self.page is not None
        page = self.page
        # Required fields render as "Label *" (the asterisk is part of the same text node, not a
        # separate element), so a plain exact match on e.g. "Salesperson" misses "Salesperson *"
        # (confirmed live, 2026-08-03).
        input_id = await page.evaluate(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0
                    && (norm(e.textContent) === label || norm(e.textContent) === label + ' *'));
                if (!host) return null;
                const tr = host.closest('tr');
                const input = tr && tr.querySelector('input:not([type=hidden]), textarea');
                return input ? input.id : null;
            }""",
            label,
        )
        if not input_id:
            raise RuntimeError(f"could not locate the {label!r} field label")
        field_input = page.locator(f'[id="{input_id}"]')
        await field_input.click()
        await page.wait_for_timeout(300)
        # Clear any existing text before typing — confirmed live (2026-08-17) that calling this
        # twice on the same field (e.g. a failed search followed by restoring the original value)
        # otherwise inserts the new text into the middle of whatever was already there instead of
        # replacing it, since a plain click() only focuses the field without selecting its content.
        await page.keyboard.press("Control+A")
        await page.keyboard.type(search_text)

        match_text = await self._click_panel_row_by_text(must_contain, timeout_ms=timeout_ms)
        if not match_text:
            await page.keyboard.press("Escape")  # close any half-open panel before the next field
            return False
        await page.wait_for_timeout(1000)
        logger.info("Selected %s row: %s", label, match_text.replace("\t", " | "))
        return True

    async def _try_set_customer_contact(self, name: str) -> bool:
        """Try to select `name` as Customer Contact via the field's own autocomplete search;
        restore the original selection if there's no match, rather than leaving a typed-but-
        unselected mismatch. Returns whether `name` was actually selected.

        Confirmed live (2026-08-17, per a client SOP review flagging Customer Contact stuck on
        "Account Department" on every quotation) that this field is a real PrimeFaces autoComplete
        with its own hidden id field (`{"party_code":...,"party_contact_code":...}`) backing a
        CLOSED list of contacts registered against the customer in Synergix — not a free-text field.
        Searching a real TCMS-scraped property-officer name (e.g. "NURUL") against it returned zero
        results live: these Town Council officer names generally aren't registered as Synergix
        contacts at all. Blindly `.fill()`-ing the visible input (the previous approach) would show
        the officer's name in the box while the hidden field silently kept pointing at the old
        contact — a display/data mismatch, not a fix. So: try the real search-and-select; if nothing
        matches, re-search-and-reselect the ORIGINAL value to restore a consistent state instead of
        leaving whatever text the failed search typed in.
        """
        original = await self._read_labeled_value("Customer Contact")
        if await self._select_autocomplete_row("Customer Contact", name, name.upper(), timeout_ms=4000):
            return True
        if original.strip():
            if not await self._select_autocomplete_row(
                "Customer Contact", original, original, timeout_ms=4000
            ):
                logger.warning(
                    "Could not restore original Customer Contact %r after failed search for %r",
                    original, name)
        return False

    async def _click_panel_row_by_text(
        self, needle: str, *, exclude_grids_with_headers: bool = False, timeout_ms: int = 6000
    ) -> str | None:
        """Find a visible autocomplete-panel row containing `needle` and click it, returning its
        text (or None if no match). Clicks via a real Playwright Locator built from a marker
        attribute stamped onto the matched row — NOT raw `page.mouse.click(x, y)` at its computed
        bounding-box coordinates. Confirmed live (2026-08-14) that coordinate clicks in this app can
        silently land on an unrelated overlapping element (`document.elementFromPoint()` at the
        computed point returned a different container entirely) with no error and no visible symptom
        besides the selection never taking — the exact bug that left the Details grid empty on 151
        production quotations; see _grid_cell_locator's docstring for the full story.

        Polls for the match to appear (up to timeout_ms) instead of trusting a single check right
        after a fixed sleep. Confirmed live (2026-08-15, the full-batch rerun) that the ajax panel's
        populate time varies with server load — a one-shot check after a fixed wait intermittently
        ran before the panel had rendered, which read as "no match" even though the same search
        would have succeeded a second or two later.
        """
        assert self.page is not None
        page = self.page
        marker = "data-claude-panel-target"
        js = """([needle, marker, excludeHeaders]) => {
                let panels = [...document.querySelectorAll('.ui-datatable, [id$="_panel"]')]
                    .filter(p => p.offsetParent !== null);
                if (excludeHeaders) panels = panels.filter(p => !p.querySelector('th'));
                for (const panel of panels.slice().reverse()) {
                    const rows = [...panel.querySelectorAll('tbody tr')];
                    const match = rows.find(r => r.innerText.includes(needle));
                    if (match) {
                        match.setAttribute(marker, '1');
                        match.scrollIntoView();
                        return match.innerText.slice(0, 150);
                    }
                }
                return null;
            }"""
        match_text = None
        elapsed = 0
        step_ms = 300
        while elapsed <= timeout_ms:
            match_text = await page.evaluate(js, [needle, marker, exclude_grids_with_headers])
            if match_text:
                break
            await page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if not match_text:
            return None
        row = page.locator(f'[{marker}="1"]')
        try:
            await row.click(timeout=10000)
        except Exception as exc:
            logger.warning("Could not click matched panel row for %r: %s", needle, exc)
            return None
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        return match_text

    async def _grid_cell_locator(self, row_index: int, header_regex: str):
        """Locator for a Details-grid cell's input, found by (row_index, column header).

        Returns a Playwright Locator built from the input's own `id` attribute — NOT raw pixel
        coordinates. Confirmed live (2026-08-14) that `getBoundingClientRect()`-based coordinates for
        this grid do not correspond to the actual clickable point: `document.elementFromPoint()` at
        those exact coordinates returned an unrelated `.ui-tabs-panel` container, not the input. A
        `page.mouse.click(x, y)` at such coordinates clicks whatever is really there — silently, no
        error — which is why every previous fill attempt (both synthetic-event and real-keyboard
        typing) "succeeded" with no exception while the value never moved. This is the same class of
        bug the JBTC/TCMS row-selection fix hit earlier: trust Playwright's own actionability-checked
        `.click()`, which auto-scrolls into view and raises a clear error if something intercepts the
        pointer, instead of computing a screen point ourselves.

        Retries briefly (up to ~3s): the grid can momentarily have zero matching `.ui-datatable`
        elements right after Customer/Salesperson/Project Site's AJAX cascades or an "Add Row" click.
        """
        assert self.page is not None
        page = self.page
        js = """([rowIndex, headerRegex]) => {
            const re = new RegExp(headerRegex, 'i');
            const grids = [...document.querySelectorAll('.ui-datatable')];
            for (const g of grids) {
                const heads = [...g.querySelectorAll('th')].map(t => (t.innerText || '').trim());
                const idx = heads.findIndex(h => re.test(h));
                if (idx < 0) continue;
                const rows = [...g.querySelectorAll('[id$="_data"] tr')];
                const row = rows[rowIndex];
                if (!row) continue;
                const cell = [...row.querySelectorAll('td')][idx];
                const input = cell ? cell.querySelector('input,textarea') : null;
                if (!input || !input.id) continue;
                return input.id;
            }
            return null;
        }"""
        for _ in range(6):
            input_id = await page.evaluate(js, [row_index, header_regex])
            if input_id:
                return page.locator(f'[id="{input_id}"]')
            await page.wait_for_timeout(500)
        return None

    async def _select_item_code(self, item_code: str, row_index: int = 0) -> bool:
        """Type into the Details row's Item Code/Desc cell (a table cell input, NOT a labeled form
        field like Customer/Salesperson/Project Site — _select_autocomplete_row's label-based click
        doesn't apply here) and click the matching autocomplete row, same panel-locate pattern.

        `row_index` selects which Details row to fill when the WO has more than one line item — each
        "Add Row" click appends a new row, so index 0 is the first-added row, 1 the second, etc.

        This cell is a PrimeFaces `<p:autoComplete>` (`role="application"`), not a plain input —
        confirmed live (2026-08-14) that it swallows real keyboard events entirely: neither
        `page.keyboard.type()` nor a real keypress after `insertText()` ever changed its value, with
        no error and no visible symptom (the same silent-failure shape as the Details-grid bug).
        `insertText()` alone DOES set the value (it bypasses key events) but never triggers the
        widget's own search, since that's bound to keyup, not input. The fix: set the value via
        `insertText()`, then trigger the search directly through PrimeFaces' own client-side widget
        API (`widget.search(value)`) instead of trying to simulate the right keyboard event.
        """
        assert self.page is not None
        page = self.page
        cell = await self._grid_cell_locator(row_index, "item code")
        if not cell:
            return False
        await cell.click(timeout=10000)
        await page.wait_for_timeout(300)
        await page.keyboard.insert_text(item_code)

        searched = await cell.evaluate(
            """(input, value) => {
                const span = input.closest('.ui-autocomplete');
                let widget = null;
                if (window.PrimeFaces && window.PrimeFaces.widgets) {
                    for (const key in window.PrimeFaces.widgets) {
                        const w = window.PrimeFaces.widgets[key];
                        if (w && w.jq && w.jq[0] === span) { widget = w; break; }
                    }
                }
                if (!widget || typeof widget.search !== 'function') return false;
                widget.search(value);
                return true;
            }""",
            item_code,
        )
        if not searched:
            logger.warning("Could not find the Item Code autoComplete widget instance for row %d", row_index)
            return False

        # The Details grid itself is a .ui-datatable and stays visible throughout, so it can wrongly
        # be picked as "the last visible panel" — exclude it explicitly (it's the only candidate with
        # column headers) and only consider panels whose rows actually contain item_code as a match.
        # Polls (see _click_panel_row_by_text) rather than a single fixed-wait check — confirmed live
        # (2026-08-15) that a one-shot check right after a blind sleep intermittently missed a panel
        # that just hadn't finished its ajax populate yet, reported as "leaving row's item blank",
        # which then left a half-open autocomplete panel that could block the row's next cell click.
        match_text = await self._click_panel_row_by_text(item_code, exclude_grids_with_headers=True)
        if not match_text:
            await page.keyboard.press("Escape")  # close any half-open panel before the next cell
            return False
        await page.wait_for_timeout(1000)
        logger.info("Selected Item Code row: %s", match_text.replace("\t", " | "))
        return True

    async def _add_line_item(self) -> bool:
        """Click the Details grid's 'Add Row' button to insert a blank editable line, needed when
        building a quotation from scratch (there is no pre-existing row to edit, unlike the old Copy
        From flow). Confirmed live (2026-08-03) via the real DOM: the button carries a stable,
        semantically-named class "add-row-button" (distinct from "Add Item from Contract SOR" and
        "Download Import Template", which sit in the same toolbar and were mis-clicked by an earlier,
        purely icon-shape-based guess at this selector).
        """
        assert self.page is not None
        page = self.page
        button = page.locator("button.add-row-button").first
        if not await button.count():
            return False
        await button.click()
        await page.wait_for_timeout(3000)
        return True

    async def _stage_b_create_quotation(self, payload: WOPayload) -> None:
        """Create a draft Service Quotation FROM SCRATCH (no Copy From), fill every field, DON'T submit.

        Replaces the earlier Copy From-based flow: Copy From only lists quotations still in Synergix
        "New" status, which made it fail whenever no draft happened to exist for a given council (this
        blocked EVERY SKTC WO — see project memory synergix-dedup-verified). Building from scratch has
        no such dependency: it works identically for JBTC and SKTC.

        Confirmed live (2026-08-01/03) that Customer, Salesperson, and Project Site are all live
        autocomplete fields, and selecting Customer/Project Site each cascade several dependent fields
        automatically (Customer -> Address/Contact/Currency/Sales Tax/SBU; Project Site -> Project
        In-Charge/Portfolio). Leaves the draft filled and un-submitted for a human to review + Submit.
        """
        assert self.page is not None
        page = self.page
        logger.info("Stage B: create quotation draft for %s", payload.wo_po_number)

        await self._open_service_quotation_list()

        await page.locator("button:has(span.fa-plus)").first.click()
        await page.wait_for_timeout(8000)
        # Captured now, while the freshly-created draft's form is still visibly loaded — used only
        # to clean up a genuinely blank shell if Customer selection fails below. Capturing it here
        # (rather than re-reading it at failure time) matters because the failure mode this guards
        # against can leave the page stuck on an unrelated view (e.g. still the list page behind a
        # stuck blockUI overlay), where _current_quotation_id() would find nothing to abort even
        # though a real empty draft was already created by the "+" click above.
        draft_quo_id = await self._current_quotation_id()

        # --- Customer (cascades Address/Contact/Currency/Sales Tax/SBU) ---
        council = payload.town_council.strip() or "Town Council"
        try:
            customer_ok = await self._select_autocomplete_row("Customer", council, council.upper())
        except Exception:
            await self._abort_blank_draft(draft_quo_id, payload.wo_po_number)
            raise
        if not customer_ok:
            await self._abort_blank_draft(draft_quo_id, payload.wo_po_number)
            raise RuntimeError(f"no Customer match found in Synergix for {council!r}")

        # --- Customer Contact: override the cascaded default with the real TC officer who raised
        # the WO, if TCMS scraping captured one (JBTC/TCMS flow only — unset for SKTC/email WOs,
        # which have no TCMS page to scrape it from). Best-effort: a full SOP audit found this stuck
        # on a generic "Account Department" default on every quotation (MAJOR finding 4.3).
        if payload.property_officer:
            if not await self._try_set_customer_contact(payload.property_officer):
                logger.warning(
                    "Stage B: no registered Customer Contact matches %r for %s — left at the "
                    "cascaded default", payload.property_officer, payload.wo_po_number)

        # --- Salesperson ---
        # TODO(human): "TAN WEI YING" is the salesperson seen on every real quotation observed so
        # far (both councils), suggesting it's a fixed default rather than per-WO — confirm with the
        # client whether this should ever vary.
        salesperson_ok = await self._select_autocomplete_row("Salesperson", "Tan Wei", "TAN WEI YING")
        if not salesperson_ok:
            logger.warning("Stage B: could not select a Salesperson for %s — leaving blank",
                            payload.wo_po_number)

        # --- Project Site (cascades Project In-Charge/Portfolio) ---
        if is_jbtc(payload.town_council):
            search_term = _PROJECT_SITE_SEARCH_JBTC
            match_fragment = resolve_project_code(payload.job_sheet_number)
        else:
            search_term = _PROJECT_SITE_SEARCH_SKTC
            match_fragment = SKTC_PROJECT_SITE_MATCH
        project_site_ok = await self._select_autocomplete_row("Project Site", search_term, match_fragment)
        if not project_site_ok:
            logger.warning(
                "Stage B: no Project Site match for %s (searched %r, expected %r) — leaving blank, "
                "human must set it before Submit", payload.wo_po_number, search_term, match_fragment)

        # --- Subject + Reference No. ---
        await self._fill_labeled_input("Enquiry/Subject", self._subject(payload))
        await self._fill_labeled_input("Reference No.", payload.gl_number)
        logger.info("Stage B: filled Subject + Reference No. for %s", payload.wo_po_number)

        # --- External Remarks (before Payment Method — same tab-hiding reason as Project Site) ---
        if not await self._select_external_remark(EXTERNAL_REMARK_CODE):
            logger.warning("Stage B: no External Remarks match for %r on %s",
                            EXTERNAL_REMARK_CODE, payload.wo_po_number)

        # --- Payment Method (left at the "Sel" placeholder if the target option isn't found) ---
        if not await self._select_dropdown_option("Payment Method", PAYMENT_METHOD):
            logger.warning("Stage B: could not set Payment Method for %s — leaving as placeholder",
                            payload.wo_po_number)

        # --- Line items: add one Details row per WO line item, then fill each ---
        # A WO with N "Job Sheet:" rows in its Description of Work table needs N Synergix rows, or
        # the quotation silently bills only the first line — see WOPayload.line_items's docstring.
        line_items = payload.effective_line_items
        remarks = build_remarks(payload)
        added = 0
        for _ in line_items:
            if not await self._add_line_item():
                break
            added += 1
        if added < len(line_items):
            logger.warning(
                "Stage B: could only add %d/%d Details line item row(s) for %s — human must add the "
                "rest before Submit", added, len(line_items), payload.wo_po_number)
        for i in range(added):
            await self._fill_line_item_row(i, line_items[i], remarks)
        if added:
            await self._verify_and_refill_rows(line_items[:added], remarks)
        logger.info("Stage B: draft filled for %s", payload.wo_po_number)

    async def _assert_details_filled(self, payload: WOPayload) -> None:
        """Raise if any Details row is missing Item Code/Qty/Unit Price/Remarks, or Total After Tax
        is not positive. Called before Submit — never submit (or report success for) an incomplete
        quotation just because no exception happened to fire while filling it.

        Added 2026-08-14 after a full SOP compliance audit found that ALL 151 quotations from an
        earlier production run had silently empty Details rows (Item Code/Qty/Unit Price/Remarks all
        blank, Total 0.00) despite every fill step logging success — the fill mechanism reported
        {priceOk: true, ...} while the value never actually committed. This assertion is the fail-safe
        the audit itself recommended: catch that class of failure explicitly rather than trusting the
        fill code's own optimistic return values.
        """
        assert self.page is not None
        line_items = payload.effective_line_items
        problems: list[str] = []
        rows: list[dict] = []
        for i in range(len(line_items)):
            row = await self._read_grid_row(i, ("item code", "^qty", "unit price", "remarks"))
            rows.append(row)
            if not (row.get("item code") or "").strip():
                problems.append(f"row {i}: Item Code is blank")
            if float(row.get("^qty") or 0) <= 0:
                problems.append(f"row {i}: Qty is {row.get('^qty')!r}")
            if float(row.get("unit price") or 0) <= 0:
                problems.append(f"row {i}: Unit Price is {row.get('unit price')!r}")
            if not (row.get("remarks") or "").strip():
                problems.append(f"row {i}: Remarks is blank")

        # Standing control added 2026-08-17 per a client SOP review of 54 live quotations: every
        # single one was under-billed by exactly the JBTC 10% SOR uplift because the grid was being
        # filled with the gross unit_price instead of the WO-authorised net figure (see
        # LineItem.billed_unit_price). Each row individually looked "filled" the whole time — only a
        # reconciliation against the WO's own net_amount actually catches a wrong-but-nonzero value.
        # Only meaningful once every row is individually clean; skip if the payload has no
        # net_amount to reconcile against (e.g. an unstructured free-text WO).
        if not problems and payload.net_amount is not None:
            computed_total = 0.0
            for row in rows:
                try:
                    computed_total += float(row.get("^qty") or 0) * float(row.get("unit price") or 0)
                except (TypeError, ValueError):
                    pass
            computed_total = round(computed_total, 2)
            if abs(computed_total - payload.net_amount) > 0.05:
                problems.append(
                    f"pre-GST total {computed_total:.2f} does not match WO-authorised net amount "
                    f"{payload.net_amount:.2f}"
                )

        total_js = """() => {
                const label = [...document.querySelectorAll('td,div,span')]
                    .find(e => e.children.length === 0 && e.textContent.trim() === 'Total After Tax:');
                if (!label) return null;
                const row = label.closest('tr') || label.parentElement?.parentElement;
                if (!row) return null;
                const nums = [...row.querySelectorAll('td,div,span')]
                    .map(e => e.textContent.trim())
                    .filter(t => /^[\\d,]+\\.\\d{2}$/.test(t));
                return nums.length ? nums[0] : null;
            }"""
        total_after_tax = await self.page.evaluate(total_js)
        # Total After Tax is a separate PrimeFaces aggregate recalculation, not part of any single
        # cell's own commit — confirmed live (2026-08-15) that every individual row can already read
        # back correct while this summary field still lags at 0.00 for a moment. Only worth polling
        # when the rows themselves are already clean; if a row still has a real problem, that recalc
        # would show 0.00 anyway and there's nothing to wait for.
        if not problems:
            for _ in range(10):
                try:
                    if total_after_tax is not None and float(total_after_tax.replace(",", "")) > 0:
                        break
                except ValueError:
                    pass
                await self.page.wait_for_timeout(300)
                total_after_tax = await self.page.evaluate(total_js)
        try:
            if total_after_tax is None or float(total_after_tax.replace(",", "")) <= 0:
                problems.append(f"Total After Tax is {total_after_tax!r}")
        except ValueError:
            problems.append(f"Total After Tax is unparseable: {total_after_tax!r}")

        if problems:
            raise RuntimeError(
                f"Details grid incomplete for {payload.wo_po_number} — refusing to submit: "
                + "; ".join(problems)
            )

    async def _submit_quotation(self, payload: WOPayload) -> str | None:
        """Submit the filled draft (DRY_RUN-gated). Returns the quotation ID if it can be read.

        In DRY_RUN the Submit click is skipped and logged. Otherwise it clicks Submit and confirms any
        follow-up dialog. The quotation ID (QUO...) is read from the form title bar either way.
        """
        assert self.page is not None
        page = self.page
        quo_id = await self._current_quotation_id()

        if _dry_guard(f"submit quotation for {payload.wo_po_number} (draft {quo_id})"):
            return quo_id  # DRY_RUN: left as a draft

        await page.locator("button:has(span.fa-vote-yea)").first.click()
        await page.wait_for_timeout(3000)
        # A confirm dialog may appear (Yes/OK) — click it if present.
        for label in ("Yes", "OK", "Confirm"):
            btn = page.get_by_role("button", name=label)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click()
                break
        await page.wait_for_timeout(8000)
        logger.info("Submitted quotation %s for %s", quo_id, payload.wo_po_number)
        return quo_id

    async def _current_quotation_id(self) -> str | None:
        """Read the QUO id from the form title bar (e.g. 'Service Quotation - LS2 [QUO0006225]')."""
        assert self.page is not None
        try:
            text = await self.page.locator("text=/QUO[0-9]+/").first.inner_text()
            m = re.search(r"QUO\d+", text)
            return m.group(0) if m else None
        except Exception:
            return None

    async def _fill_line_item_row(self, row_index: int, line_item: LineItem, remarks: str) -> None:
        """Fill one freshly-added Details row: Item Code (autocomplete), Qty, Unit Price, Remarks.

        Unlike the old Copy From flow (where the copied row already had Item Code/Type/Qty from the
        template, and only Unit Price/Remarks needed overwriting), a from-scratch row starts fully
        blank — Item Code must be looked up via its own autocomplete (see _select_item_code; it's a
        table-cell input, not a labeled form field, so the generic _select_autocomplete_row doesn't
        apply) before Unit Price/Remarks can be set. `row_index` picks which row among possibly
        several (one per WO line item) — see _stage_b_create_quotation.
        """
        assert self.page is not None
        label = f"row {row_index}"

        item_ok = await self._select_item_code(ITEM_CODE, row_index)
        if not item_ok:
            logger.warning("Stage B: could not select Item Code %s for %s — leaving %s's item blank",
                            ITEM_CODE, label, label)

        # Qty/Unit Price/Remarks: click each cell via Playwright's own actionability-checked
        # `.click()` (see _grid_cell_locator's docstring for why NOT raw coordinates), then type via
        # real keyboard input and Tab to commit. Verified + retried, not fire-and-forget: confirmed
        # live (2026-08-14) that typing into one cell right after another can silently not stick
        # (Tab likely kicks off a PrimeFaces AJAX recalculation — Total Amount depends on Qty * Unit
        # Price — and moving to the next cell before it settles interrupts that cell's own commit).
        for field_name, value, header_regex in (
            ("Qty", f"{line_item.quantity:.2f}", r"^qty"),
            ("Unit Price", f"{line_item.billed_unit_price:.2f}", "unit price"),
            ("Remarks", remarks, "remarks"),
        ):
            await self._fill_grid_field(row_index, field_name, value, header_regex)

        # Verify what actually stuck (not just "an input existed to click") before trusting it.
        actual = await self._read_grid_row(row_index, ("qty", "unit price", "remarks"))
        logger.info("Stage B line item for %s: %s", label, actual)

    async def _fill_grid_field(
        self, row_index: int, field_name: str, value: str, header_regex: str, *, attempts: int = 3
    ) -> bool:
        """Click one Details-grid cell, type `value`, Tab to commit, and poll to confirm it stuck —
        retrying up to `attempts` times. Returns whether it ultimately committed.

        Extracted from _fill_line_item_row so _verify_and_refill_rows can re-run it standalone on
        any single cell that a later edit clobbered, without re-filling the whole row.
        """
        assert self.page is not None
        page = self.page
        for attempt in range(attempts):
            cell = await self._grid_cell_locator(row_index, header_regex)
            if not cell:
                logger.warning("Stage B: could not locate the %s cell for row %d", field_name, row_index)
                return False
            try:
                await cell.click(timeout=10000)
            except Exception as exc:
                logger.warning("Stage B: could not click the %s cell for row %d: %s", field_name, row_index, exc)
                return False
            await page.keyboard.press("Control+A")
            await page.keyboard.type(value)
            await page.keyboard.press("Tab")

            # Poll instead of a single fixed-wait check: confirmed live (2026-08-15) that the
            # PrimeFaces ajax recalculation Tab kicks off (Total Amount = Qty * Unit Price) has
            # variable settle time under server load — a one-shot check at a fixed delay
            # intermittently read the value as "not stuck" moments before it actually committed.
            for _ in range(8):
                await page.wait_for_timeout(300)
                if await cell.input_value() == value:
                    return True
            logger.warning("Stage B: %s for row %d did not stick (attempt %d) — retrying",
                            field_name, row_index, attempt + 1)
        return False

    async def _verify_and_refill_rows(self, line_items: list[LineItem], remarks: str) -> None:
        """After every Details row has been filled once, re-read ALL rows and re-fill any field
        that doesn't match its target — repeating a few rounds until everything holds or the
        attempt budget runs out.

        Confirmed live (2026-08-15) that a row already read back correctly right after being filled
        can later revert to blank/0.00 once a SUBSEQUENT row's edits fire their own PrimeFaces ajax
        recalculation — a server-side ordering race (the earlier row's commit hadn't landed
        server-side yet when the later row's request went out), not a client-side click/typing bug.
        _fill_line_item_row's own per-cell retry only checks immediately after typing that cell, so
        it can't catch a value reverted by a LATER row's edit. This re-checks everything at the end.
        """
        assert self.page is not None
        for round_num in range(3):
            any_problem = False
            for i, line_item in enumerate(line_items):
                row = await self._read_grid_row(i, ("item code", "^qty", "unit price", "remarks"))
                targets = (
                    ("Qty", f"{line_item.quantity:.2f}", r"^qty", row.get("^qty")),
                    ("Unit Price", f"{line_item.billed_unit_price:.2f}", "unit price", row.get("unit price")),
                    ("Remarks", remarks, "remarks", row.get("remarks")),
                )
                if not (row.get("item code") or "").strip():
                    any_problem = True
                    logger.warning("Verify pass %d: row %d Item Code reverted — re-selecting", round_num + 1, i)
                    if not await self._select_item_code(ITEM_CODE, i):
                        logger.warning("Verify pass %d: could not re-select Item Code for row %d", round_num + 1, i)
                for field_name, target_value, header_regex, current_value in targets:
                    if current_value == target_value:
                        continue
                    any_problem = True
                    logger.warning("Verify pass %d: row %d %s reverted (%r != %r) — re-filling",
                                    round_num + 1, i, field_name, current_value, target_value)
                    await self._fill_grid_field(i, field_name, target_value, header_regex)
            if not any_problem:
                break

    async def _read_grid_row(self, row_index: int, header_regexes: tuple[str, ...]) -> dict:
        """Read back the CURRENT input values of the given Details-grid row's named columns.

        Used to confirm a fill actually committed, since dispatching input/change events on an input
        can silently fail to persist (see _fill_line_item_row's docstring) — reading the live DOM
        value after the fact is the only reliable signal.
        """
        assert self.page is not None
        return await self.page.evaluate(
            """([rowIndex, headerRegexes]) => {
                const out = {};
                const grids = [...document.querySelectorAll('.ui-datatable')];
                for (const g of grids) {
                    const heads = [...g.querySelectorAll('th')].map(t => (t.innerText || '').trim());
                    if (!heads.some(h => /unit price/i.test(h))) continue;
                    const rows = [...g.querySelectorAll('[id$="_data"] tr')];
                    const row = rows[rowIndex];
                    if (!row) continue;
                    const cells = [...row.querySelectorAll('td')];
                    for (const hr of headerRegexes) {
                        const re = new RegExp(hr, 'i');
                        const idx = heads.findIndex(h => re.test(h));
                        const input = idx >= 0 && cells[idx] ? cells[idx].querySelector('input,textarea') : null;
                        out[hr] = input ? input.value : null;
                    }
                    return out;
                }
                return out;
            }""",
            [row_index, list(header_regexes)],
        )

    # ------------------------------------------------------------------ recovery helpers
    async def _back_to_home(self) -> None:
        if not self.page:
            return
        try:
            await self.page.goto(settings.SYNERGIX_BASE_URL)
            await self.page.wait_for_load_state("networkidle")
        except Exception:
            logger.exception("Failed to return Synergix to home state")

    async def _screenshot(self, wo_po_number: str) -> None:
        if not self.page:
            return
        try:
            safe = wo_po_number.replace("/", "-")
            path = settings.LOGS_DIR / f"synergix_error_{safe}.png"
            await self.page.screenshot(path=str(path))
            logger.info("Saved error screenshot: %s", path)
        except Exception:
            logger.exception("Could not capture error screenshot")
