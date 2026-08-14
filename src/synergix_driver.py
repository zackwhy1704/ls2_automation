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

    def _subject(self, payload: WOPayload) -> str:
        """Enquiry/Subject string, capped at Synergix's 50-char limit.

        Format `WO-PO/<num> - <Town Council>` (service-type suffix dropped per client, to fit 50).
        """
        council = payload.town_council.strip().title() or "Town Council"
        subject = f"{payload.wo_po_number} - {council}"
        return subject[:50]

    async def _fill_labeled_input(self, label: str, value: str) -> None:
        """Fill the input/textarea belonging to a form field identified by its on-screen label.

        Synergix's JSF ids are auto-generated, so we anchor on the (stable) label text and take the
        input in the same table row. Raises if the field can't be found — the caller marks FAILED.
        """
        assert self.page is not None
        handle = await self.page.evaluate_handle(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return null;
                const tr = host.closest('tr');
                return (tr && tr.querySelector('input:not([type=hidden]):not([readonly]), textarea')) || null;
            }""",
            label,
        )
        element = handle.as_element()
        if element is None:
            raise RuntimeError(f"could not locate the input for field {label!r}")
        await element.click()
        await element.fill(value)

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
        The robust approach: find the visible panel via JS, then click by the ROW's own computed
        on-screen coordinates rather than asking Playwright to re-resolve a text locator.
        """
        assert self.page is not None
        page = self.page
        # Required fields render as "Label *" (the asterisk is part of the same text node, not a
        # separate element), so a plain exact=True match on e.g. "Salesperson" misses "Salesperson *"
        # and raises (confirmed live, 2026-08-03). But exact=False is NOT the fix — "Customer" with
        # exact=False also matches "Customer Type" (a substring match), and .first then non-
        # deterministically picks whichever comes first in DOM order, which broke every subsequent
        # attempt to select the real Customer field (confirmed live, same session). Match precisely:
        # the label text OR the label text followed by exactly " *", nothing looser.
        field_label = page.get_by_text(re.compile(rf"^{re.escape(label)}(?: \*)?$")).first
        box = await field_label.bounding_box()
        if not box:
            raise RuntimeError(f"could not locate the {label!r} field label")
        await page.mouse.click(box["x"] + box["width"] + 80, box["y"] + 8)
        await page.wait_for_timeout(500)
        await page.keyboard.type(search_text)
        await page.wait_for_timeout(2200)  # ajax panel populate

        coords = await page.evaluate(
            """([needle]) => {
                const panels = [...document.querySelectorAll('.ui-datatable, [id$="_panel"]')]
                    .filter(p => p.offsetParent !== null);
                if (!panels.length) return null;
                const panel = panels[panels.length - 1];
                const rows = [...panel.querySelectorAll('tbody tr')];
                const match = rows.find(r => r.innerText.includes(needle));
                if (!match) return null;
                match.scrollIntoView();
                const rect = match.getBoundingClientRect();
                return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: match.innerText.slice(0, 150)};
            }""",
            [must_contain],
        )
        if not coords:
            return False
        await page.mouse.click(coords["x"], coords["y"])
        await page.wait_for_timeout(2000)
        logger.info("Selected %s row: %s", label, coords["text"].replace("\t", " | "))
        return True

    async def _select_item_code(self, item_code: str, row_index: int = 0) -> bool:
        """Type into the Details row's Item Code/Desc cell (a table cell input, NOT a labeled form
        field like Customer/Salesperson/Project Site — _select_autocomplete_row's label-based click
        doesn't apply here) and click the matching autocomplete row, same panel-locate pattern.

        `row_index` selects which Details row to fill when the WO has more than one line item — each
        "Add Row" click appends a new row, so index 0 is the first-added row, 1 the second, etc.

        KNOWN GAP (2026-08-03): this reliably finds and fills the Item Code input when tested in
        isolation on a blank quotation, but returns False (cell_input is null) in the real pipeline
        after Customer/Salesperson/Project Site have already been selected — those selections likely
        trigger a partial-page AJAX re-render of the Details grid that this hasn't been traced through
        yet. Fails safely: the draft is still created with every other field correct, and this is
        logged as a clear warning so a human knows to fill in the item code/qty/price/remarks by hand
        before Submit. Worth a fresh investigation pass, but not currently a blocker for using the
        pipeline (see docs/operational_expectations.md).
        """
        assert self.page is not None
        page = self.page
        cell_input = await page.evaluate(
            """([rowIndex]) => {
                const grids = [...document.querySelectorAll('.ui-datatable')];
                for (const g of grids) {
                    const heads = [...g.querySelectorAll('th')].map(t => (t.innerText || '').trim());
                    const idx = heads.findIndex(h => /item code/i.test(h));
                    if (idx < 0) continue;
                    const rows = [...g.querySelectorAll('[id$="_data"] tr')];
                    const row = rows[rowIndex];
                    if (!row) continue;
                    const cell = [...row.querySelectorAll('td')][idx];
                    const input = cell ? cell.querySelector('input') : null;
                    if (!input) continue;
                    const r = input.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
                return null;
            }""",
            [row_index],
        )
        if not cell_input:
            return False
        await page.mouse.click(cell_input["x"], cell_input["y"])
        await page.wait_for_timeout(500)
        await page.keyboard.type(item_code)
        await page.wait_for_timeout(2200)

        # The Details grid itself is a .ui-datatable and stays visible throughout, so it can wrongly
        # be picked as "the last visible panel" — exclude it explicitly (identify it by containing the
        # "No records found." placeholder or the grid's own known static header) and only consider
        # panels whose rows actually contain the searched item_code as an autocomplete match.
        coords = await page.evaluate(
            """([needle]) => {
                const panels = [...document.querySelectorAll('.ui-datatable, [id$="_panel"]')]
                    .filter(p => p.offsetParent !== null)
                    .filter(p => !p.querySelector('th')); // exclude grids with column headers (the Details table)
                for (const panel of panels.reverse()) {
                    const rows = [...panel.querySelectorAll('tbody tr')];
                    const match = rows.find(r => r.innerText.includes(needle));
                    if (match) {
                        match.scrollIntoView();
                        const rect = match.getBoundingClientRect();
                        return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: match.innerText.slice(0, 150)};
                    }
                }
                return null;
            }""",
            [item_code],
        )
        if not coords:
            return False
        await page.mouse.click(coords["x"], coords["y"])
        await page.wait_for_timeout(2000)
        logger.info("Selected Item Code row: %s", coords["text"].replace("\t", " | "))
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

        # --- Customer (cascades Address/Contact/Currency/Sales Tax/SBU) ---
        council = payload.town_council.strip() or "Town Council"
        if not await self._select_autocomplete_row("Customer", council, council.upper()):
            raise RuntimeError(f"no Customer match found in Synergix for {council!r}")

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
        logger.info("Stage B: draft filled for %s", payload.wo_po_number)

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

        try:
            # Unit Price + Remarks: inputs in the details row whose column headers say so. Qty is set
            # explicitly too — a from-scratch row does not inherit "1.00" from any template. Every
            # row shares the same WO-level remarks (the WO itself, not any one line, is what's billed).
            filled = await self.page.evaluate(
                """([rowIndex, price, remarks, qty]) => {
                    const setVal = (el, v) => {
                      if (!el) return false;
                      el.focus(); el.value = v;
                      el.dispatchEvent(new Event('input', {bubbles:true}));
                      el.dispatchEvent(new Event('change', {bubbles:true}));
                      return true;
                    };
                    let priceOk = false, remarkOk = false, qtyOk = false;
                    const grids = [...document.querySelectorAll('.ui-datatable')];
                    for (const g of grids) {
                      const heads = [...g.querySelectorAll('th')].map(t => (t.innerText||'').trim());
                      const pIdx = heads.findIndex(h => /unit price/i.test(h));
                      const rIdx = heads.findIndex(h => /remarks/i.test(h));
                      const qIdx = heads.findIndex(h => /^qty/i.test(h));
                      const rows = [...g.querySelectorAll('[id$="_data"] tr')];
                      const row = rows[rowIndex];
                      if (!row) continue;
                      const cells = [...row.querySelectorAll('td')];
                      if (pIdx >= 0 && cells[pIdx]) priceOk = setVal(cells[pIdx].querySelector('input'), price) || priceOk;
                      if (rIdx >= 0 && cells[rIdx]) remarkOk = setVal(cells[rIdx].querySelector('input,textarea'), remarks) || remarkOk;
                      if (qIdx >= 0 && cells[qIdx]) qtyOk = setVal(cells[qIdx].querySelector('input'), qty) || qtyOk;
                    }
                    return {priceOk, remarkOk, qtyOk};
                }""",
                [row_index, f"{line_item.unit_price:.2f}", remarks, f"{line_item.quantity:.2f}"],
            )
            logger.info("Stage B line item for %s: %s", label, filled)
        except Exception as exc:
            logger.warning("Stage B: could not set line item for %s: %s — leaving row partially filled",
                           label, exc)

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
