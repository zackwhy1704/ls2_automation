"""Synergix ERP driver: duplicate check + create/fulfil. DRY_RUN aware.

In DRY_RUN (the default) every step runs EXCEPT the final submit/confirm clicks, which are logged
instead of executed. Live submission only happens when DRY_RUN=false is set in .env.

Stages from the workflow doc:
  B â€” Create quotation: new quotation -> "Copy From" template -> fill ~8 fields -> submit
  C â€” Schedule board update: MOST FRAGILE. Best-effort; failure marks PARTIAL, not FAILED.
  D â€” Attach PDF + fulfil service order.

After any WO (success or fail), navigate back to a known home state before the next.
Browser access is serialised by the caller via an asyncio.Lock (writes run one at a time).
"""
from __future__ import annotations

import logging
import re
import time
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
    there after it has actually been invoiced in Synergix. Synergix â€” not JBTC â€” is the source of
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

# Schedule Board (Stage C) employee. Same fixed value already used as the Stage B Salesperson (see
# _stage_b_create_quotation) -- "TAN WEI YING" is the only person seen assigned on every real
# quotation/schedule observed so far, both councils. See that TODO(human) for the same open question
# (confirm with the client whether this should ever vary per-WO).
SCHEDULE_EMPLOYEE = "TAN WEI YING"

# Payment Method dropdown target. Confirmed live (2026-08-15) that the previously-targeted
# "Cheque" does not appear as an option at all for JALAN BESAR TOWN COUNCIL â€” the only real
# (non-placeholder) option offered was "GIRO", which the client confirmed is correct.
PAYMENT_METHOD = "GIRO"

# External Remarks picker target. Confirmed live (2026-08-17) that the "External Remarks" field's
# magnifying-glass search panel offers a fixed catalog of boilerplate remark codes (OCBC BANK
# DETAIL, several T&C variants); the client asked for OCBC BANK DETAIL specifically, matching the
# GIRO payment method.
EXTERNAL_REMARK_CODE = "OCBC BANK DETAIL"

# Project Site search term per council, used to find the right autocomplete row when creating a
# quotation from scratch (see _select_autocomplete_row). Searching by the bare numeric code is NOT
# safe â€” confirmed live that the same code string can match a DIFFERENT council's project (e.g.
# "2000050" matched a Jalan Besar project first, not Sengkang), so we search by council name instead
# and let the resolve_project_code()-derived code disambiguate which of the matches to pick.
_PROJECT_SITE_SEARCH_JBTC = "Jalan Besar"
_PROJECT_SITE_SEARCH_SKTC = "Sengkang"

# TODO(human): Sengkang's real Project Site options (confirmed live, 2026-08-03) are
# "2000073-Sengkang Town Council (Pest control)" and "2000130-Sengkang Town Council (Mosquito)" â€”
# NOT the 2000050/2000069 Ecocare/Infigo codes resolve_project_code() computes from the job-sheet
# prefix (those are confirmed JBTC-only, per the same live session). Since every SKTC sample we have
# is adhoc PEST CONTROL work (not mosquito-specific), this defaults every SKTC WO to the "Pest
# control" project site (2000073) as the reasonable assumption â€” CONFIRM with the client whether any
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
        """True when Synergix is not configured yet â€” driver runs without a browser."""
        return not synergix_configured()

    async def start(self) -> None:
        if self.stubbed:
            logger.warning(
                "Synergix not configured (SYNERGIX_BASE_URL empty) â€” running in STUB mode: "
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
            # Schedule Board (Stage C) needs a wide viewport: confirmed live (2026-08-25) that at the
            # Playwright default (1280x720) the calendar's per-cell "add event" click silently fails
            # ("<td> intercepts pointer events") because the right-side info panel narrows the
            # calendar too much. Widening the viewport reproduces the same effect as collapsing that
            # panel by hand, without depending on that panel's own toggle control (which two separate
            # automated attempts -- plain and forced click -- both failed to actually trigger).
            viewport={"width": 1920, "height": 1080},
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
        # to the server via an async PrimeFaces ajax request â€” it is not proof the server has
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

    async def _goto_base_with_retry(self, *, attempts: int = 3) -> None:
        """Navigate to SYNERGIX_BASE_URL, retrying a transient navigation timeout.

        Synergix goes through short unreachable spells. Measured live (2026-08-21): three
        consecutive pipeline runs died on this one goto at the 30s page default, while a raw curl in
        the same minutes showed one 40s timeout followed by 200s in 1.7s, and a Playwright probe
        minutes later completed domcontentloaded in 0.6-1.4s on 6/6 attempts. So the server was
        briefly stalling, not broken, and a single attempt with no retry turned a few seconds of
        server wobble into a WO that did not get billed.

        That matters more than it looks: this navigation is on the dedup path for EVERY WO, and a
        failure there returns UNCERTAIN, which routes the WO to NEEDS_REVIEW. A nightly unattended
        run would quietly park work for a human instead of billing it, with nothing obviously broken
        in the report.
        """
        assert self.page is not None
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                # JSF/PrimeFaces app keeps connections open, so wait on DOM content, not networkidle.
                await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
                if attempt > 1:
                    logger.info("Synergix navigation succeeded on attempt %d", attempt)
                return
            except Exception as exc:
                last = exc
                logger.warning("Synergix navigation attempt %d/%d failed (%s) â€” retrying",
                                attempt, attempts, type(exc).__name__)
                try:
                    await self.page.wait_for_timeout(5000)
                except Exception:
                    pass
        raise RuntimeError(
            f"could not load {settings.SYNERGIX_BASE_URL} after {attempts} attempts: {last}"
        )

    async def login(self) -> None:
        if self._logged_in:
            return
        if not settings.SYNERGIX_BASE_URL:
            raise RuntimeError("SYNERGIX_BASE_URL is not set in .env")
        assert self.page is not None
        await self._goto_base_with_retry()
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
            # Persisted session already logged in â€” we landed straight on the app.
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
            logger.warning("Synergix session expired â€” re-logging in")
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

        Re-navigates from the base URL each time so the grid is a fresh, unfiltered instance â€” calling
        this twice in one session without the reset can leave a stale/filtered datatable. If the
        session has expired, re-login and retry once (self-healing).
        """
        await self.login()
        assert self.page is not None
        for attempt in (1, 2):
            # Retrying navigation here too: this existing loop only re-runs on an EXPIRED SESSION, so
            # a transient nav timeout would propagate straight out and (via check_duplicate) turn a
            # billable WO into NEEDS_REVIEW. See _goto_base_with_retry.
            await self._goto_base_with_retry()
            await self.page.wait_for_timeout(4000)
            if await self._is_session_expired():
                logger.warning("Session expired on nav (attempt %d) â€” re-logging in", attempt)
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

    async def _select_quotation_status_tab(self, tab_title: str) -> bool:
        """Switch the Service Quotation list to one of its status tabs, by the tab's title attribute.

        The screen's left rail is a set of status-filtered views â€” measured live (2026-08-20):
        Draft 446, Pending 0, Under Variation 73, History 5887, All 6406. "Draft" is the DEFAULT,
        so anything already submitted (status "Pending Confirmation") is invisible unless a tab is
        selected explicitly. That is not cosmetic: see check_duplicate, where it was a live
        double-billing hole.
        """
        assert self.page is not None
        tab = self.page.locator(f'[title="{tab_title}"]').first
        if not await tab.count():
            logger.warning("Quotation status tab %r not found", tab_title)
            return False
        try:
            await tab.click(timeout=10000)
            await self.page.wait_for_timeout(6000)
            # Wait for a VISIBLE Enquiry/Subject header, not just any. Each status tab has its own
            # copy of the grid, so after switching there are several in the DOM and only the active
            # tab's is visible â€” a plain wait_for_selector resolves to the first (the previous tab's,
            # now hidden) and times out, which is what broke the first version of this fix.
            await self.page.locator("th:visible", has_text="Enquiry/Subject").first.wait_for(
                state="visible", timeout=30000)
            await self.page.wait_for_timeout(2000)
            return True
        except Exception:
            logger.exception("Could not switch to the %r quotation tab", tab_title)
            return False

    async def check_duplicate(self, payload: WOPayload) -> DedupResult:
        """Is this WO already invoiced in Synergix? Returns a three-state, fail-safe result.

        The JBTC "Un-Invoiced WO" list is hand-maintained and can be stale, so Synergix is the source
        of truth. Any error, timeout, or ambiguous page yields UNCERTAIN (not NOT_DUPLICATE), so a WO
        is NEVER silently billed when we can't confirm it is unbilled â€” avoiding double invoicing.

        A confirmed NOT_DUPLICATE requires positive evidence of "no records" (the no-result marker),
        not merely the absence of result rows â€” otherwise a layout change would read as "safe to bill".

        Searches the "All" tab, NOT the list's default view. Confirmed live (2026-08-21) that the
        default is "Draft", and a SUBMITTED quotation moves to status "Pending Confirmation" which
        that view excludes â€” so this check was structurally blind to exactly the records it exists to
        catch. In the 2026-08-20 sweep both WO-PO/000080321 and WO-PO/000080420 were reported
        NOT_DUPLICATE despite already having verified submitted quotations (QUO0006664 at 209.00 and
        QUO0006668 at 88.00), and the drafts that produced would have double-billed both on submit.
        It only looked sound until now because nearly every historical quotation is still a draft:
        the hole opens the moment a WO is actually billed, which is the worst possible time.

        If the tab switch fails, this returns UNCERTAIN rather than falling back to the Draft view â€”
        a narrower search is precisely what caused the hole, so it must never be the silent default.
        """
        search_value = self._dedup_search_value(payload)

        if self.stubbed:
            if settings.DEDUP_STUB_ASSUME_SAFE:
                logger.warning("[STUB] DEDUP_STUB_ASSUME_SAFE=true â€” assuming %s NOT invoiced "
                               "(DEV ONLY; never use with real billing)", search_value)
                return DedupResult.NOT_DUPLICATE
            # No Synergix yet: we CANNOT verify invoiced status, so this is genuinely uncertain.
            logger.warning("[STUB] cannot verify invoiced status for %s (Synergix not configured) "
                           "-> UNCERTAIN (needs human review)", search_value)
            return DedupResult.UNCERTAIN

        try:
            await self._open_service_quotation_list()
            assert self.page is not None

            # Must cover submitted quotations too â€” see this method's docstring. No silent fallback.
            if not await self._select_quotation_status_tab("All"):
                logger.warning(
                    "Dedup %s: UNCERTAIN (could not switch to the 'All' quotation tab; refusing to "
                    "fall back to the default Draft view, which cannot see submitted quotations)",
                    search_value)
                return DedupResult.UNCERTAIN

            # Filter the Enquiry/Subject column by the WO-PO. This JSF grid's ids are auto-generated,
            # so target the column by its header text, then the stable filter class within it. The
            # PrimeFaces column filter applies on Enter.
            header = self.page.locator("th:visible", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill("")
            await filter_input.press("Enter")            # clear any prior filter first
            await self.page.wait_for_timeout(2000)
            await filter_input.fill(search_value)
            await filter_input.press("Enter")
            await self.page.wait_for_timeout(6000)        # PrimeFaces ajax re-filter + settle

            # Read the filtered grid body: does it contain the WO-PO, or the "No records found" row?
            # Scoped to the VISIBLE quotation table specifically. Each status tab has its own copy
            # (ids differ only by prefix, all ending serviceQuotationTable_data), so the visibility
            # filter is what picks the active tab's grid. Deliberately NOT a bare [id$="_data"]:
            # confirmed live (2026-08-21) that also matches the top notification bar
            # (topBarNotificationBarPanelForm:...:j_idt952_data), and folding unrelated text into the
            # haystack risks a false DUPLICATE â€” the one verdict that silently stops a WO being
            # billed at all.
            grid = await self.page.evaluate(
                """(wo) => {
                    const bodies = [...document.querySelectorAll('[id$="serviceQuotationTable_data"]')]
                        .filter(b => b.offsetParent !== null);
                    const txt = bodies.map(b => b.innerText || '').join('\\n');
                    return { present: bodies.length > 0,
                             empty: /no records found|no data/i.test(txt),
                             match: txt.includes(wo) };
                }""",
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
            # Filtered but neither a WO match nor the explicit empty marker â€” can't be sure. Fail safe.
            logger.warning("Dedup %s: UNCERTAIN (no WO match and no 'no records' marker)", search_value)
            return DedupResult.UNCERTAIN
        except Exception:
            logger.exception("Dedup check for %s errored â€” returning UNCERTAIN (fail-safe)", search_value)
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
                "Synergix stubbed â€” no write performed (SYNERGIX_* not configured). "
                f"project_code={project_code}",
            )
        try:
            await self.login()
            await self._stage_b_create_quotation(payload)
            try:
                await self._assert_details_filled(payload)
            except Exception:
                # Confirmed live (2026-08-19) on WO-PO/000080321 and WO-PO/000080420: when the
                # Details-grid fill fails this late (Customer/Salesperson/Project Site/Item Code
                # already set, only Qty/Unit Price never committing) and the WO-level 300s timeout
                # or this assertion cuts it off, the draft was left behind with NOTHING aborted â€”
                # _abort_blank_draft only covers a failure before Customer is set. That draft (e.g.
                # QUO0006683/QUO0006684, Total After Tax 0.00, blank Item Code) then permanently
                # masks the WO from every future run: check_duplicate sees a real quotation exists
                # and reports DUPLICATE forever, even though nothing was ever actually billed. This
                # is narrower than the general "later failures keep their draft for review" policy
                # (see _abort_blank_draft's docstring) â€” a draft that fails ITS OWN completeness
                # assertion has no review value by definition, unlike e.g. a Payment Method or
                # Customer Contact miss that still leaves a usable draft.
                draft_quo_id = await self._current_quotation_id()
                logger.warning(
                    "Details grid never became valid for %s â€” aborting draft %s rather than "
                    "leaving a broken record that would mask this WO from future dedup checks",
                    payload.wo_po_number, draft_quo_id)
                if draft_quo_id:
                    try:
                        await self.abort_quotation(draft_quo_id)
                    except Exception:
                        logger.exception("Could not abort incomplete draft %s for %s â€” may need "
                                         "manual cleanup", draft_quo_id, payload.wo_po_number)
                raise
            quo_id = await self._submit_quotation(payload)
            if settings.DRY_RUN:
                # Schedule Board (C) and Fulfil (D) remain manual â€” done by the team in Synergix.
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"DRY_RUN: quotation draft {quo_id or '(id unread)'} created + filled, NOT submitted.",
                )
            # Stage B.5: a submitted quotation sits in "Under Variation" and is NOT yet a schedulable
            # Service Order until this is confirmed -- see _confirm_variation_order's docstring.
            # Best-effort: a failure here still leaves a real, submitted quotation (just requires a
            # human to confirm the VO manually), not a lost WO.
            vo_confirmed = False
            if quo_id:
                try:
                    vo_confirmed = await self._confirm_variation_order(quo_id)
                except Exception:
                    logger.exception("Variation Order confirm failed for %s (quotation %s)",
                                      payload.wo_po_number, quo_id)
            if not vo_confirmed:
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"Quotation {quo_id or '(id unread)'} created + submitted, but Variation Order "
                    "confirm failed or was skipped -- human must confirm it in Synergix before it "
                    "becomes a schedulable Service Order. Schedule board + fulfil still manual.",
                )
            # Stage C: schedule the new Service Order (assign SCHEDULE_EMPLOYEE at the WO's job
            # date) and submit it -- see _schedule_stage_c's docstring. Best-effort, same reasoning
            # as Stage B.5: a failure here still leaves a real, confirmed quotation + Service Order,
            # just requiring a human to finish scheduling manually in Synergix.
            scheduled = False
            try:
                scheduled = await self._schedule_stage_c(payload)
            except Exception:
                logger.exception("Stage C scheduling failed for %s (quotation %s)",
                                  payload.wo_po_number, quo_id)
            if not scheduled:
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"Quotation {quo_id or '(id unread)'} created, submitted, and Variation Order "
                    "confirmed, but Stage C (Schedule Board) scheduling failed or was skipped -- "
                    "human must schedule it manually in Synergix. Fulfil still manual.",
                )
            return WriteResult(
                WOStatus.PROCESSED,
                f"Quotation {quo_id or '(id unread)'} created, submitted, Variation Order confirmed, "
                "and Schedule Board (Stage C) completed. Fulfil (Stage D) still manual.",
            )
        except S.MissingSelectorError as exc:
            logger.error("MISSING SELECTOR: %s â€” fill it in config/selectors.py", exc)
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

        Added 2026-08-17 after an independent audit found QUO0006650 â€” a fresh blank shell dated
        that same day, created by exactly this failure mode (a Customer-selection crash right after
        the "+" click, from a stuck blockUI overlay). The write() pipeline's normal except handler
        just logs and returns FAILED with no cleanup step, which is how the ORIGINAL 151-empty-
        quotation incident this whole investigation started from actually happened â€” this closes
        that gap for the specific case where NOTHING useful was captured yet.

        Deliberately only called this early. A failure after Customer/Salesperson/Details are
        already set still leaves a (possibly incomplete) draft that has real review value â€” e.g.
        the 54 partial drafts from tonight's runs â€” and must NOT be aborted just because one later
        step failed.

        Confirms the target is ACTUALLY blank before aborting it, rather than trusting the id it was
        handed. Confirmed live (2026-08-20 overnight sweep) that a stale id can reach here and
        destroy a different WO's fully-filled, gate-verified draft: WO-PO/000081588's failure
        aborted QUO0006710, which belonged to WO-PO/000080420. The caller now also refuses to pass
        an id matching the pre-click one, but this is the guard that holds regardless of how the id
        was obtained â€” same safety boundary reap_incomplete_draft already applies ("never touch
        anything that looks legitimately filled").
        """
        if not quo_id:
            logger.warning(
                "Stage B failed before Customer was set for %s, and no draft id could be read to "
                "clean up â€” a blank shell may have been left behind; check Synergix manually",
                wo_po_number)
            return
        try:
            # A genuine blank shell has no Subject and no positive total. Anything else is somebody
            # else's record (or a partially useful one) and must survive.
            subject = (await self._read_labeled_value("Enquiry/Subject") or "").strip()
            total = await self._read_total_after_tax()
            if subject or (total is not None and total > 0):
                logger.error(
                    "REFUSING to abort %s for %s: it is not a blank shell (subject=%r, total=%r). "
                    "This id is almost certainly stale and belongs to another WO â€” leaving it "
                    "untouched. Any real blank shell from this failure needs manual cleanup.",
                    quo_id, wo_po_number, subject, total)
                return
            logger.warning("Aborting blank draft %s for %s (failed before Customer was set)",
                            quo_id, wo_po_number)
            await self.abort_quotation(quo_id)
        except Exception:
            logger.exception("Could not abort blank draft %s for %s â€” may need manual cleanup",
                              quo_id, wo_po_number)

    async def abort_quotation(self, quotation_no: str) -> bool:
        """Admin/cleanup utility, NOT part of the regular write pipeline: open an existing quotation
        by its number and abort (discard) it â€” for removing bad/orphaned drafts, e.g. the batch of
        empty quotations a full SOP compliance audit found from before the Details-grid fill was
        fixed. Only works on an un-submitted draft (Revision 0) â€” Abort is Synergix's own action for
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
            logger.warning("abort_quotation: no Abort button for %s â€” may already be submitted "
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

    async def reap_incomplete_draft(self, wo_po_number: str) -> bool:
        """Find whatever quotation Synergix now has for `wo_po_number` and abort it, but ONLY if
        it's genuinely incomplete (Total After Tax not positive) â€” never touches a quotation that
        looks legitimately filled, submitted or not.

        For a WO whose write() call was cut off by the batch-level SYNERGIX_WO_TIMEOUT_S guardrail
        (asyncio.wait_for cancels the coroutine â€” write()'s own except block never runs, so its
        draft-abort-on-Details-failure handling can't fire either). Confirmed live (2026-08-19) on
        WO-PO/000080321 and WO-PO/000080420: both timed out deep in the Details-grid retry loop and
        left behind a draft (QUO0006683: empty grid; QUO0006684: Item Code/Qty/Unit Price all
        blank, Total After Tax 0.00) that then permanently masks the WO â€” check_duplicate finds a
        real quotation and reports DUPLICATE forever, even though nothing was ever actually billed.

        Not part of the regular write() path â€” call this from the batch loop's timeout handler,
        same admin-utility spirit as abort_quotation.
        """
        assert self.page is not None
        page = self.page
        try:
            await self._open_service_quotation_list()
            header = page.locator("th", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill("")
            await filter_input.press("Enter")
            await page.wait_for_timeout(2000)
            await filter_input.fill(wo_po_number)
            await filter_input.press("Enter")
            await page.wait_for_timeout(6000)

            row = page.locator(f"tr:has-text('{wo_po_number}')").first
            quo_link = row.locator("a").first
            if not await quo_link.count():
                logger.info("reap_incomplete_draft: no quotation found for %s â€” nothing to reap",
                            wo_po_number)
                return False
            quo_id = (await quo_link.inner_text()).strip()
            await quo_link.click(timeout=10000)
            await page.wait_for_timeout(5000)

            total = await self._read_total_after_tax()
            if total is not None and total > 0:
                logger.info("reap_incomplete_draft: %s (for %s) has Total After Tax %.2f â€” looks "
                            "legitimate, leaving it alone", quo_id, wo_po_number, total)
                return False

            logger.warning("reap_incomplete_draft: %s (for %s) has Total After Tax %r â€” aborting "
                            "as an incomplete draft left behind by a timed-out write",
                            quo_id, wo_po_number, total)
            return await self.abort_quotation(quo_id)
        except Exception:
            logger.exception("reap_incomplete_draft: error while checking %s â€” leaving as-is",
                              wo_po_number)
            return False

    async def _read_labeled_value(self, label: str) -> str:
        """Read the current value of a labeled form field (see _fill_labeled_input) without
        changing it. Unlike _fill_labeled_input, does not exclude [readonly] â€” reading should work
        regardless of the field's edit state.

        Falls back to the label's own parent container if it has no `<tr>` ancestor. Confirmed live
        (2026-08-17) that "External Remarks" uses a div-based grid layout (label + content as
        sibling divs under a shared `grid-item-column` parent), not a table row â€” `closest('tr')`
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
        incomplete quotation (Customer/Subject/GL/Project Site already correct, Details grid empty â€”
        the shape every quotation from before the 2026-08-14 fill fix came out in) and fill in
        whatever's missing, reusing the same fixed logic _stage_b_create_quotation uses. Does NOT
        touch Customer/Salesperson/Project Site (already correct) or Subject/Reference No. â€” only
        adds Details rows, sets Payment Method, and fills Project Site if it was left blank.

        `payload` must already have the correct wo_po_number/line_items/job_sheet_number/etc. for
        this quotation (the caller is expected to have matched it up, e.g. via the Subject field).
        Leaves the quotation as an un-submitted draft either way â€” never submits.

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

        # Customer Contact: map to the WO's real Property officer, overriding the generic "Account
        # Department" cascaded default. Client explicitly decided (2026-08-17, after a post-
        # remediation audit flagged this on all 62 live quotations) to map the officer here rather
        # than accept the default. Must run BEFORE Payment Method for the same tab-hiding reason as
        # Project Site below. Skip if it already shows the right officer (idempotent re-run).
        if payload.property_officer:
            current_contact = await self._read_labeled_value("Customer Contact")
            if payload.property_officer.strip().upper() not in current_contact.strip().upper():
                if not await self._try_set_customer_contact(payload.property_officer):
                    logger.warning(
                        "amend_quotation: no registered Customer Contact matches %r for %s â€” left as %r",
                        payload.property_officer, quotation_no, current_contact)

        # Project Site, only if currently blank (don't disturb an already-correct value). Must run
        # BEFORE Payment Method: confirmed live (2026-08-17) that Payment Method's tab-activation
        # (see _ensure_tab_active) switches away from the "General" tab Project Site lives on and
        # never switches back, so filling Payment Method first leaves Project Site's own input
        # genuinely not visible â€” a Locator.click on it then times out for the full 30s rather than
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

        # Details rows: add rows only up to the target count â€” idempotent, so re-running this on a
        # quotation from a prior partially-failed attempt doesn't pile on duplicate rows.
        line_items = payload.effective_line_items
        remarks = build_remarks(payload)
        fill_started_at = time.monotonic()
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
            await self._force_totals_commit(payload, line_items[:min(added, len(line_items))])

            await self._ensure_remarks_intact(payload, line_items[:min(added, len(line_items))], remarks)
        # Monitoring only â€” see the matching log line in _stage_b_create_quotation for why.
        logger.info("amend_quotation: Details-grid fill took %.1fs for %s",
                     time.monotonic() - fill_started_at, quotation_no)

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

    async def _click_when_clear(self, locator, *, timeout_ms: int = 10000, overlay_wait_ms: int = 30000) -> None:
        """Click `locator`, but first wait out any active PrimeFaces `blockUI` overlay.

        Added 2026-08-17 after a live run hit a 30s Locator.click timeout on the Customer field
        with the call log showing `<div class="blockUI blockOverlay"></div> intercepts pointer
        events` on every retry â€” the exact failure mode the client's own audit already identified
        as the root cause of the original 151-empty-quotation incident (see
        _abort_blank_draft's docstring). That incident's fix (_abort_blank_draft) cleans up the
        orphaned draft AFTER the failure; this addresses the failure itself by waiting for the
        overlay PrimeFaces shows during its own ajax calls to detach before attempting the click,
        instead of fighting it via Playwright's built-in click retries (which give up at the
        locator's own timeout regardless of overlay state).

        overlay_wait_ms widened from 8s to 30s (2026-08-18) after a live batch run hit this exact
        failure again on the Customer field right after quotation creation, with the overlay still
        blocking after the full original 8s+10s (18s) budget. Measured live immediately after: 5
        fresh back-to-back draft creations in the same session all cleared in 2.2-2.9s â€” the slow
        case is a genuine occasional server-side spike on an operation that reliably does complete,
        not a permanently-stuck state (unlike the unrelated TCMS grid-scoping bug this was once
        confused with) â€” so more patience here is the right fix, not a guess.

        Bounded: if the overlay never clears within overlay_wait_ms, proceeds to the click attempt
        anyway (the ordinary Locator timeout/error still applies) rather than hanging indefinitely
        â€” a genuinely stuck overlay must still surface as a clear failure, not a silent hang. The
        existing _abort_blank_draft safety net still cleans up if it does.
        """
        assert self.page is not None
        page = self.page
        try:
            await page.locator(".blockUI.blockOverlay").first.wait_for(
                state="detached", timeout=overlay_wait_ms)
        except Exception:
            pass  # no overlay was showing, or it didn't clear in time â€” either way, try the click
        await locator.click(timeout=timeout_ms)

    async def _fill_labeled_input(self, label: str, value: str, *, timeout_ms: int = 4000) -> None:
        """Fill the input/textarea belonging to a form field identified by its on-screen label.

        Synergix's JSF ids are auto-generated, so we anchor on the (stable) label text and take the
        input in the same table row. Raises if the field can't be found â€” the caller marks FAILED.

        Returns a Locator built from the input's own `id` â€” NOT an ElementHandle from
        `evaluate_handle()`. Confirmed live (2026-08-15) that an ElementHandle is a frozen reference
        to one specific DOM node: Customer selection cascades an ajax update (Address/Contact/
        Currency/Sales Tax/SBU all re-render), and if that cascade replaces this field's node between
        grabbing the handle and clicking it, the click raises "Element is not attached to the DOM" â€”
        confirmed live on the Customer Contact field, which is filled right after Customer's cascade.
        A Locator re-resolves the id at click time instead of clicking a stale reference, and its
        ids are stable across a PrimeFaces re-render even though the DOM node object is replaced.

        Polls for the host+input to appear (up to timeout_ms) instead of a single check. Confirmed
        live (2026-08-17, per a client SOP review) that Customer Contact was failing with "could not
        locate the input" on effectively every WO all night: it's filled immediately after Customer's
        own cascade, and a one-shot check right after that cascade starts can run before the
        cascade-rendered row exists yet â€” the same ajax-timing class of bug already fixed elsewhere
        in this file (_click_panel_row_by_text, _select_dropdown_option's option match).
        """
        assert self.page is not None
        page = self.page
        # Scoped to .synfaces-grid-item first, falling back to the classic <tr>: confirmed live
        # (2026-08-25) that the Schedule Board Event Details popup uses the div-based
        # "synfaces-grid-label"/"synfaces-grid-item" layout (same family as External Remarks, see
        # _select_external_remark) for From/To/Remarks, which has no <tr> ancestor at all -- the
        # original <tr>-only lookup would raise "could not locate the input" for every field in that
        # popup. Additive: every existing <tr>-based caller (Enquiry/Subject, Reference No.) still
        # matches via the fallback, unchanged.
        js = """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return null;
                const scope = host.closest('.synfaces-grid-item') || host.closest('tr');
                const input = scope && scope.querySelector('input:not([type=hidden]):not([readonly]), textarea');
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
        await self._click_when_clear(field)
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
        on empty cell space next to the link and left the textarea untouched, with no error â€” the
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
            await self._click_when_clear(page.locator(f'[id="{button_id}"]'))
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
            await self._click_when_clear(page.locator(f'[{marker}="1"]'))
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
        live on a SEPARATE tab (icon-only header, no visible text â€” matched by position among
        sibling tabs, not label) from the "General" tab that's active when a quotation draft is
        first created. Every earlier fix attempt at "could not set Payment Method" (coordinate
        click, then marker-Locator click, then polling) missed this because the trigger really was
        `display: none` the whole time â€” not a timing race or wrong-element click, a genuinely
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
            await self._click_when_clear(page.locator(f'[{marker}="1"]'), timeout_ms=5000)
        except Exception as exc:
            logger.warning("Could not activate the tab containing %s: %s", element_id, exc)
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        await page.wait_for_timeout(800)

    async def _select_dropdown_option(self, label: str, option_text: str) -> bool:
        """Select an option from a plain PrimeFaces `ui-selectonemenu` dropdown by its label.

        Unlike Customer/Salesperson/Project Site (live-search autocompletes â€” _select_autocomplete_row)
        this is a closed, fixed-option dropdown (role=combobox, aria-haspopup=listbox): click the
        trigger to open its panel, then click the matching `<li>` by text.

        The option `<li>` is clicked via a real Playwright Locator built from a marker attribute
        stamped onto that exact element, polling for the panel to render â€” NOT a raw
        `page.mouse.click(x, y)` at a computed bounding-box center. Confirmed live (2026-08-15) that
        this dropdown (used for Payment Method) was failing on almost every WO in a full batch run
        with "could not set Payment Method"; the coordinate click is the same silent-failure class
        documented on _grid_cell_locator and _click_panel_row_by_text.

        Also activates the field's tab first if it's hidden â€” see _ensure_tab_active's docstring
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
            await self._click_when_clear(page.locator(f'[id="{trigger_id}"]'))
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
            await self._click_when_clear(page.locator(f'[{marker}="1"]'))
        except Exception:
            return False
        finally:
            await page.evaluate(
                "(m) => document.querySelectorAll(`[${m}]`).forEach(e => e.removeAttribute(m))", marker
            )
        await page.wait_for_timeout(500)
        return True

    async def _select_autocomplete_row(
        self, label: str, search_text: str, must_contain: str, *, timeout_ms: int = 8000,
        extra_words: list[str] | None = None,
    ) -> bool:
        """Click a live-autocomplete field by its label, type search_text, and click the first
        visible dropdown row whose text contains must_contain.

        Synergix's Customer/Salesperson/Project Site fields are all the same PrimeFaces pattern: a
        plain input that, once focused and typed into, ajax-populates a floating panel of matching
        rows (NOT a modal). Confirmed live (2026-08-01/03) that clicking via generic text-locator
        matching is unreliable â€” with many near-identical rows, Playwright's own visibility check on
        the matched text node intermittently reports it as hidden even though it's visibly on screen.

        The row is located via JS (by text match within the visible panel), then clicked through a
        real Playwright Locator built from a marker attribute stamped onto that exact element â€” NOT
        raw `page.mouse.click(x, y)` at its computed bounding-box coordinates. Confirmed live
        (2026-08-14) that coordinate clicks in this app can silently land on an unrelated overlapping
        element (`document.elementFromPoint()` at the computed point returned a different container
        entirely) with no error and no visible symptom besides the selection never taking â€” the exact
        bug that left the Details grid empty on 151 production quotations; see _grid_cell_locator's
        docstring for the full story. A stamped-attribute Locator gets Playwright's own actionability
        checks (auto-scroll, and a real thrown error if something intercepts the click).

        The FIELD's own input is now located the same way (host label -> its <tr> -> the real
        <input>), not the offset-coordinate click used here previously. Confirmed live (2026-08-15,
        the full-batch rerun) that the previous `page.mouse.click(box.x + box.width + 80, box.y + 8)`
        guess intermittently missed the actual input â€” same silent-failure shape as the grid-cell bug
        â€” showing up as flaky "no Customer match found" / "could not select a Salesperson" despite
        the exact same council/name working moments earlier or later in the same run.

        The input is a Locator built from its own `id`, NOT an ElementHandle from
        `evaluate_handle()`. Confirmed live (2026-08-15, immediately after the fix above) that an
        ElementHandle is a frozen reference to one specific DOM node â€” Customer's own selection
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
        await self._click_when_clear(field_input)
        await page.wait_for_timeout(300)
        # Clear any existing text before typing â€” confirmed live (2026-08-17) that calling this
        # twice on the same field (e.g. a failed search followed by restoring the original value)
        # otherwise inserts the new text into the middle of whatever was already there instead of
        # replacing it, since a plain click() only focuses the field without selecting its content.
        await page.keyboard.press("Control+A")
        await page.keyboard.type(search_text)

        match_text = await self._click_panel_row_by_text(
            must_contain, timeout_ms=timeout_ms, extra_needles=extra_words)
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
        CLOSED list of contacts registered against the customer in Synergix â€” not a free-text field.
        Blindly `.fill()`-ing the visible input (the previous approach) would show the officer's
        name in the box while the hidden field silently kept pointing at the old contact â€” a
        display/data mismatch, not a fix. So: try the real search-and-select; if nothing matches,
        re-search-and-reselect the ORIGINAL value to restore a consistent state instead of leaving
        whatever text the failed search typed in.

        Search by the officer's FIRST word only, not the full name. Confirmed live (2026-08-17) that
        Synergix's ajax search appears to filter on a single name field: searching "NURUL" (one
        word) returned the real contact row ("BUYER35 | Nurul Hasanah"), but searching the full
        "NURUL HASANAH" returned zero rows for that contact at all â€” a multi-word query the server
        itself can't satisfy, not a client-side matching problem. The remaining word(s) are instead
        required (case-insensitively) to also appear in whichever row the first word turns up, so a
        first-name collision with an unrelated contact doesn't get picked by mistake.
        """
        original = await self._read_labeled_value("Customer Contact")
        words = [w for w in name.split() if w]
        search_term = words[0] if words else name
        extra_words = words[1:]
        if await self._select_autocomplete_row(
            "Customer Contact", search_term, search_term, timeout_ms=4000, extra_words=extra_words
        ):
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
        self, needle: str, *, exclude_grids_with_headers: bool = False, timeout_ms: int = 6000,
        extra_needles: list[str] | None = None,
    ) -> str | None:
        """Find a visible autocomplete-panel row containing `needle` (and, if given, every one of
        `extra_needles` too) and click it, returning its text (or None if no match). Clicks via a
        real Playwright Locator built from a marker attribute stamped onto the matched row â€” NOT raw
        `page.mouse.click(x, y)` at its computed bounding-box coordinates. Confirmed live
        (2026-08-14) that coordinate clicks in this app can silently land on an unrelated overlapping
        element (`document.elementFromPoint()` at the computed point returned a different container
        entirely) with no error and no visible symptom besides the selection never taking â€” the exact
        bug that left the Details grid empty on 151 production quotations; see _grid_cell_locator's
        docstring for the full story.

        Polls for the match to appear (up to timeout_ms) instead of trusting a single check right
        after a fixed sleep. Confirmed live (2026-08-15, the full-batch rerun) that the ajax panel's
        populate time varies with server load â€” a one-shot check after a fixed wait intermittently
        ran before the panel had rendered, which read as "no match" even though the same search
        would have succeeded a second or two later.

        Match is case-insensitive. Confirmed live (2026-08-17, Customer Contact mapping) that a
        case-sensitive `.includes()` silently missed a real, currently-displayed match â€” the panel
        row read "Nurul Hasanah" (Synergix's own Title Case) while the caller's needle was the
        TCMS-scraped ALL-CAPS "NURUL HASANAH", so the exact same bug shape (looks empty, isn't) that
        _read_labeled_value already hit for External Remarks.
        """
        assert self.page is not None
        page = self.page
        marker = "data-claude-panel-target"
        js = """([needle, marker, excludeHeaders, extraNeedles]) => {
                let panels = [...document.querySelectorAll('.ui-datatable, [id$="_panel"]')]
                    .filter(p => p.offsetParent !== null);
                if (excludeHeaders) panels = panels.filter(p => !p.querySelector('th'));
                const needleUp = needle.toUpperCase();
                const extraUp = (extraNeedles || []).map(e => e.toUpperCase());
                for (const panel of panels.slice().reverse()) {
                    const rows = [...panel.querySelectorAll('tbody tr')];
                    const match = rows.find(r => {
                        const t = r.innerText.toUpperCase();
                        return t.includes(needleUp) && extraUp.every(e => t.includes(e));
                    });
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
            match_text = await page.evaluate(
                js, [needle, marker, exclude_grids_with_headers, extra_needles])
            if match_text:
                break
            await page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if not match_text:
            return None
        row = page.locator(f'[{marker}="1"]')
        try:
            await self._click_when_clear(row)
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

        Returns a Playwright Locator built from the input's own `id` attribute â€” NOT raw pixel
        coordinates. Confirmed live (2026-08-14) that `getBoundingClientRect()`-based coordinates for
        this grid do not correspond to the actual clickable point: `document.elementFromPoint()` at
        those exact coordinates returned an unrelated `.ui-tabs-panel` container, not the input. A
        `page.mouse.click(x, y)` at such coordinates clicks whatever is really there â€” silently, no
        error â€” which is why every previous fill attempt (both synthetic-event and real-keyboard
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
        field like Customer/Salesperson/Project Site â€” _select_autocomplete_row's label-based click
        doesn't apply here) and click the matching autocomplete row, same panel-locate pattern.

        `row_index` selects which Details row to fill when the WO has more than one line item â€” each
        "Add Row" click appends a new row, so index 0 is the first-added row, 1 the second, etc.

        This cell is a PrimeFaces `<p:autoComplete>` (`role="application"`), not a plain input â€”
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
        await self._click_when_clear(cell)
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
        # be picked as "the last visible panel" â€” exclude it explicitly (it's the only candidate with
        # column headers) and only consider panels whose rows actually contain item_code as a match.
        # Polls (see _click_panel_row_by_text) rather than a single fixed-wait check â€” confirmed live
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
        blocked EVERY SKTC WO â€” see project memory synergix-dedup-verified). Building from scratch has
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

        # Read whatever id is on screen BEFORE creating, so a stale one can be recognised below.
        pre_click_quo_id = await self._current_quotation_id()

        # The "+" click does not always open the new-quotation form, and EVERYTHING after this point
        # assumes it did. Confirmed live (2026-08-21) on WO-PO/000080322, reproducibly across two
        # runs: the click left the page on the LIST, and then
        #   - _current_quotation_id() returned the previous record's id (which is how the
        #     2026-08-20 sweep aborted QUO0006710, a fully-filled draft belonging to a different WO,
        #     while orphaning its own blank shell QUO0006711), and
        #   - _select_autocomplete_row("Customer", ...) matched the LIST's "Customer" COLUMN FILTER
        #     (id ...serviceQuotationTable:...:filter) instead of the form's Customer field, then
        #     timed out clicking it â€” the failure that read as a "blockUI flake" for days.
        # So: confirm a NEW draft id actually appeared before proceeding, retry the click if not, and
        # fail with a diagnosable error rather than acting on the wrong page.
        draft_quo_id = None
        for attempt in range(1, 4):
            await page.locator("button:has(span.fa-plus)").first.click()
            for _ in range(16):  # up to ~8s for the form (and its new id) to render
                await page.wait_for_timeout(500)
                candidate = await self._current_quotation_id()
                if candidate and candidate != pre_click_quo_id:
                    draft_quo_id = candidate
                    break
            if draft_quo_id:
                if attempt > 1:
                    logger.info("Stage B: new draft %s opened on '+' attempt %d for %s",
                                draft_quo_id, attempt, payload.wo_po_number)
                break
            logger.warning(
                "Stage B: the '+' click did not open a new quotation form for %s (still showing %r) "
                "â€” retrying (attempt %d/3)",
                payload.wo_po_number, pre_click_quo_id, attempt)
            await self._open_service_quotation_list()
        if not draft_quo_id:
            raise RuntimeError(
                f"the New-quotation form never opened for {payload.wo_po_number} after 3 '+' clicks "
                f"(page still showing {pre_click_quo_id!r}). Refusing to continue: every later step "
                "would target the quotation LIST instead of the form â€” which is how a previous run "
                "aborted another WO's draft and how the 'Customer field' click timeouts arose.")
        await page.wait_for_timeout(3000)  # let the freshly-opened form settle before filling

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
        # the WO, if TCMS scraping captured one (JBTC/TCMS flow only â€” unset for SKTC/email WOs,
        # which have no TCMS page to scrape it from). Best-effort: a full SOP audit found this stuck
        # on a generic "Account Department" default on every quotation (MAJOR finding 4.3).
        if payload.property_officer:
            if not await self._try_set_customer_contact(payload.property_officer):
                logger.warning(
                    "Stage B: no registered Customer Contact matches %r for %s â€” left at the "
                    "cascaded default", payload.property_officer, payload.wo_po_number)

        # --- Salesperson ---
        # TODO(human): "TAN WEI YING" is the salesperson seen on every real quotation observed so
        # far (both councils), suggesting it's a fixed default rather than per-WO â€” confirm with the
        # client whether this should ever vary.
        salesperson_ok = await self._select_autocomplete_row("Salesperson", "Tan Wei", "TAN WEI YING")
        if not salesperson_ok:
            logger.warning("Stage B: could not select a Salesperson for %s â€” leaving blank",
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
                "Stage B: no Project Site match for %s (searched %r, expected %r) â€” leaving blank, "
                "human must set it before Submit", payload.wo_po_number, search_term, match_fragment)

        # --- Subject + Reference No. ---
        await self._fill_labeled_input("Enquiry/Subject", self._subject(payload))
        await self._fill_labeled_input("Reference No.", payload.gl_number)
        logger.info("Stage B: filled Subject + Reference No. for %s", payload.wo_po_number)

        # --- External Remarks (before Payment Method â€” same tab-hiding reason as Project Site) ---
        if not await self._select_external_remark(EXTERNAL_REMARK_CODE):
            logger.warning("Stage B: no External Remarks match for %r on %s",
                            EXTERNAL_REMARK_CODE, payload.wo_po_number)

        # --- Payment Method (left at the "Sel" placeholder if the target option isn't found) ---
        if not await self._select_dropdown_option("Payment Method", PAYMENT_METHOD):
            logger.warning("Stage B: could not set Payment Method for %s â€” leaving as placeholder",
                            payload.wo_po_number)

        # --- Line items: add one Details row per WO line item, then fill each ---
        # A WO with N "Job Sheet:" rows in its Description of Work table needs N Synergix rows, or
        # the quotation silently bills only the first line â€” see WOPayload.line_items's docstring.
        line_items = payload.effective_line_items
        remarks = build_remarks(payload)
        fill_started_at = time.monotonic()
        added = 0
        for _ in line_items:
            if not await self._add_line_item():
                break
            added += 1
        if added < len(line_items):
            logger.warning(
                "Stage B: could only add %d/%d Details line item row(s) for %s â€” human must add the "
                "rest before Submit", added, len(line_items), payload.wo_po_number)
        for i in range(added):
            await self._fill_line_item_row(i, line_items[i], remarks)
        if added:
            await self._verify_and_refill_rows(line_items[:added], remarks)
            # The rows can all read back correctly while the server is still short of the authorised
            # total â€” only the page total exposes that. See _force_totals_commit.
            await self._force_totals_commit(payload, line_items[:added])

            # The totals repair only touches Qty/Unit Price and stops when the total matches, which it can

            # do with Remarks wiped. See _ensure_remarks_intact.

            await self._ensure_remarks_intact(payload, line_items[:added], remarks)
        # Monitoring only: the flicker-tolerance fix (see _fill_grid_field) widened per-cell
        # patience from ~2.4s to ~15s to correctly ride out the grid's own re-render â€” worst case
        # that's real time added per field, not just per WO. Logging how long the whole Details
        # fill actually took makes a real slowdown visible in logs rather than only discovered when
        # a batch job runs unexpectedly long.
        logger.info("Stage B: draft filled for %s (Details-grid fill took %.1fs)",
                     payload.wo_po_number, time.monotonic() - fill_started_at)

    async def _assert_details_filled(self, payload: WOPayload) -> None:
        """Raise if any Details row is missing Item Code/Qty/Unit Price/Remarks, or Total After Tax
        is not positive. Called before Submit â€” never submit (or report success for) an incomplete
        quotation just because no exception happened to fire while filling it.

        Added 2026-08-14 after a full SOP compliance audit found that ALL 151 quotations from an
        earlier production run had silently empty Details rows (Item Code/Qty/Unit Price/Remarks all
        blank, Total 0.00) despite every fill step logging success â€” the fill mechanism reported
        {priceOk: true, ...} while the value never actually committed. This assertion is the fail-safe
        the audit itself recommended: catch that class of failure explicitly rather than trusting the
        fill code's own optimistic return values.
        """
        assert self.page is not None
        line_items = payload.effective_line_items
        problems: list[str] = []
        rows: list[dict] = []

        def _blank(v) -> bool:
            return not (v or "").strip()

        def _not_positive(v) -> bool:
            return float(v or 0) <= 0

        _FIELD_CHECKS = (
            ("item code", _blank, "Item Code is blank"),
            ("^qty", _not_positive, "Qty is {value!r}"),
            ("unit price", _not_positive, "Unit Price is {value!r}"),
            ("remarks", _blank, "Remarks is blank"),
        )
        for i in range(len(line_items)):
            row = await self._read_grid_row(i, ("item code", "^qty", "unit price", "remarks"))
            rows.append(row)
            for header_regex, is_bad, message_template in _FIELD_CHECKS:
                value = row.get(header_regex)
                if not is_bad(value):
                    continue
                # Re-check the SAME field once more after a short wait before treating it as a real
                # problem â€” confirmed live (2026-08-19) the grid can transiently blank/zero an
                # already-correct, untouched cell for several seconds during its own re-render. This
                # function is the last gate before a WO is trusted (added after the 151-empty-
                # quotation incident), so a false positive here wrongly routes a genuinely fine WO to
                # FAILED/review. Same defensive pattern as _verify_and_refill_rows's recheck.
                await self.page.wait_for_timeout(2000)
                recheck = await self._read_grid_row(i, (header_regex,))
                recheck_value = recheck.get(header_regex)
                if not is_bad(recheck_value):
                    logger.info(
                        "_assert_details_filled: row %d %s read %r once but %r on recheck â€” a "
                        "flicker, not a real problem, not reporting it", i, header_regex, value,
                        recheck_value)
                    row[header_regex] = recheck_value  # keep `rows` consistent for the reconciliation below
                    continue
                problems.append(f"row {i}: " + message_template.format(value=recheck_value))

        # Standing control added 2026-08-17 per a client SOP review of 54 live quotations: every
        # single one was under-billed by exactly the JBTC 10% SOR uplift because the grid was being
        # filled with the gross unit_price instead of the WO-authorised net figure (see
        # LineItem.billed_unit_price). Each row individually looked "filled" the whole time â€” only a
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
        # cell's own commit â€” confirmed live (2026-08-15) that every individual row can already read
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
            total_value = None if total_after_tax is None else float(total_after_tax.replace(",", ""))
        except ValueError:
            total_value = None
            problems.append(f"Total After Tax is unparseable: {total_after_tax!r}")

        if total_after_tax is not None and total_value is None:
            pass  # already reported as unparseable above
        elif total_value is None or total_value <= 0:
            problems.append(f"Total After Tax is {total_after_tax!r}")
        else:
            # A positive total is NOT sufficient: it must also equal what the WO authorises. Added
            # 2026-08-18 after finding that a row the server never committed (see
            # _force_totals_commit) leaves the OTHER rows' amounts summing to a positive but
            # understated total, which this gate used to accept. Concretely, WO-PO/000080321 (rows
            # 1x33.00 + 4x44.00 = 209.00) could submit at 176.00 â€” the qty-1 row silently missing â€”
            # because every per-row DOM check passed and the total was merely "> 0". That is the same
            # under-billing class this file's billed_unit_price fix exists to prevent, reached by a
            # different route, so it gets the same standing reconciliation.
            #
            # Which figure this field holds is not assumed: measured live at 44.00 for a single
            # 1 x 44.00 line (i.e. pre-GST there), but the label reads "Total After Tax", so both the
            # pre-GST net and the GST-inclusive grand total are accepted and the match is logged â€”
            # real runs will show which it actually is without risking a false rejection now.
            candidates = {"net_amount": payload.net_amount, "grand_total": payload.grand_total}
            known = {name: v for name, v in candidates.items() if v is not None}
            if known:
                matched = [name for name, v in known.items() if abs(total_value - v) <= 0.05]
                if matched:
                    logger.info("Total After Tax %.2f matches payload %s", total_value,
                                " and ".join(matched))
                else:
                    problems.append(
                        f"Total After Tax {total_value:.2f} matches neither the WO's net amount "
                        f"({payload.net_amount}) nor its grand total ({payload.grand_total}) â€” a row "
                        "the server never committed would look exactly like this"
                    )

        if problems:
            raise RuntimeError(
                f"Details grid incomplete for {payload.wo_po_number} â€” refusing to submit: "
                + "; ".join(problems)
            )

    async def _confirm_variation_order(self, quotation_no: str) -> bool:
        """Stage B.5: retrieve a just-submitted quotation from "Under Variation" and click Confirm.

        Discovered live (2026-08-25): a submitted quotation does NOT immediately become a
        schedulable Service Order. It lands in the "Under Variation" status tab first (this file's
        own docstrings already named this step -- "Go to Variation Order, retrieve the same service
        quotation, click it, and Confirm the VO" -- but it had never been automated, and Schedule
        Board's Unscheduled Service Orders grid has nothing to show without it). Confirmed live on
        QUO0006749: opening it from "Under Variation" and clicking its Confirm button (title="Confirm",
        icon fa-check-double) raises a "Are you sure?" dialog; clicking Yes produced
        "SA0005: Service Order No.: SV00008852 is created successfully." and the new Service Order
        immediately appeared in Schedule Board's Unscheduled Service Orders grid (count 44 -> 45).

        Without this step, Stage C automation has nothing to act on -- there is no Service Order to
        schedule. Returns whether the confirm was observed to succeed (the info-banner toast, or the
        quotation no longer appearing under "Under Variation" afterward).
        """
        assert self.page is not None
        page = self.page

        if _dry_guard(f"confirm Variation Order for {quotation_no}"):
            return True  # DRY_RUN: leave it sitting in Under Variation

        await self._open_service_quotation_list()
        if not await self._select_quotation_status_tab("Under Variation"):
            logger.warning("_confirm_variation_order: could not switch to 'Under Variation' tab "
                            "for %s", quotation_no)
            return False

        # Scope to the VISIBLE "Quotation No." header, not just the first in DOM order -- confirmed
        # live (2026-08-25) that each status tab keeps its own hidden/shown copy of the same grid
        # (the exact issue check_duplicate's docstring already documents), so a plain .first grabbed
        # the Draft tab's hidden filter input and every click on it timed out waiting for visibility.
        header = page.locator("th:visible", has_text="Quotation No.").first
        filter_input = header.locator("input.ui-column-filter").first
        await filter_input.click()
        await filter_input.fill(quotation_no)
        await filter_input.press("Enter")
        await page.wait_for_timeout(3000)

        link = page.get_by_role("link", name=quotation_no, exact=True).locator("visible=true")
        if not await link.count():
            # Confirmed live (2026-08-25): calling this twice on the same quotation (e.g. a retry
            # after a transient error elsewhere) is not a failure the second time -- it means the
            # first call's Confirm click already succeeded and the quotation has moved out of
            # "Under Variation" already. Treat "not found here" as already-done, not broken.
            logger.info("_confirm_variation_order: %s not under 'Under Variation' (already "
                        "confirmed, or never got this far) -- treating as already done",
                        quotation_no)
            return True
        await link.first.click(timeout=10000)
        await page.wait_for_timeout(4000)

        # The Confirm button is sometimes absent even on a fully-filled, otherwise-normal-looking
        # record -- confirmed live (2026-08-25) on two separate quotations (QUO0006761, QUO0006769),
        # comparing every visible field against a working record with no difference found. Root
        # cause unknown; reloading the record fresh a couple of times before giving up costs little
        # and has not yet been ruled out as fixing it (a longer in-place wait alone did NOT help).
        confirm_btn = page.locator('[title="Confirm"]').first
        for reload_attempt in range(3):
            if await confirm_btn.count():
                break
            if reload_attempt == 2:
                break
            logger.warning("_confirm_variation_order: no Confirm button on %s (attempt %d/3) -- "
                            "reloading and retrying", quotation_no, reload_attempt + 1)
            await self._open_service_quotation_list()
            if not await self._select_quotation_status_tab("Under Variation"):
                break
            await filter_input.click()
            await filter_input.fill(quotation_no)
            await filter_input.press("Enter")
            await page.wait_for_timeout(3000)
            link2 = page.get_by_role("link", name=quotation_no, exact=True).locator("visible=true")
            if not await link2.count():
                break
            await link2.first.click(timeout=10000)
            await page.wait_for_timeout(4000)
            confirm_btn = page.locator('[title="Confirm"]').first
        if not await confirm_btn.count():
            logger.warning("_confirm_variation_order: no Confirm button on %s after retries "
                            "(on-screen title: %r)", quotation_no, await page.title())
            await self._screenshot(f"vo_confirm_missing_button_{quotation_no}")
            return False
        await confirm_btn.click(timeout=10000)
        await page.wait_for_timeout(2000)
        yes_btn = page.get_by_role("button", name="Yes").locator("visible=true")
        if await yes_btn.count():
            await yes_btn.first.click(timeout=10000)
        else:
            logger.warning("_confirm_variation_order: no visible 'Yes' button after Confirm on %s",
                            quotation_no)
            await self._screenshot(f"vo_confirm_no_yes_button_{quotation_no}")
        await page.wait_for_timeout(6000)

        # Verify: the quotation should no longer be sitting under "Under Variation".
        await self._open_service_quotation_list()
        if not await self._select_quotation_status_tab("Under Variation"):
            logger.warning("_confirm_variation_order: could not re-open 'Under Variation' to "
                            "verify %s", quotation_no)
            return False
        header2 = page.locator("th:visible", has_text="Quotation No.").first
        filter_input2 = header2.locator("input.ui-column-filter").first
        await filter_input2.click()
        await filter_input2.fill(quotation_no)
        await filter_input2.press("Enter")
        await page.wait_for_timeout(3000)
        still_there = await page.get_by_text(quotation_no, exact=True).locator("visible=true").count() > 0
        if still_there:
            # Recheck once before declaring failure -- confirmed live (2026-08-25) that this list
            # can take longer than the wait above to drop a just-confirmed record, and re-opening the
            # list too early read a stale grid. Same defensive pattern as _verify_and_refill_rows.
            await page.wait_for_timeout(4000)
            await filter_input2.click()
            await filter_input2.fill(quotation_no)
            await filter_input2.press("Enter")
            await page.wait_for_timeout(3000)
            still_there = await page.get_by_text(quotation_no, exact=True).locator("visible=true").count() > 0
        if still_there:
            logger.warning("_confirm_variation_order: %s still under 'Under Variation' after "
                            "Confirm+Yes (confirmed on recheck)", quotation_no)
            await self._screenshot(f"vo_confirm_still_present_{quotation_no}")
            return False
        logger.info("Confirmed Variation Order for %s -- Service Order created", quotation_no)
        return True

    async def _open_schedule_board(self) -> None:
        """Navigate (logged in) to General Service -> Schedule Board - LS2.

        Same re-navigate-fresh pattern as _open_service_quotation_list, for the same reason (a
        stale/filtered grid left over from a previous visit).
        """
        await self.login()
        assert self.page is not None
        for attempt in (1, 2):
            await self._goto_base_with_retry()
            await self.page.wait_for_timeout(4000)
            if await self._is_session_expired():
                logger.warning("Session expired on nav (attempt %d) â€” re-logging in", attempt)
                self._logged_in = False
                await self.login()
                continue
            await self.page.get_by_text("General Service", exact=False).first.click()
            await self.page.wait_for_timeout(3000)
            await self.page.get_by_text("Schedule Board - LS2", exact=False).first.click()
            await self.page.wait_for_selector("th:has-text('Enquiry/Subject')", timeout=30000)
            await self.page.wait_for_timeout(4000)
            return
        raise RuntimeError("could not open Schedule Board after re-login")

    async def _schedule_stage_c(self, payload: WOPayload) -> bool:
        """Stage C: find the WO's Service Order on Schedule Board, assign SCHEDULE_EMPLOYEE at the
        WO's job date, and submit.

        Discovered live (2026-08-25): a confirmed Variation Order (Stage B.5) creates a Service Order
        that appears in Schedule Board's "Unscheduled Service Orders" grid, but scheduling it needs a
        specific sequence found only by watching the user do it by hand -- see docs/synergix_workflow.md
        ("Stage C completed end-to-end") for the full narrative. Summary of the non-obvious parts:
          1. The order's ROW must be selected first (click it) -- clicking the calendar with no order
             selected does nothing at all, no error, no popup.
          2. The Employee filter's checkboxes are the ONLY way to make an employee's calendar row
             appear; "Work Team" (a separate toggle) is a different, unrelated list.
          3. The actual click target for "add an event" is a fully-transparent overlay button
             (`[id*="newEventButton"]`), NOT the visible cell div underneath it -- the click failed
             with a "<td> intercepts pointer events" error until this was found, because Playwright
             was (correctly) reporting that invisible overlay as the interceptor.
          4. The Event Details popup's time sub-fields reset to 00:00 every time a DIFFERENT field in
             the same popup is edited (a whole-form ajax re-render) -- so times must be set LAST,
             immediately before submitting, or they get silently wiped by a later edit.
          5. After the popup's own checkmark, a SECOND, separate "Submit" action appears on the
             underlying Order Details panel -- both must be clicked; the popup's checkmark alone does
             not finish Stage C.

        Returns whether the schedule was confirmed to have taken (checked via the "Upcoming Service"
        panel showing a new dated entry -- the only signal confirmed live to actually reflect
        server-side persistence, not just a client-side form state).
        """
        assert self.page is not None
        page = self.page
        wo = payload.wo_po_number

        if _dry_guard(f"schedule Stage C for {wo}"):
            return True

        await self._open_schedule_board()

        council_search = _PROJECT_SITE_SEARCH_JBTC if is_jbtc(payload.town_council) else _PROJECT_SITE_SEARCH_SKTC
        header = page.locator("th:visible", has_text="Customer").first
        filter_input = header.locator("input.ui-column-filter").first
        await filter_input.click()
        await filter_input.fill(council_search)
        await filter_input.press("Enter")
        await page.wait_for_timeout(3000)

        # Match the order row by its Enquiry/Subject text containing the WO-PO number -- the same
        # text Stage B wrote into the quotation's Subject, carried through to the Service Order.
        order_row = page.locator("tr", has_text=wo.replace("WO-PO/", "")).locator("visible=true").first
        if not await order_row.count():
            logger.warning("Stage C: no Schedule Board order found for %s (searched customer %r)",
                            wo, council_search)
            return False
        order_no_cell = order_row.locator("td").nth(1)
        order_no = (await order_no_cell.inner_text()).strip()
        await order_row.click(timeout=10000)
        await page.wait_for_timeout(2000)

        # Employee view (NOT Work Team -- that list is empty; see docstring point 2). Confirmed live
        # (2026-08-26) that a Playwright Locator click here (both get_by_text(exact=True) and the
        # generic text= form) can report success while the toggle visibly stays on "Work Team" --
        # repeated across multiple fresh sessions, not a one-off. Clicking the underlying radio
        # input directly via JS .click() (bypassing Playwright's actionability + event simulation
        # entirely) is what the live discovery session actually used successfully; verify the button
        # went active (ui-state-active) afterward and retry if not.
        employee_active = False
        for _ in range(3):
            await page.evaluate(
                """() => {
                    const btn = [...document.querySelectorAll('div.ui-button')]
                      .find(b => b.textContent.trim() === 'Employee' &&
                                 b.getBoundingClientRect().width > 0);
                    if (btn) btn.click();
                }"""
            )
            await page.wait_for_timeout(1500)
            employee_active = await page.evaluate(
                """() => {
                    const btn = [...document.querySelectorAll('div.ui-button')]
                      .find(b => b.textContent.trim() === 'Employee' &&
                                 b.getBoundingClientRect().width > 0);
                    return btn ? btn.classList.contains('ui-state-active') : false;
                }"""
            )
            if employee_active:
                break
        if not employee_active:
            logger.warning("Stage C: could not switch to Employee view for %s", wo)
            await self._screenshot(f"stage_c_no_employee_toggle_{wo.replace('/', '-')}")
            return False
        filter_link = page.get_by_text("Filter", exact=True).locator("visible=true").first
        await filter_link.click(timeout=10000)
        await page.wait_for_timeout(2000)

        # Confirmed live (2026-08-26) that the checkbox list can still be genuinely empty in the DOM
        # several seconds after the Filter panel visibly opens -- a fixed 2s wait was not enough on a
        # retest, even though the exact same sequence had worked moments earlier in the same session.
        # ALSO confirmed live, separately, that the Employee/Work Team toggle can drift back to "Work
        # Team" between here and the earlier check with no single action caught doing it -- so this
        # loop re-asserts Employee is active on every poll, not just once up front, rather than
        # trying to pin the exact moment/cause of the reset. Same ajax-timing tolerance pattern
        # already applied throughout this file (e.g. _click_panel_row_by_text).
        #
        # Confirmed live (2026-08-26) that even with Employee correctly active the whole time, the
        # checklist itself can stay genuinely empty for the full ~8s poll window on some runs -- a
        # separate failure mode from the toggle-state bug above, still unexplained. As a recovery
        # attempt (not a confirmed fix), periodically toggle to Work Team and back to Employee to
        # force a fresh ajax repopulate, since the toggle's own onchange is the only known trigger
        # for this list to (re)render at all.
        click_button_js = """(label) => {
                const btn = [...document.querySelectorAll('div.ui-button')]
                  .find(b => b.textContent.trim() === label &&
                             b.getBoundingClientRect().width > 0);
                if (btn) btn.click();
            }"""
        employee_checkbox_js = """(name) => {
                const label = [...document.querySelectorAll('label')]
                  .find(l => l.textContent.trim() === name);
                return label ? label.getAttribute('for') : null;
            }"""
        employee_checkbox_id = None
        for attempt in range(24):  # ~20s, with forced refreshes every ~5s
            if attempt > 0 and attempt % 6 == 0:
                logger.warning("Stage C: employee checklist still empty after %.1fs for %s -- "
                                "forcing a refresh via Work Team -> Employee", attempt * 0.8, wo)
                await page.evaluate(click_button_js, "Work Team")
                await page.wait_for_timeout(1000)
                await page.evaluate(click_button_js, "Employee")
                await page.wait_for_timeout(1500)
            else:
                await page.evaluate(
                    """() => {
                        const btn = [...document.querySelectorAll('div.ui-button')]
                          .find(b => b.textContent.trim() === 'Employee' &&
                                     b.getBoundingClientRect().width > 0);
                        if (btn && !btn.classList.contains('ui-state-active')) btn.click();
                    }"""
                )
            employee_checkbox_id = await page.evaluate(employee_checkbox_js, SCHEDULE_EMPLOYEE)
            if employee_checkbox_id:
                break
            await page.wait_for_timeout(300)
        if not employee_checkbox_id:
            # Last resort: a full re-navigation, not just re-toggling within the same page load.
            # Confirmed live (2026-08-26) that toggling Work Team<->Employee repeatedly within one
            # page load did NOT recover an empty checklist -- unlike the Stage B.5 Confirm-button
            # gap, this has not been proven to be helped by a reload either, but it costs one extra
            # nav + re-select before giving up entirely.
            logger.warning("Stage C: employee checklist still empty for %s after in-page retries -- "
                            "trying a full re-navigation", wo)
            await self._open_schedule_board()
            await filter_input.click()
            await filter_input.fill(council_search)
            await filter_input.press("Enter")
            await page.wait_for_timeout(3000)
            order_row3 = page.locator("tr", has_text=wo.replace("WO-PO/", "")).locator("visible=true").first
            if await order_row3.count():
                await order_row3.click(timeout=10000)
                await page.wait_for_timeout(2000)
                await page.evaluate(click_button_js, "Employee")
                await page.wait_for_timeout(1500)
                filter_link2 = page.get_by_text("Filter", exact=True).locator("visible=true").first
                await filter_link2.click(timeout=10000)
                await page.wait_for_timeout(2000)
                for _ in range(10):  # ~5s more after the reload
                    employee_checkbox_id = await page.evaluate(employee_checkbox_js, SCHEDULE_EMPLOYEE)
                    if employee_checkbox_id:
                        break
                    await page.wait_for_timeout(500)
        if not employee_checkbox_id:
            logger.warning("Stage C: employee %r not found in the Filter checklist for %s "
                            "after waiting, forced refreshes, and a re-navigation", SCHEDULE_EMPLOYEE, wo)
            await self._screenshot(f"stage_c_no_employee_{wo.replace('/', '-')}")
            return False
        await page.locator(f'label[for="{employee_checkbox_id}"]').click(timeout=10000)
        await page.wait_for_timeout(2000)
        # Verify the checkbox actually toggled -- confirmed live (2026-08-25) that a text-based click
        # here can report success while the box stays unchecked.
        checked = await page.evaluate(
            "(id) => document.getElementById(id)?.parentElement?.parentElement?"
            ".querySelector('.ui-chkbox-icon')?.classList.contains('ui-icon-check')",
            employee_checkbox_id,
        )
        if not checked:
            logger.warning("Stage C: clicking %s's checkbox did not actually check it for %s",
                            SCHEDULE_EMPLOYEE, wo)
            return False
        await filter_link.click(timeout=10000)
        await page.wait_for_timeout(2000)

        new_event_btn = page.locator('[id*="newEventButton"]').first
        if not await new_event_btn.count():
            logger.warning("Stage C: no 'add event' cell found for %s's row (%s)", SCHEDULE_EMPLOYEE, wo)
            return False
        await new_event_btn.click(timeout=10000)
        await page.wait_for_timeout(3000)

        job_date_str = payload.job_date.strftime("%d/%m/%Y")
        remarks = f"{payload.nature_of_work} - {wo}"
        await self._fill_labeled_input("From", job_date_str)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(1000)
        await self._fill_labeled_input("To", job_date_str)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(1000)
        await self._fill_labeled_input("Remarks", remarks)
        await page.wait_for_timeout(1000)

        # Time sub-fields last -- see docstring point 4. Located relationally: each date/time pair
        # lives together in one .synfaces-grid-item (the div-based layout confirmed for this popup,
        # NOT a <td> -- there is no <tr>/<td> anywhere in this dialog's markup). The date input
        # (DD/MM/YYYY) and its sibling time input (HH:MM) share that same grid-item container.
        # Uses Locator.fill(), not a raw JS value-setter -- confirmed elsewhere in this file
        # (_fill_grid_field's docstring) that PrimeFaces cells can silently ignore a value set via a
        # synthetic event and only reliably commit through Playwright's own fill().
        for date_value, time_value in (("From", "08:00"), ("To", "08:30")):
            # Scoped to inside the Event Details dialog (same reasoning as the confirm button fix
            # below): querying the whole document risks matching a same-named label/field belonging
            # to an unrelated, hidden element elsewhere on the page.
            time_input_id = await page.evaluate(
                """(label) => {
                    const dialog = [...document.querySelectorAll('[role="dialog"]')]
                      .find(d => d.textContent.includes('Event Details') &&
                                 d.getBoundingClientRect().width > 0);
                    if (!dialog) return null;
                    const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                    const host = [...dialog.querySelectorAll('td,div,span,label')]
                      .find(e => e.children.length === 0 && norm(e.textContent) === label);
                    const item = host && host.closest('.synfaces-grid-item');
                    if (!item) return null;
                    const inputs = [...item.querySelectorAll('input:not([type=hidden])')];
                    const timeInput = inputs.find(i => /^\\d{1,2}:\\d{2}$/.test(i.value));
                    return timeInput ? timeInput.id : null;
                }""",
                date_value,
            )
            if not time_input_id:
                logger.warning("Stage C: could not locate the %s time field for %s", date_value, wo)
                continue
            await page.locator(f'[id="{time_input_id}"]').fill(time_value)
            await page.wait_for_timeout(500)

        # Scoped to the Event Details dialog itself, not a bare button:has(span.fa-check) --
        # confirmed live (2026-08-26) that a generic fa-check search can resolve to an unrelated,
        # hidden confirm-dialog's own "Yes" button elsewhere on the page (id="j_idt969", a
        # PrimeFaces-generated id that recurs across different dialogs) and hang forever waiting for
        # it to become visible. role="dialog" + its own title text is a stable, structural anchor.
        event_dialog = page.locator('[role="dialog"]:has-text("Event Details")').locator("visible=true").first
        confirm_btn = event_dialog.locator('button:has(span.fa-check)').first
        if not await confirm_btn.count():
            logger.warning("Stage C: no Event Details confirm button found for %s", wo)
            return False
        await confirm_btn.click(timeout=10000)
        await page.wait_for_timeout(4000)
        # Confirmed live (2026-08-26) that this click can leave the dialog (and its modal overlay)
        # still open, which then blocks every subsequent click with "<div class=ui-dialog-mask>
        # intercepts pointer events" for a full 30s timeout. Wait for it to actually close, retrying
        # the confirm click once if it hasn't.
        try:
            await event_dialog.wait_for(state="hidden", timeout=8000)
        except Exception:
            logger.warning("Stage C: Event Details dialog still open after Confirm for %s -- "
                            "retrying the click", wo)
            if await confirm_btn.count():
                await confirm_btn.click(timeout=10000)
                await page.wait_for_timeout(4000)
            try:
                await event_dialog.wait_for(state="hidden", timeout=8000)
            except Exception:
                logger.warning("Stage C: Event Details dialog would not close for %s", wo)
                await self._screenshot(f"stage_c_dialog_stuck_{wo.replace('/', '-')}")
                return False

        # The grid/selection can reset after the popup's ajax refresh -- re-select the order before
        # looking for the (now separately-appearing) Submit action.
        await filter_input.click()
        await filter_input.fill(council_search)
        await filter_input.press("Enter")
        await page.wait_for_timeout(3000)
        order_row2 = page.locator("tr", has_text=wo.replace("WO-PO/", "")).locator("visible=true").first
        if await order_row2.count():
            await order_row2.click(timeout=10000)
            await page.wait_for_timeout(2000)

        submit_btn = page.locator('button:has(span.fa-vote-yea)').locator("visible=true").first
        if not await submit_btn.count():
            logger.warning("Stage C: no Submit button found on Order Details for %s", wo)
            return False
        # Confirmed live (2026-08-26) that this button can still read disabled right after
        # re-selecting the order row -- poll for it to actually enable before giving up, same
        # ajax-timing tolerance pattern used throughout this file.
        enabled = False
        for _ in range(10):  # ~5s
            if await submit_btn.is_enabled():
                enabled = True
                break
            await page.wait_for_timeout(500)
        if not enabled:
            logger.warning("Stage C: Submit button stayed disabled for %s -- re-selecting the order "
                            "row once more", wo)
            if await order_row2.count():
                await order_row2.click(timeout=10000)
                await page.wait_for_timeout(2000)
            for _ in range(10):
                if await submit_btn.is_enabled():
                    enabled = True
                    break
                await page.wait_for_timeout(500)
        if not enabled:
            logger.warning("Stage C: Submit button never enabled for %s", wo)
            await self._screenshot(f"stage_c_submit_disabled_{wo.replace('/', '-')}")
            return False
        await submit_btn.click(timeout=10000)
        await page.wait_for_timeout(1500)
        yes_btn = page.get_by_role("button", name="Yes").locator("visible=true")
        if await yes_btn.count():
            await yes_btn.first.click(timeout=10000)
        await page.wait_for_timeout(5000)

        # Verify via "Upcoming Service" showing a new entry -- confirmed live this is the only signal
        # that reflects real server-side persistence; the "not submitted" warning disappearing alone
        # is NOT sufficient (it disappears one step earlier, at the popup's own checkmark).
        upcoming = await page.locator("text=Upcoming Service").locator("visible=true").count()
        if not upcoming:
            logger.warning("Stage C: could not find 'Upcoming Service' panel to verify %s", wo)
            return False
        confirmed = await page.get_by_text(order_no, exact=False).locator("visible=true").count() > 0
        if not confirmed:
            logger.warning("Stage C: %s (%s) not found in Upcoming Service after submit", wo, order_no)
            return False
        logger.info("Stage C scheduled and submitted for %s (%s)", wo, order_no)
        return True

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
        # A confirm dialog may appear (Yes/OK) â€” click it if present.
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
        blank â€” Item Code must be looked up via its own autocomplete (see _select_item_code; it's a
        table-cell input, not a labeled form field, so the generic _select_autocomplete_row doesn't
        apply) before Unit Price/Remarks can be set. `row_index` picks which row among possibly
        several (one per WO line item) â€” see _stage_b_create_quotation.
        """
        assert self.page is not None
        label = f"row {row_index}"

        item_ok = await self._select_item_code(ITEM_CODE, row_index)
        if not item_ok:
            logger.warning("Stage B: could not select Item Code %s for %s â€” leaving %s's item blank",
                            ITEM_CODE, label, label)

        # Qty/Unit Price/Remarks: click each cell via Playwright's own actionability-checked
        # `.click()` (see _grid_cell_locator's docstring for why NOT raw coordinates), then type via
        # real keyboard input and Tab to commit. Verified + retried, not fire-and-forget: confirmed
        # live (2026-08-14) that typing into one cell right after another can silently not stick
        # (Tab likely kicks off a PrimeFaces AJAX recalculation â€” Total Amount depends on Qty * Unit
        # Price â€” and moving to the next cell before it settles interrupts that cell's own commit).
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
        """Fill one Details-grid cell and poll to confirm it stuck â€” retrying up to `attempts` times.
        Returns whether it ultimately committed.

        Uses Locator.fill(), NOT click + Control+A + keyboard.type() + Tab. Confirmed live
        (2026-08-19), directly prompted by the user manually typing into a stuck cell and having it
        commit instantly: 6 different keystroke-simulation commit strategies (Tab, Enter, click-away,
        explicit Tab down/up, human-cadence typed delay) were tested head-to-head against fill() on
        the same cell â€” every keystroke-simulation method failed (0/6 across two full test runs,
        including the exact production sequence used here before this fix), while fill() stuck 5/5.
        fill() sets the value and dispatches real input/change events directly, bypassing individual
        key events entirely â€” whatever server-side listener this PrimeFaces cell binds its commit to
        is apparently keyed off input/change, not the keydown/keyup sequence Playwright's
        keyboard.type() produces, which is why a real human's native browser keystrokes worked all
        along while the automated equivalent did not.

        The bigger finding (2026-08-19, from watching the user type manually into a live cell with
        5s polling): the grid RE-RENDERS ITSELF independently of any write in flight, and can
        transiently blank out an already-committed, untouched cell for several seconds before it
        recovers on its own â€” observed directly on Qty while the user was typing into an unrelated
        Unit Price cell. The old 2.4s patience window (8 x 300ms) is shorter than that recovery
        window, so it was reading a mid-flicker blank as "genuinely failed" and immediately
        overwriting â€” an overwrite landing mid-re-render is a plausible way to actually corrupt what
        would otherwise have recovered fine on its own. Two changes address this directly:
          1. The window is now much longer (up to ~15s) before giving up on one attempt.
          2. A read is only trusted once it's STABLE across two consecutive checks (not just present
             once) â€” a single matching read can itself be a flicker artifact on its way to something
             else, same as a single blank read can be.
        This is deliberately defensive: it costs a little time on the (common) fast-committing case,
        but a slow, correct commit is far cheaper than a fast, wrong retry that corrupts a good value.

        Extracted from _fill_line_item_row so _verify_and_refill_rows can re-run it standalone on
        any single cell that a later edit clobbered, without re-filling the whole row.
        """
        assert self.page is not None
        page = self.page
        poll_interval_ms = 500
        max_polls = 30  # ~15s per attempt
        for attempt in range(attempts):
            cell = await self._grid_cell_locator(row_index, header_regex)
            if not cell:
                logger.warning("Stage B: could not locate the %s cell for row %d", field_name, row_index)
                return False
            try:
                await self._click_when_clear(cell)
            except Exception as exc:
                logger.warning("Stage B: could not click the %s cell for row %d: %s", field_name, row_index, exc)
                return False
            await cell.fill(value)

            # Require the target value to read back correctly on TWO CONSECUTIVE polls, not just
            # one â€” a single matching read can itself be a transient state on its way to reverting,
            # same as a single non-matching read can be on its way to recovering. See docstring.
            previously_matched = False
            for _ in range(max_polls):
                await page.wait_for_timeout(poll_interval_ms)
                try:
                    current = await cell.input_value()
                except Exception:
                    # The cell can go briefly stale/detached during a grid re-render; re-resolve it
                    # rather than treat a transient DOM error as a real failure.
                    cell = await self._grid_cell_locator(row_index, header_regex)
                    if not cell:
                        break
                    continue
                matched = current == value
                if matched and previously_matched:
                    return True
                previously_matched = matched
            logger.warning("Stage B: %s for row %d did not stick (attempt %d) â€” retrying",
                            field_name, row_index, attempt + 1)
        return False

    async def _verify_and_refill_rows(self, line_items: list[LineItem], remarks: str) -> None:
        """After every Details row has been filled once, re-read ALL rows and re-fill any field
        that doesn't match its target â€” repeating a few rounds until everything holds or the
        attempt budget runs out.

        Confirmed live (2026-08-15) that a row already read back correctly right after being filled
        can later revert to blank/0.00 once a SUBSEQUENT row's edits fire their own PrimeFaces ajax
        recalculation â€” a server-side ordering race (the earlier row's commit hadn't landed
        server-side yet when the later row's request went out), not a client-side click/typing bug.
        _fill_line_item_row's own per-cell retry only checks immediately after typing that cell, so
        it can't catch a value reverted by a LATER row's edit. This re-checks everything at the end.

        Confirmed live (2026-08-19) that the grid can also transiently blank out an already-correct,
        untouched cell for several seconds during its own re-render, unrelated to any write â€” a
        single read here is not proof of a real problem. Before treating a mismatch as real (and
        triggering a re-fill, which risks overwriting mid-flicker), re-checks that SAME cell once
        more after a short wait â€” only acts if it's still wrong on the second look.
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
                    await self.page.wait_for_timeout(2000)
                    row = await self._read_grid_row(i, ("item code",))
                    if not (row.get("item code") or "").strip():
                        any_problem = True
                        logger.warning("Verify pass %d: row %d Item Code reverted â€” re-selecting",
                                        round_num + 1, i)
                        if not await self._select_item_code(ITEM_CODE, i):
                            logger.warning("Verify pass %d: could not re-select Item Code for row %d",
                                            round_num + 1, i)
                for field_name, target_value, header_regex, current_value in targets:
                    if current_value == target_value:
                        continue
                    # Double-check before acting â€” confirmed live this can be a transient re-render
                    # flicker, not a real reversion.
                    await self.page.wait_for_timeout(2000)
                    recheck = await self._read_grid_row(i, (header_regex,))
                    recheck_value = recheck.get(header_regex)
                    if recheck_value == target_value:
                        logger.info("Verify pass %d: row %d %s read %r once but %r on recheck â€” "
                                    "a flicker, not a real reversion, leaving it alone",
                                    round_num + 1, i, field_name, current_value, recheck_value)
                        continue
                    any_problem = True
                    logger.warning("Verify pass %d: row %d %s reverted (%r != %r, confirmed on "
                                    "recheck) â€” re-filling",
                                    round_num + 1, i, field_name, recheck_value, target_value)
                    await self._fill_grid_field(i, field_name, target_value, header_regex)
            if not any_problem:
                break

    _TOTAL_AFTER_TAX_JS = """() => {
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

    async def _read_total_after_tax(self) -> float | None:
        """The page's own total aggregate, parsed â€” or None if absent/unparseable.

        This is the ONLY signal that reflects what the server actually committed. Every per-cell and
        per-row check in this file reads the DOM, which can show correct values for a row the server
        never recorded â€” see _force_totals_commit.
        """
        assert self.page is not None
        raw = await self.page.evaluate(self._TOTAL_AFTER_TAX_JS)
        if raw is None:
            return None
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _expected_totals(payload: WOPayload) -> list[float]:
        """The figures the page total may legitimately equal: the WO's pre-GST net and its
        GST-inclusive grand total. Which one this field holds isn't assumed â€” see the reconciliation
        in _assert_details_filled."""
        return [v for v in (payload.net_amount, payload.grand_total) if v is not None]

    async def _total_is_settled(self, expected: list[float]) -> tuple[bool, float | None]:
        """(is the page total both positive and equal to an expected figure, the total itself).

        With no expected figures to compare against (an unstructured WO), a positive total is all
        that can be required.
        """
        total = await self._read_total_after_tax()
        if total is None or total <= 0:
            return False, total
        if not expected:
            return True, total
        return any(abs(total - e) <= 0.05 for e in expected), total

    async def _force_totals_commit(self, payload: WOPayload, line_items: list[LineItem]) -> None:
        """Wait for the page total to reach the WO-authorised figure, re-writing each row's Qty via a
        nudge (0.00, then the real value) whenever it stalls short of it.

        Writes into a freshly-added Details row are unreliable, and the failure is NOT confined to one
        field or one quantity. Established live on 2026-08-18, across two independent investigations:

        - A fresh row's Qty defaults to '0.00' (measured immediately after Add Row, and again after
          selecting a real Item Code, without touching Qty).
        - Sometimes the writes visibly never land: with a qty=1 / price=33.00 line, BOTH Qty and Unit
          Price failed all three retry attempts and stayed 0.00/0.00 through _verify_and_refill_rows.
          That case is loud, and the submit gate already rejects it correctly.
        - Sometimes the DOM reads back CORRECT while the server has not caught up: on
          WO-PO/000080454 (rows 1x44.00 + 1x55.00) both rows read qty 1.00 with prices 44.00/55.00
          while the page total sat at 44.00 â€” row 1's amount simply missing. This is the dangerous
          shape, because every per-row DOM check passes and only the total exposes it.

        Deliberately NOT claimed here: that quantity=1 causes this. Every all-qty-1 WO failed and
        every WO with a qty>1 row succeeded (5 and 3 respectively on 2026-08-18), but a same-value
        no-op cannot be the mechanism â€” the default is 0.00, so writing 1.00 is a genuine change like
        any other. With 8 samples that split may well be coincidence; it is recorded as unexplained
        rather than dressed up as a cause.

        The actual mechanism, isolated live on 2026-08-20 on a throwaway 2-row draft: writes in one
        row ZERO OUT numeric cells in the OTHER row. Filling row 0 qty, row 1 qty, row 0 price, row 1
        price in that order â€” every one reporting success â€” ended with row 0 holding qty 1.00 / price
        0.00 and row 1 holding qty 0.00 / price 55.00. No row ever held qty AND price at the same
        moment, so the total never left 0.00. The grid's own columns rule out a per-row pricing
        constraint: "Unit Price/Adjusted Unit Price" is not readOnly, and "Free Balance"/"Total
        Amount" carry no input at all. So this is cross-row ajax interference â€” the same class
        _verify_and_refill_rows was built for, but it does not always converge within its 3 rounds
        once two or more rows are in play.

        So this method repairs whatever is ACTUALLY short, rather than assuming Qty. Each round
        re-reads every row and re-fills the specific fields that don't match (Qty and/or Unit Price),
        one at a time with a settle wait so a repair doesn't immediately knock out the cell it just
        fixed in another row. Only when every row's DOM already matches yet the total is still short
        does it fall back to the nudge (0.00 then the real value), which is the DOM-correct-but-
        server-behind case. Waiting for the EXPECTED total matters throughout â€” returning as soon as
        it went positive stopped at 44.00 on WO-PO/000080454, which the submit gate then correctly
        rejected as understated. Previously this only ever nudged Qty, which could not fix the real
        problem there (row 1's missing PRICE) and burned all three rounds achieving nothing.
        """
        assert self.page is not None
        expected = self._expected_totals(payload)
        # One round tends to land exactly ONE row, because repairing a row can knock out the cells a
        # previous round just fixed. Measured live (2026-08-20) on the 4-row WO-PO/000079836: the
        # total climbed 33.00 -> 77.00 -> 110.00 across three rounds, adding one row's amount each
        # time, and was still converging when a hardcoded 3-round budget cut it off short of 275.00.
        # The 3-row WO-PO/000081257 passed the same day needing only one round. So the budget has to
        # scale with row count, not sit at a constant: one round per row, plus headroom for rows that
        # need a second attempt. Still bounded â€” a genuinely stuck grid must fail, not spin forever.
        rounds = max(3, len(line_items) + 2)
        for attempt in range(rounds):
            for _ in range(24):  # ~12s per round for in-flight commits to land
                settled, total = await self._total_is_settled(expected)
                if settled:
                    if attempt:
                        logger.info("Totals committed after %d repair round(s): total=%.2f",
                                    attempt, total)
                    return
                await self.page.wait_for_timeout(500)
            # Confirmed live (2026-08-19) that the grid can transiently show a stale/blank total
            # during its own re-render, unrelated to any real problem â€” a single "still unsettled"
            # read after the wait above is not proof nudging is actually needed. Re-check once more
            # after a short pause before nudging, same defensive pattern as _fill_grid_field and
            # _verify_and_refill_rows: a nudge that lands mid-flicker is a plausible way to corrupt
            # an otherwise-fine row (this method only re-writes Qty â€” a wrong nudge risks knocking
            # out Unit Price via the same server-side ordering race _verify_and_refill_rows guards
            # against elsewhere).
            await self.page.wait_for_timeout(3000)
            settled, total = await self._total_is_settled(expected)
            if settled:
                logger.info("Page total settled on the recheck (%.2f) â€” was a flicker, not repairing",
                            total)
                if attempt:
                    logger.info("Totals committed after %d repair round(s): total=%.2f", attempt, total)
                return
            logger.warning(
                "Page total is %r, expected one of %s â€” repairing the Details grid (round %d/%d)",
                total, expected or "(nothing to compare against)", attempt + 1, rounds)
            for i, line_item in enumerate(line_items):
                qty_target = f"{line_item.quantity:.2f}"
                price_target = f"{line_item.billed_unit_price:.2f}"
                row = await self._read_grid_row(i, ("^qty", "unit price"))
                repairs = [
                    (name, target, regex) for name, target, regex, current in (
                        ("Qty", qty_target, r"^qty", row.get("^qty")),
                        ("Unit Price", price_target, "unit price", row.get("unit price")),
                    ) if current != target
                ]
                if repairs:
                    # Fix what's genuinely wrong, one cell at a time, letting each land before
                    # touching another â€” a repair itself can zero a cell in another row.
                    for name, target, regex in repairs:
                        logger.warning("Row %d %s is short â€” re-filling to %s", i, name, target)
                        await self._fill_grid_field(i, name, target, regex)
                        await self.page.wait_for_timeout(1500)
                elif qty_target != "0.00":
                    # Every cell in this row already reads right, so the server is simply behind:
                    # nudge Qty so the final write is a real change whatever the field holds. A
                    # zero-qty line is a data problem, not this bug, so there's nothing to nudge to.
                    await self._fill_grid_field(i, "Qty", "0.00", r"^qty")
                    await self._fill_grid_field(i, "Qty", qty_target, r"^qty")
                    await self.page.wait_for_timeout(1500)
        _, final = await self._total_is_settled(expected)
        logger.warning("Page total still %r after %d repair rounds â€” the submit gate will reject this",
                        final, rounds)

    async def _ensure_remarks_intact(self, payload: WOPayload, line_items: list[LineItem],
                                      remarks: str) -> None:
        """Re-check every row's Remarks AFTER the totals repair, and re-fill any that went blank.

        _force_totals_commit only ever touches Qty and Unit Price, and it stops as soon as the page
        total matches â€” which it can, correctly, while Remarks has been wiped, because Remarks does
        not contribute to the total. So the repair loop exits satisfied and _assert_details_filled
        then refuses to submit on "Remarks is blank".

        Confirmed live (2026-08-21) on WO-PO/000061116, the first SKTC WO through Synergix: Remarks
        was demonstrably filled (logged in full mid-fill), the total settled correctly at 27.00
        matching the WO, and the submit gate still rejected the record because Remarks had reverted
        to blank by the time it was checked. Everything else on that quotation was right.

        Same shape as the bug where a Qty-only nudge could never fix a missing Unit Price: a repair
        pass that cannot touch the field that is actually broken. Re-verifies the total afterwards,
        since re-filling Remarks fires its own ajax and can in turn disturb the numbers.
        """
        assert self.page is not None
        if not line_items:
            return
        repaired = False
        for i, _ in enumerate(line_items):
            row = await self._read_grid_row(i, ("remarks",))
            current = (row.get("remarks") or "").strip()
            if current == remarks.strip():
                continue
            # Re-check once before acting: the grid re-renders on its own and a single blank read can
            # be a flicker, the same defensive pattern _verify_and_refill_rows uses.
            await self.page.wait_for_timeout(2000)
            row = await self._read_grid_row(i, ("remarks",))
            current = (row.get("remarks") or "").strip()
            if current == remarks.strip():
                continue
            logger.warning(
                "Row %d Remarks did not survive the totals repair (%s) â€” re-filling", i,
                "blank" if not current else "differs")
            await self._fill_grid_field(i, "Remarks", remarks, "remarks")
            repaired = True
            await self.page.wait_for_timeout(1500)
        if repaired:
            # Re-filling Remarks can knock the numbers about, so settle the total again.
            await self._force_totals_commit(payload, line_items)

    async def _read_grid_row(self, row_index: int, header_regexes: tuple[str, ...]) -> dict:
        """Read back the CURRENT input values of the given Details-grid row's named columns.

        Used to confirm a fill actually committed, since dispatching input/change events on an input
        can silently fail to persist (see _fill_line_item_row's docstring) â€” reading the live DOM
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
        # Settle before navigating away: confirmed live (2026-08-17, an independent audit) that
        # this is the SAME class of bug as close()'s pre-close wait, just triggered by navigation
        # instead of closing the browser â€” a page.goto() abandons any in-flight PrimeFaces ajax
        # request the moment it starts, before the server has necessarily processed/saved it. Since
        # _back_to_home() runs after EVERY record in a batch (write()'s finally, and the end of
        # amend_quotation), not just the very last one, this explains a wider pattern than close()
        # alone covered: 5 of ~20 multi-line quotations in one run had their unit price commit
        # correctly but a quantity field on one row silently revert to 0 afterward â€” the pricing
        # value settled before this navigation, the quantity value (written later, per-field) did
        # not. The wait must happen BEFORE goto(), not after â€” the in-flight request is abandoned
        # the instant navigation starts, so waiting only after wait_for_load_state is too late.
        try:
            await self.page.wait_for_timeout(4000)
        except Exception:
            pass
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

