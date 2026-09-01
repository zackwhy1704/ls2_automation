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

# RETIRED as of 2026-08-31 -- kept only because Stage B's Salesperson field uses the same literal
# "TAN WEI YING" string directly (see _stage_b_create_quotation, unrelated to this constant). This
# constant is no longer read by Stage C: frame-by-frame analysis of the client's own walkthrough
# video (JBTC WO Synergix.mp4, see docs/synergix_workflow.md "Stage C, frame-by-frame") proved
# Stage C's real scheduling target is a WORK TEAM ("Assigned" dropdown) paired with INFIGO/ECOCARE
# via a "To Pair With" checklist -- NOT an individual employee. "TAN WEI YING" genuinely does
# appear on the real Event Details popup, but as one of 12 "To Pair With" options, not the
# assignee -- which is almost certainly why every session before this one assumed she was the
# target. See ASSIGNED_WORK_TEAM below for what replaced this.
SCHEDULE_EMPLOYEE = "TAN WEI YING"

# Schedule Board (Stage C) "Assigned" Work Team, per the video walkthrough (WO-PO/000080291,
# 2026-08-31). ASSUMPTION, NOT CONFIRMED: the video shows exactly one example, where the "Assigned"
# dropdown defaulted to "800SUPER" and the human operator left it as-is. Nothing on the source WO
# (Job Sheet, GL No., Schedule Type, Contractor Code) obviously determines this value, and the
# workflow doc never explains how to choose it. Using the observed default until the client
# confirms whether it's fixed or WO/council/date-dependent -- see docs/synergix_workflow.md
# "Open question for the client" for the full writeup of this assumption.
ASSIGNED_WORK_TEAM = "800SUPER"

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
            quo_id, vo_confirmed = await self._submit_quotation(payload)
            if settings.DRY_RUN:
                # Schedule Board (C) and Fulfil (D) remain manual â€” done by the team in Synergix.
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"DRY_RUN: quotation draft {quo_id or '(id unread)'} created + filled, NOT submitted.",
                )
            # Stage B.5: a submitted quotation sits in "Under Variation" and is NOT yet a schedulable
            # Service Order until this is confirmed. _submit_quotation already attempts this inline
            # (matching the client's actual flow -- see its docstring) -- only fall back to the older
            # navigate-to-"Under Variation"-and-reopen path if that didn't already succeed. Confirmed
            # live (2026-08-27) that a successfully-confirmed quotation can still show under "Under
            # Variation" on a fresh reopen, so re-running the fallback unconditionally risks clicking
            # Confirm a second time on an already-confirmed record -- untested territory, avoided here.
            # Best-effort either way: a failure leaves a real, submitted quotation (just requires a
            # human to confirm the VO manually).
            if quo_id and not vo_confirmed:
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
            # Stage C: schedule the new Service Order (assign ASSIGNED_WORK_TEAM + To-Pair-With at
            # the WO's job date) and submit it -- see _schedule_stage_c's docstring. Best-effort, same reasoning
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
            # Stage D: Fulfil the now-scheduled Service Order for billing -- see _fulfil_stage_d's
            # docstring. Best-effort, same reasoning as B.5/C: a failure here still leaves a real,
            # scheduled Service Order, just requiring a human to Fulfil it manually in Synergix.
            fulfilled = False
            try:
                fulfilled = await self._fulfil_stage_d(payload)
            except Exception:
                logger.exception("Stage D fulfil failed for %s (quotation %s)",
                                  payload.wo_po_number, quo_id)
            if not fulfilled:
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"Quotation {quo_id or '(id unread)'} created, submitted, Variation Order "
                    "confirmed, and Schedule Board (Stage C) completed, but Stage D (Fulfil) failed "
                    "or was skipped -- human must fulfil it manually in Synergix.",
                )
            return WriteResult(
                WOStatus.PROCESSED,
                f"Quotation {quo_id or '(id unread)'} created, submitted, Variation Order confirmed, "
                "Schedule Board (Stage C) completed, and Fulfil (Stage D) submitted for billing.",
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
        # Confirmed live (2026-08-31) on WO-PO/99999m4: for a jQuery-UI/PrimeFaces datepicker input
        # (class includes "hasDatepicker", seen on Schedule Board's Event Details From/To fields),
        # Playwright's fill() sets the raw DOM value but does NOT fire the datepicker widget's own
        # change handler -- Synergix's server-side booking still used its PREVIOUS date (defaulting
        # to today) even though the input visibly showed the correct WO date, causing a real
        # "SV9010: Schedule time overlapped with other schedules" collision against another test WO
        # that also defaulted to today. A plain field.fill() looked like it worked (correct text in
        # the input) but silently didn't commit -- the same "silent-failure shape" as
        # _select_external_remark's row-click bug above, just via a different mechanism. Firing a
        # real 'change' event (matching what the widget's own onSelect would dispatch after a
        # calendar-day click) forces the value to actually register. Harmless no-op for any
        # non-datepicker field this method is also used for (Enquiry/Subject, Reference No., etc.).
        await field.evaluate("(el) => el.dispatchEvent(new Event('change', {bubbles: true}))")

    async def _select_external_remark(self, remark_code: str, *, timeout_ms: int = 20000) -> bool:
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

        Confirmed the popup closes ITSELF the instant the matching row is clicked (frame-by-frame
        video review, 2026-09-01, JBTC WO Synergix.mp4 4:55-5:05) -- no separate dismiss step exists
        in a working flow, matching the click-handler description above.

        `timeout_ms` widened from 6000 to 20000 (2026-09-01) after five straight live "no match"
        failures (WO-PO/99999m5, m7, m8, m10, m12, m13) all showed "OCBC BANK DETAIL" plainly visible
        in the results table on the failure screenshot, yet the match poll gave up first. Timestamps
        from the m13 run showed only ~6.8s elapsed between the prior step finishing and this method's
        own "no match" warning firing -- i.e. right at the OLD 6000ms budget's edge -- consistent with
        this being a genuine, occasional server-response-time race (the same class already documented
        elsewhere in this file for the Event Details Confirm ajax, which can take up to ~18-30s), not
        a permanently-broken selector. A `[id$="searchResultTable"]` (ends-with) vs.
        `table[id*="searchResultTable"]` (contains) selector fix was also tried and shipped
        separately (commit 9e3c20b) since a live error-log DOM excerpt showed the real ids continue
        past that substring -- keep both fixes; if failures still recur after this widening, that
        selector is the next thing to re-verify live, not this timeout.
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
        # Confirmed live (2026-09-01) via a real error-log DOM excerpt that the actual table id is
        # "searchResultsForm:searchResultTable" with PrimeFaces column-header ids suffixed further
        # (e.g. "...:searchResultTable:j_idt8132") -- `[id$="searchResultTable"]` (ends-with) can
        # only match an element whose id LITERALLY ENDS at that string, so it was silently matching
        # nothing (or the wrong element) whenever the real table/header ids continued past that
        # point, exactly the kind of "no match found" false negative repeatedly seen live on a row
        # that screenshots proved was plainly visible (WO-PO/99999m5, m7, m8, m10, m12). Switched to
        # `[id*="searchResultTable"]` (contains, not ends-with) and scoped to `<table>` specifically
        # to avoid also matching the column-header `<th>` elements that share the same id substring.
        # SIMPLIFIED (2026-09-01): both the ends-with (`[id$=...]`) and contains (`[id*=...]`) table-id
        # selector approaches kept silently failing live on rows a screenshot proved were plainly
        # visible (WO-PO/99999m5, m7, m8, m10, m12, m13, live1) -- confirmed the SAME run's popup
        # right after the user manually clicked the identical visible "OCBC BANK DETAIL" row
        # themselves and it worked instantly, no special handling needed. The whole table/tbody/tr
        # scoping was solving a problem that doesn't exist: a human doesn't care what table element
        # wraps the row, they just look at the text and click it. Replaced with the same, much
        # simpler approach -- find any visible <a> anywhere on the page whose own text contains the
        # target remark code, exactly matching what a human actually does.
        js = """([needle, marker]) => {
                const links = [...document.querySelectorAll('a')]
                    .filter(a => a.offsetParent !== null && a.textContent.includes(needle));
                if (!links.length) return false;
                links[0].setAttribute(marker, '1');
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
            # REAL ROOT CAUSE finally found (2026-09-01) via frame-by-frame video review (every
            # single 30fps frame, 4:55-5:05 in JBTC WO Synergix.mp4, pixel-diffed to localize the
            # exact click): the whole premise of this "not found" branch -- that a working flow ever
            # needs to explicitly DISMISS this popup -- was wrong. The video shows the popup closes
            # ITSELF, automatically, the instant the matching row is clicked (one frame: popup open
            # with rows highlighted on hover -> next frame: popup gone, External Remarks filled).
            # There is no separate close step in the real flow at all, matching this method's own
            # docstring ("the row's actual click handler... both sets the textarea and fires a
            # PrimeFaces ajax update"). Every live failure (WO-PO/99999m5, m7, m8, m10, m12) was NOT
            # a dismiss-mechanism bug -- it was the JS match query above failing to find a row that a
            # screenshot proved was plainly visible, meaning `found` stayed False and this whole
            # branch fired for a popup that a human would have simply clicked through normally.
            # FIXED (see the js string above): `[id$="searchResultTable"]` (ends-with) could only
            # match an id that LITERALLY ENDS at that string -- a live error-log DOM excerpt showed
            # the real ids continue further (e.g. "...:searchResultTable:j_idt8132" on the column
            # header), so the ends-with selector was silently matching nothing. Switched to a
            # `table[id*="searchResultTable"]` contains-selector.
            #
            # This Escape+close-button fallback is kept as a defensive last resort ONLY (e.g. a
            # genuinely different remark code with zero real matches, which the video never exercised
            # since the human always found a match) -- it should rarely if ever fire now that the
            # match query itself is fixed. If it does fire and this still leaves the popup open,
            # that's a new, separate incident worth its own fresh investigation, not a sign this
            # comment's diagnosis was wrong.
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            close_btn = page.locator(
                '#searchPanel [role="dialog"]:has-text("Remarks") a.ui-dialog-titlebar-close'
            ).locator("visible=true").first
            if await close_btn.count():
                await close_btn.click(timeout=5000)
                await page.wait_for_timeout(500)
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

    async def _service_order_exists_for_quotation(self, quotation_no: str) -> bool:
        """Check Schedule Board's Unscheduled Service Orders grid for a row whose "Quotation No."
        column matches. Used as an idempotency guard before _confirm_variation_order clicks Confirm,
        so a missed/slow success signal (the SA0005 toast, or "left Under Variation") never causes a
        second real Confirm click and a duplicate Service Order -- confirmed live (2026-08-30) this
        exact scenario created SV00008877 AND SV00008878 for the same quotation, QUO0006817.

        Polls for up to ~12s (not a single fixed 3s wait) before concluding "not found". Confirmed
        live (2026-08-30) that even THIS check's own single-wait version was too fast once: it ran
        moments after the inline confirm's own Confirm+Yes click, found nothing yet (Schedule
        Board's grid had not indexed the just-created order in time), and let a second real Confirm
        click through -- creating SV00008879 AND SV00008880 for QUO0006818 the same way. This is the
        same server-side ajax-timing class of bug as the rest of Stage C; the fix is the same
        pattern used throughout this file: poll, don't assume one wait is long enough.

        This only checks Unscheduled Service Orders (not yet scheduled/submitted) -- a Service Order
        that has since been scheduled and submitted by Stage C would no longer appear here, but by
        that point Stage B.5 is moot anyway (write() only calls this before Stage C runs).
        """
        assert self.page is not None
        page = self.page
        try:
            await self._open_schedule_board()
            header = page.locator("th:visible", has_text="Quotation No.").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click(timeout=8000)
            await filter_input.fill(quotation_no)
            await filter_input.press("Enter")
            row = page.locator("tr", has_text=quotation_no).locator("visible=true").first
            for _ in range(8):  # ~12s: 8 x 1.5s, matching the SA0005 toast poll's own budget
                await page.wait_for_timeout(1500)
                if await row.count() > 0:
                    return True
            return False
        except Exception:
            logger.exception("_service_order_exists_for_quotation: check errored for %s -- "
                              "assuming no existing Service Order (fail open, matches prior "
                              "behavior)", quotation_no)
            return False

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
        schedule. Returns whether the confirm was observed to succeed, verified via the SA0005
        info-banner toast ("Service Order No.: SV... is created successfully") -- NOT via the
        quotation leaving "Under Variation", which is not a reliable signal (see below).
        """
        assert self.page is not None
        page = self.page

        if _dry_guard(f"confirm Variation Order for {quotation_no}"):
            return True  # DRY_RUN: leave it sitting in Under Variation

        # Pre-flight: a Service Order already existing for this quotation is the one signal proven
        # NOT to have false positives (unlike the SA0005 toast, which can be missed by a slow/failed
        # locator wait even after a genuine success, and unlike "left Under Variation", proven
        # unreliable above). Confirmed live (2026-08-30) that a missed toast on QUO0006817 led this
        # method to re-run Confirm+Yes a second time, creating a SECOND real Service Order
        # (SV00008878) for the same quotation -- a genuine duplicate this check would have caught
        # before ever clicking Confirm again.
        if await self._service_order_exists_for_quotation(quotation_no):
            logger.info("_confirm_variation_order: a Service Order already exists for %s -- "
                        "treating as already confirmed, not re-clicking Confirm", quotation_no)
            return True

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
        if not await yes_btn.count():
            logger.warning("_confirm_variation_order: no visible 'Yes' button after Confirm on %s",
                            quotation_no)
            await self._screenshot(f"vo_confirm_no_yes_button_{quotation_no}")
            return False
        await yes_btn.first.click(timeout=10000)

        # Verify via the SA0005 success toast, NOT via the quotation leaving "Under Variation".
        # Confirmed live (2026-08-27) on QUO0006787: the toast "SA0005: Service Order No.:
        # SV00008873 is created successfully." fired and a real Service Order was created (verified
        # separately in Schedule Board), but the quotation STILL showed in "Under Variation" (badge
        # count even went up, 102->103) on every recheck afterward, including after a full
        # navigate-away-and-back cycle. The "no longer under Under Variation" check used previously
        # was therefore a false negative that mislabelled genuine successes as PARTIAL failures on
        # every WO run that day. This toast is the reliable signal instead.
        #
        # Polling toast.count() (DOM presence), NOT toast.wait_for(state="visible") -- confirmed
        # live (2026-08-30) on QUO0006817 that TWO SA0005 toasts (for two separately-created Service
        # Orders) were sitting stacked in the notification tray at the exact moment wait_for(visible)
        # timed out and this method reported "no toast seen", which then caused write() to retry
        # Confirm+Yes a second time and create a genuine duplicate Service Order. count() polling
        # tolerates a toast that is DOM-present but not (yet, or no longer) Playwright-"visible" due
        # to stacking/animation.
        toast = page.locator("text=/SA0005.*Service Order No\\.?:?\\s*SV\\d+.*created successfully/i")
        for _ in range(16):  # ~8s
            if await toast.count():
                toast_text = await toast.first.inner_text()
                logger.info("Confirmed Variation Order for %s -- %s", quotation_no, toast_text.strip())
                return True
            await page.wait_for_timeout(500)

        # No toast found even by DOM presence -- before declaring failure, check Schedule Board
        # directly. Confirmed live (2026-08-30): this is the ground-truth signal a toast can never
        # be better than, and catches a success the toast check missed for any reason.
        if await self._service_order_exists_for_quotation(quotation_no):
            logger.info("_confirm_variation_order: no toast seen for %s, but a Service Order exists "
                        "in Schedule Board -- treating as confirmed", quotation_no)
            return True
        logger.warning("_confirm_variation_order: no SA0005 success toast seen for %s after "
                        "Confirm+Yes, and no Service Order found in Schedule Board", quotation_no)
        await self._screenshot(f"vo_confirm_no_toast_{quotation_no}")
        return False

    async def _hard_reset_browser(self) -> None:
        """Close the entire browser context and relaunch it from scratch, reusing the persisted
        session directory (so the login cookie survives -- no credentials re-entry needed).

        Built specifically for Stage C's employee-checklist flakiness. Confirmed live (2026-08-30):
        _open_schedule_board's own "re-navigation" loop (login() is a no-op once self._logged_in is
        set; it just re-clicks through the menu in the SAME page/tab/browser process) failed 6/6
        times in one continuous automated run -- 3 in-page toggle refreshes plus 3 of that
        re-navigation loop. Minutes later, the exact same order's checklist populated on the FIRST
        attempt from a genuinely fresh browser process (a separate discovery-script run, new
        Playwright launch, new login flow). That is a materially different action than anything the
        in-page retries do: a new process means a new renderer, new websocket/polling connections to
        Synergix, and a fresh client-side JS heap -- any of which could be what the old session was
        missing. This method reproduces that exact difference programmatically, so write() never
        again needs a human to relaunch a script to get past this.
        """
        logger.warning("Stage C: hard-resetting the browser (new process, same persisted session) "
                        "after in-page recovery failed")
        if self._context:
            try:
                await self._context.close()
            except Exception:
                logger.exception("Stage C hard reset: error closing the old browser context "
                                  "(continuing anyway)")
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                logger.exception("Stage C hard reset: error stopping the old Playwright instance "
                                  "(continuing anyway)")
        self._logged_in = False
        self.page = None
        await self.start()
        await self.login()

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
        """Stage C, outer retry wrapper: run _schedule_stage_c_attempt, and if it fails, do a full
        HARD RESET of the browser (new process, same persisted login session -- see
        _hard_reset_browser's docstring) and retry the entire attempt from scratch.

        WHY A WHOLE-METHOD RETRY, NOT another per-step fix: _schedule_stage_c_attempt has 13
        distinct internal failure points (checklist population, calendar row rendering, the
        newEventButton click, the Event Details dialog appearing, the wrong-employee check, the
        Submit button enabling, the final "Upcoming Service" verification, and others) -- each one
        individually documented, across multiple sessions, as intermittently failing and then
        succeeding on a LATER attempt with no code change in between. That pattern -- same input,
        same code, different outcome -- is the signature of server-side ajax timing/load on
        Synergix's own backend, not a client-side bug isolated to any one step. Patching each of the
        13 failure points with its own bespoke hard-reset-and-retry would duplicate the same logic
        13 times and still miss whichever ajax call breaks next in a future session. Retrying the
        WHOLE method after a hard reset covers all of them uniformly, including ones not yet seen.

        Confirmed live (2026-08-30): a fresh browser process (new Playwright launch, new login,
        same session cookie) succeeded on WO-PO/000080935's checklist step on the FIRST attempt,
        immediately after 6 consecutive in-page-retry failures in the previous, long-lived session.
        This wrapper reproduces that exact recovery programmatically instead of requiring a human to
        relaunch a script.

        Each attempt gets a FRESH quotation/order lookup (Schedule Board's grid state, not just the
        employee checklist, could differ after a reset) -- _schedule_stage_c_attempt re-navigates
        and re-filters from scratch every time it's called, so this is safe to call repeatedly.
        """
        if _dry_guard(f"schedule Stage C for {payload.wo_po_number}"):
            return True
        # 2, not 3: each attempt (including its own internal retries) runs ~2-4 minutes, and a hard
        # reset adds another ~15-30s for the browser relaunch + login. This shares SYNERGIX_WO_TIMEOUT_S
        # (900s) with Stage A/B/B.5 (already run) and Stage D (still to come) -- 2 attempts leaves
        # comfortable margin; 3 risked leaving too little for Stage D on a slow day.
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                if await self._schedule_stage_c_attempt(payload):
                    return True
            except Exception:
                logger.exception("Stage C attempt %d/%d errored for %s", attempt, max_attempts,
                                  payload.wo_po_number)
            if attempt < max_attempts:
                logger.warning("Stage C attempt %d/%d failed for %s -- hard-resetting the browser "
                                "and retrying the whole stage from scratch", attempt, max_attempts,
                                payload.wo_po_number)
                try:
                    await self._hard_reset_browser()
                except Exception:
                    logger.exception("Stage C: hard reset itself failed for %s -- aborting retries",
                                      payload.wo_po_number)
                    break
        logger.warning("Stage C: all %d attempts failed for %s -- this needs a human to finish "
                        "Stage C manually in Synergix", max_attempts, payload.wo_po_number)
        return False

    async def _schedule_stage_c_attempt(self, payload: WOPayload) -> bool:
        """One end-to-end attempt at Stage C -- see _schedule_stage_c (the retry wrapper that calls
        this) for why failures here are retried with a full browser reset rather than patched
        per-step. Find the WO's Service Order on Schedule Board, assign ASSIGNED_WORK_TEAM + a
        To-Pair-With council (INFIGO/ECOCARE) at the WO's job date, and submit.

        REWRITTEN 2026-08-31 -- see docs/synergix_workflow.md "Stage C, frame-by-frame from JBTC WO
        Synergix.mp4" for the full corrected narrative and open assumptions pending client
        confirmation. Prior versions of this method (through 2026-08-30) assigned an EMPLOYEE
        (`SCHEDULE_EMPLOYEE`, "TAN WEI YING") via a Filter checklist -- that was the wrong target,
        found by extracting and reviewing the client's own walkthrough video frame-by-frame. The
        real, successfully-submitted flow assigns a WORK TEAM via the Event Details popup's own
        "Assigned" dropdown, paired with INFIGO or ECOCARE via a separate "To Pair With" checklist.

        Discovered live (2026-08-25): a confirmed Variation Order (Stage B.5) creates a Service Order
        that appears in Schedule Board's "Unscheduled Service Orders" grid. Non-obvious parts that
        still hold after the rewrite:
          1. The order's ROW must be selected first (click it) -- clicking the calendar with no order
             selected does nothing at all, no error, no popup.
          2. The actual click target for "add an event" is a fully-transparent overlay button
             (`[id*="newEventButton"]`), NOT the visible cell div underneath it -- the click failed
             with a "<td> intercepts pointer events" error until this was found, because Playwright
             was (correctly) reporting that invisible overlay as the interceptor.
          3. After the popup's own checkmark, a SECOND, separate "Submit" action appears on the
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

        # REVERTED (2026-08-31, same day as added): the JBTC workflow doc's Stage C Step 1 shows
        # the human collapsing the right-side Customer Info panel first (via a layout toggle,
        # a.ui-layout-unit-header-icon, a right-pointing triangle -- NOT the Unscheduled Service
        # Orders panel's own titlebar minimize icon). Adding this step here reproduced a NEW
        # click-intercept failure on the Employee toggle itself (its own parent buttonset container
        # started intercepting pointer events) that had never once occurred in ~15 prior live test
        # runs without it. Not confirmed as definitely caused by the collapse (could be coincidental
        # timing/layout-reflow flakiness), but reverted out of caution rather than risk stacking a
        # new regression on top of the still-unresolved dialog-mask blocker below. If revisited,
        # test the collapse step in isolation first, independent of the rest of Stage C.

        wo_bare = wo.replace("WO-PO/", "")

        async def _filter_orders_by_wo() -> None:
            # Filter the grid's own Enquiry/Subject column by the bare WO number -- the same text
            # Stage B wrote into the quotation's Subject, carried through to the Service Order.
            # Confirmed live (2026-08-28) on WO-PO/000080788: this alone narrows the grid to exactly
            # the one target row, at the grid's default page size.
            #
            # Superseded by this: filtering by Customer (council name) instead, which needed a
            # second fix to also raise the grid's page size, since a council with more than 5
            # unscheduled orders otherwise hides a freshly-created one (it sorts onto a later page,
            # never page 1). Confirmed live (2026-08-28) on the SAME WO: raising the page size to 200
            # then broke a LATER step in this same method -- the order grid became tall enough that
            # its own paginator footer visually overlapped the calendar/Employee-filter pane below
            # it, and the increased DOM size also seemed to slow the ajax that populates the Employee
            # checklist, which then failed even after a full re-navigation retried the same approach.
            # Filtering by WO number avoids the whole page-size problem instead of working around it.
            header = page.locator("th:visible", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill(wo_bare)
            await filter_input.press("Enter")
            # Widened 3000 -> 5000 (2026-09-01) to match the user's literal instruction: "filter for
            # WO first, wait 5s" -- see the fuller comment on the fixed-wait pattern after row select.
            await page.wait_for_timeout(5000)

        async def _wait_for_ajax_spinner(label: str, *, timeout_s: float = 15) -> None:
            # THE REAL FIX for the employee-checklist race (found live with the user, 2026-08-31):
            # Synergix has a genuine, visible global ajax indicator -- <img class="js-ajax-spinner">
            # in the page footer, id like "j_idt7501:j_idt7503", display:none when idle -- that the
            # user demonstrated live: click the order row -> watch the spinner run -> wait for it to
            # finish -> THEN click the Employee toggle -> watch it run again -> wait for it to finish
            # -> the checklist is populated. Every previous "fix" in this method (real mouse clicks,
            # Clear All, hard resets, re-navigation) was working around this same root cause: fixed
            # `wait_for_timeout()` calls only guess how long the server's ajax response takes, and a
            # click issued before the PREVIOUS click's ajax has actually finished lands on a page
            # still mid-update. Confirmed live: waiting for this exact spinner's visible->hidden
            # cycle after each click reproduced a populated checklist 4/4 times in isolated testing,
            # including on a WO where the checklist didn't appear until the check after the SECOND
            # (Employee) click -- so this is called after every click in the sequence below, not
            # just once.
            spinner = page.locator("img.js-ajax-spinner").first
            became_visible = False
            for _ in range(15):  # ~3s grace period to catch the spinner appearing at all
                if await spinner.is_visible():
                    became_visible = True
                    break
                await page.wait_for_timeout(200)
            if not became_visible:
                # No ajax observed for this click -- nothing to wait out. Not necessarily an error:
                # some clicks (e.g. a toggle that was already in the target state) are genuine no-ops.
                return
            deadline_polls = int(timeout_s / 0.2)
            for _ in range(deadline_polls):
                if not await spinner.is_visible():
                    return
                await page.wait_for_timeout(200)
            logger.warning("Stage C: ajax spinner still visible %.1fs after %s -- proceeding anyway",
                            timeout_s, label)

        async def _close_stray_event_dialog() -> None:
            # Found live (2026-09-01) on WO-PO/99999live1, via a failure screenshot: a SECOND, STRAY
            # "Event Details" popup was sitting open on top of the real Order Details panel right
            # when Submit's click kept failing -- From/To 31/08/2026, a date that didn't even match
            # this order's own Schedule (03/03/2026 underneath) -- almost certainly a leftover dialog
            # from an EARLIER synthetic test WO/calendar row on the same cluttered Schedule Board
            # (today's session has ~20+ leftover 9999xxx test WOs visible in the same calendar view)
            # that never got dismissed. This dialog visually covers the whole page, including the
            # real Submit button, which is exactly why Playwright's click kept timing out with
            # "element is not enabled" even though a human's manual click on the SAME page worked
            # instantly -- the human's click likely landed on or dismissed this stray dialog first.
            # Close ANY visible "Event Details" dialog before ever attempting Submit, regardless of
            # which order it's for -- there should be none open at this point in a clean flow.
            stray = page.locator('[role="dialog"]:has-text("Event Details")').locator("visible=true")
            count = await stray.count()
            for i in range(count):
                dlg = stray.nth(i)
                close_btn = dlg.locator('a.ui-dialog-titlebar-close[aria-label="Close"]').locator(
                    "visible=true"
                ).first
                if await close_btn.count():
                    logger.warning("Stage C: closing a stray 'Event Details' dialog before Submit "
                                    "for %s -- this should not normally be open at this point", wo)
                    try:
                        await close_btn.click(timeout=5000)
                        await page.wait_for_timeout(1000)
                    except Exception:
                        pass

        async def _submit_and_recover(submit_btn) -> str:
            # Confirmed live (2026-09-01) by the user driving this by hand at the exact stuck point
            # this method's own automation had hit: the "no newEventButton found" symptom was NOT a
            # real bug -- it was Synergix's own Employee checklist/calendar rendering being
            # intermittently slow ("an intermittent UI fault on their end"), which eventually
            # resolved on its own after a re-click, exactly the "click too fast, page hadn't loaded"
            # pattern already documented elsewhere in this file. Separately, the user found clicking
            # Submit too early (before the schedule had genuinely attached in that view) surfaces a
            # real, informative Synergix error: "SV9317: Service order: SV0000XXXX requires schedules
            # to be set" -- a toast that appears top-right, SOMETIMES PARTIALLY OFF-SCREEN, easy to
            # miss in a screenshot. The correct recovery, demonstrated live: re-click the calendar
            # (forces a re-render showing the schedule now correctly attached, visible as a light-
            # green block), then retry Submit -- which then succeeds. Returns "ok", "sv9317_retried",
            # or "failed".
            await self._click_when_clear(submit_btn, timeout_ms=30000, overlay_wait_ms=30000)
            await page.wait_for_timeout(1500)
            yes_btn = page.get_by_role("button", name="Yes").locator("visible=true")
            if await yes_btn.count():
                await yes_btn.first.click(timeout=10000)
            await page.wait_for_timeout(3000)
            sv9317_shown = await page.locator("text=/SV9317/").locator("visible=true").count() > 0
            if not sv9317_shown:
                return "ok"
            logger.warning("Stage C: SV9317 (schedule not yet set) after Submit for %s -- "
                            "re-clicking the calendar to force a refresh, then retrying Submit", wo)
            # Re-click the calendar area to force PrimeFaces to re-render with the now-attached
            # schedule visible (confirmed live: this is what made the light-green block appear).
            calendar_area = page.locator("text=Schedule Calendar").locator("visible=true").first
            if await calendar_area.count():
                await calendar_area.click(timeout=5000)
            await page.wait_for_timeout(5000)
            submit_btn_retry = page.locator('button:has(span.fa-vote-yea)').locator("visible=true").first
            if not await submit_btn_retry.count():
                return "failed"
            await self._click_when_clear(submit_btn_retry, timeout_ms=30000, overlay_wait_ms=30000)
            await page.wait_for_timeout(1500)
            yes_btn_retry = page.get_by_role("button", name="Yes").locator("visible=true")
            if await yes_btn_retry.count():
                await yes_btn_retry.first.click(timeout=10000)
            await page.wait_for_timeout(3000)
            still_sv9317 = await page.locator("text=/SV9317/").locator("visible=true").count() > 0
            return "failed" if still_sv9317 else "sv9317_retried"

        await _filter_orders_by_wo()

        # Confirmed live (2026-08-27) on WO-PO/000077662: the row genuinely existed (verified
        # manually moments later, same filter) but was not yet rendered at the 3s mark -- poll a
        # few more times before giving up, same flicker-tolerance pattern used throughout this file.
        order_row = page.locator("tr", has_text=wo_bare).locator("visible=true").first
        for _ in range(4):  # ~6s more on top of the initial 3s wait
            if await order_row.count():
                break
            await page.wait_for_timeout(1500)
        if not await order_row.count():
            logger.warning("Stage C: no Schedule Board order found for %s", wo)
            return False
        order_no_cell = order_row.locator("td").nth(1)
        order_no = (await order_no_cell.inner_text()).strip()
        # Click the row's own CHECKBOX specifically (2026-09-01), not the row body -- the user
        # directly observed a live run and corrected this: "i manually click on the green tick to
        # select the correct order." This method already uses the exact same .ui-chkbox-box pattern
        # elsewhere (the To Pair With tick) for the same PrimeFaces checkbox structure -- a native
        # <input type="checkbox"> wrapped in a hidden-accessible div, with the sibling .ui-chkbox-box
        # div as the real clickable element. Falls back to clicking the row itself if no checkbox is
        # found, so this doesn't regress on a page layout without one.
        row_checkbox = order_row.locator(".ui-chkbox-box").locator("visible=true").first
        if await row_checkbox.count():
            await self._click_when_clear(row_checkbox, timeout_ms=10000)
        else:
            await order_row.click(timeout=10000)
        await _wait_for_ajax_spinner("row selection")
        # Fixed 5s wait after every click (2026-09-01), per the user's explicit, literal instruction
        # after re-grounding this whole method in JBTC WO Synergix.mp4 6:00-8:00: "every click you
        # must wait 5s... click on WO, wait 5s, select WO, wait 5s, toggle to employee, wait 5s...".
        # This is IN ADDITION TO the real ajax-spinner wait above, not a replacement for it -- the
        # spinner wait already proves the server's own response has landed; this fixed wait mirrors
        # the further, real elapsed time a human naturally takes (reading the screen, moving the
        # mouse) between actions, which the video itself shows matters (e.g. at least ~20s of real
        # gap between Confirm closing and Submit being clicked, per the frame-by-frame record in
        # docs/synergix_workflow.md) and which several of today's bugs (5, 6) trace back to skipping.
        await page.wait_for_timeout(5000)

        # CRITICAL FIX (2026-09-01), caught directly by the user from a live screenshot: a genuinely
        # FRESH WO's calendar is white/empty (matching the video's own 6:00-8:00 starting state --
        # "a fresh work order is supposed to be white and calendar no input"). But on a RETRY (this
        # method's own outer wrapper, _schedule_stage_c, hard-resets and calls this method again from
        # scratch after any failure), the calendar can ALREADY show a green "Schedule of Current
        # Service Order" block from attempt 1's Confirm having genuinely succeeded server-side even
        # though attempt 1 then failed at the LATER Submit step. Every previous version of this
        # method never checked for this and just re-ran the full row-select -> Employee toggle ->
        # newEventButton -> Event Details -> Confirm sequence again -- clicking Confirm a second time
        # on an already-booked date/team/pairing collides with itself and raises repeated SV9104
        # "you can only book one task on the same Timeslot" errors (confirmed live via a screenshot
        # showing four stacked SV9104 toasts, all for the same date, after a retry). The user's own
        # framing: "you keep repeating on a logged (look at the green inputs in the calendars) means
        # you already have valid orders logged and saved all you have to do is submit when this
        # happens. thats why the same WO doesn't work repeatedly because theres a saved order
        # unsubmitted."
        #
        # RETRACTED-AND-FIXED same day: the first version of this check searched for "Schedule" +
        # a team name ANYWHERE visible on the page. The user caught this live, watching a run land
        # on a genuinely NEW Service Order (SV00008932) but still trigger the skip-to-Submit path --
        # because the Schedule Calendar shows a WHOLE WEEK of entries at once, and today's ~20+
        # leftover synthetic test WOs (all in the 9999xxx range) cluttered nearby cells with their
        # own real "800SUPER"/"INFIGO"/"ECOCARE" schedule text, which the old, unscoped check happily
        # matched even though none of it belonged to the CURRENT order. Scoped correctly now: only
        # the Order Details panel's OWN "Schedule" section, found via order_no (e.g. "SV00008932")
        # as an anchor, counts -- not any text visible elsewhere on the page.
        order_details_panel = page.locator(f'text=Order Details[{order_no}]').locator(
            "visible=true"
        ).first
        already_scheduled = False
        if await order_details_panel.count():
            panel_container = order_details_panel.locator(
                "xpath=ancestor::div[contains(@class,'ui-panel') or contains(@class,'ui-widget')][1]"
            ).first
            if await panel_container.count():
                already_scheduled = await panel_container.locator("text=Schedule").locator(
                    "visible=true"
                ).count() > 0 and (
                    await panel_container.locator("text=800SUPER").locator("visible=true").count() > 0
                    or await panel_container.locator("text=INFIGO").locator("visible=true").count() > 0
                    or await panel_container.locator("text=ECOCARE").locator("visible=true").count() > 0
                )
        if already_scheduled:
            logger.info("Stage C: %s already shows a committed Schedule (green calendar entry from "
                        "a prior attempt) -- skipping straight to Submit, not re-running Confirm", wo)
            await _close_stray_event_dialog()
            submit_btn = page.locator('button:has(span.fa-vote-yea)').locator("visible=true").first
            if not await submit_btn.count():
                logger.warning("Stage C: already-scheduled but no Submit button found for %s", wo)
                return False
            try:
                submit_outcome = await _submit_and_recover(submit_btn)
            except Exception as exc:
                logger.exception("Stage C: Submit click failed on already-scheduled order for %s (%s)",
                                  wo, exc)
                await self._screenshot(f"stage_c_submit_disabled_{wo.replace('/', '-')}")
                return False
            if submit_outcome == "failed":
                logger.warning("Stage C: Submit still failing (SV9317 or no button) after retry for "
                                "%s (already-scheduled path)", wo)
                await self._screenshot(f"stage_c_submit_disabled_{wo.replace('/', '-')}")
                return False
            await page.wait_for_timeout(20000)
            upcoming = await page.locator("text=Upcoming Service").locator("visible=true").count()
            confirmed = (upcoming
                         and await page.get_by_text(order_no, exact=False).locator("visible=true").count() > 0)
            if not confirmed:
                logger.warning("Stage C: %s (%s) not found in Upcoming Service after submit "
                                "(already-scheduled path)", wo, order_no)
                return False
            logger.info("Stage C submitted (already-scheduled path) for %s (%s)", wo, order_no)
            return True

        async def _mouse_click_ui_button(label: str) -> bool:
            # Confirmed live (2026-08-28): a JS-dispatched btn.click() on this toggle is a no-op as
            # far as the server is concerned -- checkbox count on the page measured 0 change after
            # several such clicks in a diagnostic session, vs. a real page.mouse.click() at the same
            # button's coordinates visibly opening the Employee Job Type filter panel within ~1.5s.
            # This is very likely why this toggle (and the Filter link below) had been flaky across
            # multiple prior sessions despite "working" moments earlier -- a synthetic click can
            # satisfy Playwright's own success check (the CSS class DOES flip client-side) while
            # never reaching whatever handler actually triggers PrimeFaces's ajax call.
            rect = await page.evaluate(
                """(label) => {
                    const btn = [...document.querySelectorAll('div.ui-button')]
                      .find(b => b.textContent.trim() === label && b.getBoundingClientRect().width > 0);
                    if (!btn) return null;
                    const r = btn.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""",
                label,
            )
            if not rect:
                return False
            await page.mouse.click(rect["x"], rect["y"])
            return True

        # REWRITTEN 2026-08-31: everything from here to the Event Details Confirm below used to
        # search for SCHEDULE_EMPLOYEE ("TAN WEI YING") in the Employee Filter checklist and open
        # her calendar row specifically. That entire approach targeted the WRONG field -- frame-by-
        # frame analysis of the client's own walkthrough video (JBTC WO Synergix.mp4, see
        # docs/synergix_workflow.md "Stage C, frame-by-frame") proved the real, successfully-
        # submitted flow assigns a WORK TEAM via the Event Details popup's own "Assigned" dropdown
        # (defaulted to ASSIGNED_WORK_TEAM = "800SUPER" in the one recorded example) plus a
        # "To Pair With" checklist tick (INFIGO or ECOCARE, by the same alphabetic/numeric Job Sheet
        # rule as resolve_project_code() already uses for Project Site) -- TAN WEI YING genuinely
        # appears on that same "To Pair With" checklist, as one of 12 options, which is almost
        # certainly why every session before this one assumed she was the assignee.
        #
        # The video's own recording never explicitly clicked "Employee" vs "Work Team" as a
        # deliberate binary choice the way earlier sessions assumed -- it toggled to Employee, the
        # calendar populated with a real (paginated) list, and clicking into the calendar body opened
        # Event Details directly. This code keeps the Employee toggle click (proven harmless, and
        # matches the doc's own Step 3) but no longer searches for a specific person's row -- it
        # opens Event Details via the FIRST available newEventButton-style overlay found in the
        # visible calendar, since Stage C no longer needs to target a specific individual's slot.
        # ASSUMPTION, not directly observed on video (the recording's 1fps sampling skipped the exact
        # moment of this click): if this lands on the wrong row/date in practice, the fallback is to
        # scope by the WO's job date column instead of taking the first button found -- flagged here
        # for whoever revisits this once the client confirms the open questions in the doc.
        employee_active = False
        for _ in range(3):
            await _mouse_click_ui_button("Employee")
            await _wait_for_ajax_spinner("Employee toggle")
            await page.wait_for_timeout(5000)  # fixed 5s wait after every click, see row-select comment above
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

        # CRITICAL FIX (2026-09-01), found live with the user driving a handoff by hand: the
        # calendar has TWO distinct view modes -- a weekly grid (date columns like "30 Sun Aug
        # 2026", "31 Mon Aug 2026"...) and an hourly "Time Calendar" (columns 08:00-22:00 for a
        # single day). The `[id*="newEventButton"]` clickable overlay this method searches for ONLY
        # exists in the WEEKLY grid view -- clicking a blank cell in the weekly view opens Event
        # Details correctly (confirmed live, user's own screenshot); the hourly Time Calendar view
        # has no such overlay at all, which is the real reason multiple live runs today found "no
        # newEventButton" even on a correctly-dated, genuinely-open calendar (WO-PO/910001912) --
        # Synergix was rendering the hourly view, not a rendering lag or a missing element. This
        # also retroactively explains why the "Week of" date field search kept failing: the hourly
        # view's date field has a different structure than the weekly view's. Explicitly click
        # "Switch to Time Calendar"'s own counterpart if that link is showing "Switch to Time
        # Calendar" (meaning we're currently on the weekly view already, nothing to do) vs. showing
        # something like "Switch to Week View" (meaning we're on the hourly view and need to switch
        # back) -- checked by the link's OWN current text, not assumed.
        switch_link_text = await page.evaluate(
            """() => {
                const link = [...document.querySelectorAll('a, span, div')]
                    .find(el => el.children.length === 0 &&
                                (el.textContent || '').trim().startsWith('Switch to'));
                return link ? link.textContent.trim() : null;
            }"""
        )
        if switch_link_text and "time calendar" not in switch_link_text.lower():
            # Currently on the hourly Time Calendar view (the link offers to switch AWAY from it,
            # i.e. it reads something other than "Switch to Time Calendar") -- click it to get back
            # to the weekly grid view where newEventButton actually exists.
            logger.warning("Stage C: calendar is on the hourly Time Calendar view (%r) -- switching "
                            "back to the weekly grid view where newEventButton actually exists",
                            switch_link_text)
            switch_link = page.get_by_text(switch_link_text, exact=True).locator("visible=true").first
            if await switch_link.count():
                await switch_link.click(timeout=5000)
                await _wait_for_ajax_spinner("switch to weekly view")
                await page.wait_for_timeout(3000)

        # CRITICAL FIX (2026-09-01), found live on WO-PO/910001894: the Schedule Calendar does NOT
        # automatically navigate to the WO's own job date just because the row is selected -- it
        # stays on whatever date it currently happens to be showing (today, by default). A failure
        # screenshot showed the calendar sitting on today's date (01/09/2026, NOT this WO's job date
        # of 24/06/2021) with every single hourly cell under "800SUPER" reading "Taken by day tasks"
        # -- genuinely, correctly unavailable on THAT date, not a rendering bug at all. The earlier
        # "date collision" fix (widening the test-date range) does not help here since the collision
        # was against TODAY's date, which every test run implicitly starts on regardless of its own
        # job_date. Explicitly navigate the calendar's own "Week of" date field to the WO's job date
        # before ever searching for a newEventButton.
        job_date_calendar_str = payload.job_date.strftime("%d/%m/%Y")
        # SIMPLIFIED (2026-09-01), after two wrong guesses at the "Week of" label's DOM relationship
        # to its date input (searching for literal "Week of" text failed live twice -- a screenshot
        # showed this calendar view doesn't even display that label text, just the date input itself
        # next to the << < > >> navigation arrows and a calendar icon). Stop trying to find the field
        # via label text at all -- just find any visible text input, within the Schedule Calendar
        # section specifically, whose CURRENT VALUE already looks like a dd/mm/yyyy date. This is
        # exactly what a human does: they don't look for a label, they see the date box and click it.
        week_of_input_id = await page.evaluate(
            """() => {
                const section = [...document.querySelectorAll('*')]
                    .find(el => (el.textContent || '').includes('Schedule Calendar'))
                    ?.closest('div');
                const scope = section || document;
                const dateRe = /^\\d{2}\\/\\d{2}\\/\\d{4}$/;
                const inputs = [...scope.querySelectorAll('input[type="text"]')]
                    .filter(i => i.offsetParent !== null && dateRe.test((i.value || '').trim()));
                return inputs.length ? inputs[0].id : null;
            }"""
        )
        week_of_input = (
            page.locator(f'[id="{week_of_input_id}"]').locator("visible=true").first
            if week_of_input_id else page.locator("__never_match__")
        )
        if await week_of_input.count():
            try:
                await week_of_input.click(timeout=5000)
                await week_of_input.fill(job_date_calendar_str)
                await week_of_input.press("Enter")
                await week_of_input.evaluate(
                    "(el) => el.dispatchEvent(new Event('change', {bubbles: true}))"
                )
                await _wait_for_ajax_spinner("calendar date navigation")
                await page.wait_for_timeout(5000)
                logger.info("Stage C: navigated calendar to job date %s for %s",
                            job_date_calendar_str, wo)
            except Exception:
                logger.warning("Stage C: could not navigate calendar to job date %s for %s -- "
                                "proceeding with whatever date it's currently showing",
                                job_date_calendar_str, wo)
        else:
            logger.warning("Stage C: could not find the 'Week of' calendar date field for %s -- "
                            "proceeding with whatever date it's currently showing", wo)

        # Poll for the calendar to actually render at least one newEventButton-style overlay cell --
        # same ajax-timing tolerance pattern as every other wait in this file (see
        # _wait_for_ajax_spinner's docstring for why fixed timeouts alone are not reliable here).
        async def _poll_for_new_event_button() -> str | None:
            for _ in range(20):  # ~10s
                found_id = await page.evaluate(
                    """() => {
                        const btn = document.querySelector('[id*="newEventButton"]');
                        return btn ? btn.id : null;
                    }"""
                )
                if found_id:
                    return found_id
                await page.wait_for_timeout(500)
            return None

        new_event_btn_id = await _poll_for_new_event_button()
        if not new_event_btn_id:
            # Confirmed live (2026-09-01) by the user driving this exact stuck point by hand: this is
            # NOT a real bug -- it's an intermittent Synergix-side render lag ("an intermittent UI
            # fault on their end") that resolved after a re-click of the row and Employee toggle. The
            # original 10s poll above assumed a fixed, short ajax delay; on a genuinely slow render it
            # never recovers on its own. Re-click the row + Employee toggle once (matching exactly
            # what the user did) and poll again before giving up -- much cheaper than this method's
            # own outer hard-reset-and-retry-everything wrapper, and directly addresses the actual
            # cause instead of restarting the whole Stage C attempt from scratch.
            logger.warning("Stage C: no 'add event' cell found on first attempt for %s -- re-clicking "
                            "row + Employee toggle (Synergix's own calendar render can lag "
                            "intermittently) before giving up", wo)
            row_checkbox_retry = order_row.locator(".ui-chkbox-box").locator("visible=true").first
            if await row_checkbox_retry.count():
                await self._click_when_clear(row_checkbox_retry, timeout_ms=10000)
            else:
                await order_row.click(timeout=10000)
            await page.wait_for_timeout(5000)
            await _mouse_click_ui_button("Employee")
            await _wait_for_ajax_spinner("Employee toggle retry")
            await page.wait_for_timeout(5000)
            new_event_btn_id = await _poll_for_new_event_button()
        if not new_event_btn_id:
            # LAST RESORT (2026-09-01): even a correctly-dated, weekly-view calendar with a
            # genuinely open (unbooked) grid can still have no `[id*="newEventButton"]` element
            # findable via querySelector -- confirmed live (WO-PO/910002207) across two full attempts
            # (including a hard reset) with the calendar visually correct both times. The user's own
            # live click, directly into a blank cell in the weekly grid, opened Event Details
            # correctly on the first try -- meaning the overlay likely IS there but is not reliably
            # queryable by this id pattern (e.g. it may only fully attach on :hover, or the id
            # substring assumption is incomplete). Fall back to clicking the actual visible grid cell
            # directly by coordinates, exactly replicating the user's own manual action, instead of
            # querying for an overlay element that may not always be findable this way.
            logger.warning("Stage C: no newEventButton element found for %s after retry -- falling "
                            "back to clicking a blank grid cell directly by coordinates", wo)
            cell_rect = await page.evaluate(
                """() => {
                    const header = [...document.querySelectorAll('th, td')]
                        .find(el => (el.textContent || '').trim() === 'Service Personnel');
                    if (!header) return null;
                    const table = header.closest('table');
                    if (!table) return null;
                    const rows = [...table.querySelectorAll('tbody tr')];
                    for (const row of rows) {
                        const cells = [...row.querySelectorAll('td')];
                        // Skip the first cell (the Service Personnel name column itself); look for
                        // the first genuinely empty date/time cell in this row.
                        for (const cell of cells.slice(1)) {
                            const text = (cell.textContent || '').trim();
                            if (text === '') {
                                const r = cell.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                                }
                            }
                        }
                    }
                    return null;
                }"""
            )
            if cell_rect:
                await page.mouse.click(cell_rect["x"], cell_rect["y"])
                await page.wait_for_timeout(3000)
                # Re-check: did this direct click open Event Details, even without ever finding a
                # newEventButton id? If so, skip straight past the newEventButton click below.
                event_dialog_opened_directly = await page.evaluate(
                    """() => [...document.querySelectorAll('.ui-dialog')].some(d => {
                        const r = d.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 &&
                               d.querySelector('.ui-dialog-title')?.textContent?.trim() === 'Event Details';
                    })"""
                )
                if event_dialog_opened_directly:
                    logger.info("Stage C: direct cell click opened Event Details for %s "
                                "(newEventButton element was never found, but the click worked "
                                "anyway)", wo)
                    new_event_btn_id = "DIRECT_CLICK_ALREADY_OPENED"
        if not new_event_btn_id:
            logger.warning("Stage C: no 'add event' cell found on the calendar for %s", wo)
            await self._screenshot(f"stage_c_no_calendar_row_{wo.replace('/', '-')}")
            return False
        if new_event_btn_id != "DIRECT_CLICK_ALREADY_OPENED":
            # Confirmed live (2026-08-28, still applicable): document.elementFromPoint at this
            # button's own (scrolled-into-view) coordinates resolved to a <div class="blockUI
            # blockOverlay">, not the button -- a pending-ajax "please wait" mask was still covering
            # it. Playwright's click sometimes reports success anyway while the click itself lands
            # on the mask, opening nothing. This is exactly what _click_when_clear exists for
            # elsewhere in this file -- reuse it here.
            new_event_btn = page.locator(f'[id="{new_event_btn_id}"]')
            try:
                await new_event_btn.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass  # best-effort; the click below will raise its own clear error if this matters
            try:
                await self._click_when_clear(new_event_btn, timeout_ms=15000)
            except Exception:
                logger.exception("Stage C: newEventButton click failed for %s (id=%s)", wo,
                                  new_event_btn_id)
                await self._screenshot(f"stage_c_newevent_click_failed_{wo.replace('/', '-')}")
                raise
            await page.wait_for_timeout(5000)  # fixed 5s wait after every click, see row-select comment above

        # Confirmed live (2026-08-28, still applicable): the "Event Details" dialog takes several
        # seconds to actually render (ajax "onstart" fires immediately but the dialog itself lags) --
        # poll instead of guessing a fixed duration.
        event_dialog_visible = False
        for _ in range(20):  # ~10s
            event_dialog_visible = await page.evaluate(
                """() => [...document.querySelectorAll('.ui-dialog')].some(d => {
                    const r = d.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 &&
                           d.querySelector('.ui-dialog-title')?.textContent?.trim() === 'Event Details';
                })"""
            )
            if event_dialog_visible:
                break
            await page.wait_for_timeout(500)
        if not event_dialog_visible:
            logger.warning("Stage C: Event Details dialog never appeared for %s", wo)
            await self._screenshot(f"stage_c_no_event_dialog_{wo.replace('/', '-')}")
            return False

        event_dialog = page.locator('[role="dialog"]:has-text("Event Details")').locator("visible=true").first

        # "Assigned" dropdown -- per the video, defaults to ASSIGNED_WORK_TEAM already; only change
        # it if it's showing something else, so a differently-defaulted popup doesn't get silently
        # overwritten to the wrong team. Confirmed structure: a PrimeFaces selectonemenu, same
        # pattern used for Customer/Salesperson/Project Site in Stage B (_select_autocomplete_row is
        # NOT reused here since Assigned is a plain dropdown, not a live-search autocomplete).
        assigned_label = event_dialog.locator(".ui-selectonemenu-label").first
        assigned_text = (await assigned_label.inner_text()).strip() if await assigned_label.count() else ""
        if assigned_text and assigned_text != ASSIGNED_WORK_TEAM:
            logger.warning("Stage C: Event Details 'Assigned' defaulted to %r, not the expected %r, "
                            "for %s -- leaving as-is rather than guessing which dropdown option to "
                            "pick (ASSIGNED_WORK_TEAM is an unconfirmed assumption, see "
                            "docs/synergix_workflow.md)", assigned_text, ASSIGNED_WORK_TEAM, wo)

        # "To Pair With" checklist -- tick INFIGO or ECOCARE by the SAME alphabetic/numeric Job Sheet
        # rule resolve_project_code() already uses for Project Site (Stage B). Only one example seen
        # on video (Job Sheet A25-01086, alphabetic -> Infigo, ticked INFIGO) -- ASSUMING this rule
        # generalizes rather than being independently confirmed for Ecocare/numeric-prefix WOs.
        pair_with_label = "INFIGO" if (payload.job_sheet_number or "").strip()[:1].isalpha() else "ECOCARE"
        # Confirmed live (2026-08-31), via a raw DOM dump: this checklist's real <input
        # type="checkbox"> is wrapped in a div.ui-helper-hidden-accessible (PrimeFaces' standard
        # a11y pattern, same as the retired employee-checklist code found for its own checkbox) --
        # the input itself is not interactable; the VISIBLE, actually-clickable element is the
        # sibling .ui-chkbox-box div. Found by the checkbox's own `value` attribute (which encodes
        # the option, e.g. "...oa_code=INFIGO;..."), not by label text, since this popup's
        # label/input pairing structure was not confirmed to use a `for` attribute.
        pair_checkbox_id = await event_dialog.evaluate(
            """(dialog, label) => {
                const input = [...dialog.querySelectorAll('input[type=checkbox]')]
                  .find(i => (i.value || '').includes(`oa_code=${label}`));
                return input ? input.id : null;
            }""",
            pair_with_label,
        )
        if not pair_checkbox_id:
            logger.warning("Stage C: could not find the %r 'To Pair With' checkbox for %s",
                            pair_with_label, wo)
            await self._screenshot(f"stage_c_no_pair_with_{wo.replace('/', '-')}")
            close_btn = event_dialog.locator('a.ui-dialog-titlebar-close[aria-label="Close"]').locator("visible=true").first
            if await close_btn.count():
                await close_btn.click(timeout=5000)
            return False
        pair_checkbox_box = page.locator(f'input[id="{pair_checkbox_id}"]').locator(
            "xpath=ancestor::div[contains(@class,'ui-chkbox')][1]//div[contains(@class,'ui-chkbox-box')]"
        )
        await self._click_when_clear(pair_checkbox_box, timeout_ms=10000)
        await _wait_for_ajax_spinner("To Pair With tick")
        await page.wait_for_timeout(5000)  # fixed 5s wait after every click, see row-select comment above
        # Verify the tick actually landed -- confirmed elsewhere in this file (the retired employee-
        # checklist code) that a click can report success while the underlying checkbox state
        # doesn't actually change.
        pair_checked = await page.evaluate(
            "(id) => document.getElementById(id)?.checked === true", pair_checkbox_id
        )
        if not pair_checked:
            logger.warning("Stage C: clicking %r's checkbox did not actually check it for %s",
                            pair_with_label, wo)
            await self._screenshot(f"stage_c_pair_with_not_checked_{wo.replace('/', '-')}")
            return False

        # From/To dates -- per the video, both set to the WO Date (top of the WO PDF), same day for
        # both fields. Uses the same _fill_labeled_input helper as the retired employee-flow version
        # (unchanged field-lookup mechanics, just the same From/To labels on the same popup).
        job_date_str = payload.job_date.strftime("%d/%m/%Y")
        await self._fill_labeled_input("From", job_date_str)
        await page.wait_for_timeout(5000)  # fixed 5s wait after every click/fill, per the user's instruction
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(5000)
        await self._fill_labeled_input("To", job_date_str)
        await page.wait_for_timeout(5000)
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(5000)
        # Explicit "ensure correct data loaded" check (2026-09-01), per the user's instruction to
        # verify the calendar shows the correct date before proceeding, not just assume the fill
        # landed. Warn (don't fail outright) on a mismatch -- the fields are re-readable and the
        # existing _fill_labeled_input dispatches its own 'change' event, so a transient read race
        # here is more likely than a genuine stuck value.
        from_value = await self._read_labeled_value("From")
        to_value = await self._read_labeled_value("To")
        if job_date_str not in from_value or job_date_str not in to_value:
            logger.warning("Stage C: From/To read back as %r/%r after setting %r for %s -- "
                            "proceeding anyway, but this may indicate the date fill did not stick",
                            from_value, to_value, job_date_str, wo)
        # Remarks left blank -- per the video, the one confirmed successful example never filled
        # this field and Submit still succeeded. ASSUMPTION: optional, not independently confirmed
        # as intentional vs. an omission in that one recording.

        # Scoped to the Event Details dialog itself, not a bare button:has(span.fa-check) --
        # confirmed live (2026-08-26) that a generic fa-check search can resolve to an unrelated,
        # hidden confirm-dialog's own "Yes" button elsewhere on the page (id="j_idt969", a
        # PrimeFaces-generated id that recurs across different dialogs) and hang forever waiting for
        # it to become visible. role="dialog" + its own title text is a stable, structural anchor.
        confirm_btn = event_dialog.locator('button:has(span.fa-check)').first
        if not await confirm_btn.count():
            logger.warning("Stage C: no Event Details confirm button found for %s", wo)
            return False
        # _click_when_clear, not a raw click (2026-09-01): the From/To date fields immediately
        # before this now each dispatch their own 'change' event (added to make the datepicker
        # actually register -- see _fill_labeled_input's docstring) which fires a real PrimeFaces
        # ajax cascade of its own, on top of the To Pair With tick's ajax right before that. Two
        # live runs (WO-PO/99999m4, /99999m6) after that fix both saw this exact Confirm click
        # leave the Event Details dialog open (needing the retry-or-fail path below) where the
        # SAME flow had no such issue before the datepicker fix existed -- consistent with this
        # click now firing while one of those trailing ajax calls (and its blockUI overlay) is
        # still in flight. Every other click in this method already goes through
        # _click_when_clear for exactly this reason; this one was the one holdout.
        await self._click_when_clear(confirm_btn, timeout_ms=10000, overlay_wait_ms=15000)
        # Confirmed live (2026-08-31), by polling masks/dialog/spinner state every second: the
        # Confirm ajax call can genuinely take ~18s to complete (spinner visible continuously for
        # 18s, then dialog+mask cleared cleanly in the SAME second with no error) -- not a hang, a
        # real slow operation. Widened to 30s/20s to give real margin above the slowest observed case.
        await _wait_for_ajax_spinner("Event Details confirm", timeout_s=30)
        # Confirmed live (2026-08-26) that this click can leave the dialog (and its modal overlay)
        # still open, which then blocks every subsequent click with "<div class=ui-dialog-mask>
        # intercepts pointer events" for a full 30s timeout. Wait for it to actually close.
        #
        # IMPORTANT (2026-08-31): do NOT blindly re-click Confirm just because the dialog is still
        # showing after the wait -- confirmed live that the first click can genuinely succeed
        # server-side (the event visibly commits to the calendar) while the dialog itself is slow
        # to detach, and clicking Confirm a SECOND time on an already-scheduled event re-submits the
        # same booking and collides with itself, surfacing Synergix's own "SV9104: you can only book
        # one task on the same Timeslot" error -- a real, self-inflicted duplicate-submit bug, not a
        # Synergix flaw. Check the actual toast/error state before deciding whether a retry is safe.
        try:
            await event_dialog.wait_for(state="hidden", timeout=20000)
        except Exception:
            sv9104_shown = await page.locator("text=/SV9104/").locator("visible=true").count() > 0
            if sv9104_shown:
                logger.warning("Stage C: Confirm already succeeded for %s (SV9104 duplicate-booking "
                                "message from a stray double-submit) -- treating as scheduled, not "
                                "retrying", wo)
                close_btn = event_dialog.locator(
                    'a.ui-dialog-titlebar-close[aria-label="Close"]'
                ).locator("visible=true").first
                if await close_btn.count():
                    await close_btn.click(timeout=5000)
                    await page.wait_for_timeout(1000)
            else:
                logger.warning("Stage C: Event Details dialog still open after Confirm for %s -- "
                                "retrying the click", wo)
                if await confirm_btn.count():
                    await confirm_btn.click(timeout=10000)
                    await _wait_for_ajax_spinner("Event Details confirm retry", timeout_s=30)
                try:
                    await event_dialog.wait_for(state="hidden", timeout=20000)
                except Exception:
                    logger.warning("Stage C: Event Details dialog would not close for %s", wo)
                    await self._screenshot(f"stage_c_dialog_stuck_{wo.replace('/', '-')}")
                    return False

        # Confirmed live (2026-09-01) on WO-PO/99999m9 and /99999m14: even after the Event Details
        # dialog itself is confirmed hidden, the Order Details panel's own Submit button can still
        # take a further, SEPARATE ajax round-trip to actually refresh into its enabled state --
        # both runs' failure screenshots showed the Schedule section already correctly populated
        # (right team, right pairing, right date) while Submit's own `disabled` HTML attribute was
        # still literally present in the DOM (confirmed via a live discovery script's direct
        # attribute dump, not just is_enabled()) -- meaning the panel had not yet re-rendered from
        # its OWN trailing ajax call, a second, distinct cycle from the one already waited out above
        # for the dialog's own close. The user's own framing of this exact class of bug: "if you see
        # [stale data right after a popup opens], it means you clicked too fast and the UI hasn't
        # loaded the correct API response yet" -- the same principle applies here to READING Submit's
        # state, not just clicking. Give the page one more explicit ajax-spinner wait before ever
        # checking Submit, instead of relying solely on the polling loop below to eventually catch a
        # DOM update that may not have even been requested yet at the moment polling starts.
        await _wait_for_ajax_spinner("Order Details panel refresh after Confirm", timeout_s=15)
        # Fixed wait matching the video's own observed timing (2026-09-01), per the user's explicit
        # instruction to follow the recording literally rather than re-guess: the frame-by-frame
        # record in docs/synergix_workflow.md measured at least ~20 seconds of real elapsed time
        # between the Event Details Confirm closing (7:25.5) and Submit being clicked (7:45.0) in
        # the video -- a human's natural pause (reading the screen, moving the mouse) that this
        # method had never explicitly reproduced before today. This is on top of, not instead of,
        # the ajax-spinner wait above.
        await page.wait_for_timeout(20000)

        # CORRECTED (2026-08-31, frame-by-frame review of JBTC WO Synergix.mp4 at 7:00-7:52):
        # everything from here through Submit was WRONG in every prior version of this method. Every
        # earlier session (including the 2026-08-26/28/31 comments preserved in git blame for this
        # block) assumed Submit is gated on re-selecting the order row in the LEFT "Unscheduled
        # Service Orders" grid and ticking its own .ui-chkbox -- that a fresh checkbox tick there is
        # what flips the Order Details panel's Submit button from disabled to enabled. The video
        # proves this is false: after the Event Details popup's Confirm closes, the human does NOT
        # touch the left grid again at all (no re-filter, no row click, no checkbox click). The left
        # grid visibly resets to an unrelated, unfiltered page underneath -- and that reset is
        # harmless noise to be ignored, not a state to repair. The RIGHT-hand Order Details panel
        # (already open the whole time, e.g. "Order Details[SV00008851]") is what matters: it still
        # shows the "This Service Order has not been submitted" warning, and its own Submit
        # link/button (top-left of that panel's own header, same fa-vote-yea icon used at every
        # other stage-ending confirm in this app) is clicked DIRECTLY. Immediately after that click,
        # the warning disappears and a new "Schedule" section appears at the bottom of Order Details
        # showing the paired-with name (e.g. "INFIGO") -- the real, only success signal at this step.
        #
        # This also explains the entire `ui-dialog-mask`/`j_idt737_modal` blocker chased across
        # multiple sessions (see docs/synergix_workflow.md's retracted "session timeout" theory): the
        # blocked click was always on the LEFT grid's checkbox, which was never part of the real flow
        # to begin with -- of course it kept timing out, it was clicking into a stale, resetting grid
        # for no reason, while the real Submit button sat right there on the Order Details panel the
        # whole time, unblocked. No mask-clearing, no Escape, no re-click sequence was ever needed.
        await _close_stray_event_dialog()
        submit_btn = page.locator('button:has(span.fa-vote-yea)').locator("visible=true").first
        if not await submit_btn.count():
            logger.warning("Stage C: no Submit button found on Order Details for %s", wo)
            return False
        # CORRECTED (2026-09-01), in two steps:
        #
        # Step 1: every earlier version of this method GATED the click behind a manual
        # submit_btn.is_enabled() poll-then-give-up loop (5s, then widened to 30s), which kept
        # failing live (WO-PO/99999m9, m14) even when a screenshot proved the schedule had ALREADY
        # committed correctly (right team, pairing, date). The user corrected this directly from the
        # video (JBTC WO Synergix.mp4, 6:00-8:00): a human never explicitly checks whether Submit
        # reads "enabled" before clicking it -- they just click it once the date is set.
        #
        # Step 2 (this version): removing that manual check entirely and clicking immediately
        # (previous commit, c70ef8d) ALSO failed live on WO-PO/99999m17 -- Playwright's own click
        # genuinely refused, because the button's `disabled` HTML attribute was still truly present
        # at that exact instant (confirmed via the same screenshot pattern as m9/m14: correct
        # schedule, but this time the click itself threw rather than a manual check reading false).
        # The real lesson from the user's correction was narrower than "never wait" -- it was "don't
        # manually poll is_enabled() and BAIL if it's slow"; a human still implicitly waits (reading
        # the screen, moving the mouse) long enough for the button to become clickable before their
        # click lands. Playwright's own click() already does exactly this waiting AS PART OF its
        # normal actionability checks (it will not click a genuinely disabled element and will retry
        # until it becomes actionable or its own timeout_ms elapses) -- the ONLY problem was
        # timeout_ms=10000 was too short for this specific button, the same slow-ajax-dependent
        # class of wait already documented multiple times elsewhere in this method (up to ~18-30s).
        # Widened to 30000ms so Playwright's own built-in wait-until-actionable has the same
        # generous budget already given to every other slow step here, instead of either a manual
        # poll-and-bail (step 1's bug) or no wait at all (step 2's bug).
        try:
            submit_outcome = await _submit_and_recover(submit_btn)
        except Exception as exc:
            # logger.exception (not .warning) so the actual Playwright error/call-log is captured --
            # confirmed live (2026-09-01) on WO-PO/99999m18 that even a 30s click timeout budget can
            # still fail here, contradicting this method's own ~18-30s figure for the related Confirm
            # ajax -- the real cause is still unconfirmed. A generic .warning() with no exception
            # detail was not enough to diagnose why; capture the full error next time this fires.
            logger.exception("Stage C: Submit click failed outright for %s (%s)", wo, exc)
            await self._screenshot(f"stage_c_submit_disabled_{wo.replace('/', '-')}")
            return False
        if submit_outcome == "failed":
            logger.warning("Stage C: Submit still failing (SV9317 or no button) after retry for %s",
                            wo)
            await self._screenshot(f"stage_c_submit_disabled_{wo.replace('/', '-')}")
            return False
        # Confirmed in the video (frame at 7:27.93): this Submit click raises its own separate
        # "Confirmation -- Are you sure?" Yes/No dialog -- distinct from Event Details' own Confirm
        # popup earlier in this method. _submit_and_recover already handles this Yes-click (and, if
        # needed, the SV9317-recovery retry with its own Yes-click) internally.
        # Widened 5s -> 20s (2026-09-01), per the user's explicit instruction: "press submit, yes,
        # wait 10-20s. observe data being logged as per video then move on to stage D." Using the top
        # of that range since Synergix's own ajax here has been measured taking up to ~18-30s
        # elsewhere in this method for a closely related action (the Event Details Confirm).
        await page.wait_for_timeout(20000)

        # Corrected (2026-09-01), per the user's direct instruction to check this against the video:
        # the earlier comment here claiming the "not submitted" warning disappears at the EARLIER
        # Event Details Confirm step (not at Submit) was WRONG -- the frame-by-frame record in
        # docs/synergix_workflow.md's "Stage C ground truth" section shows the warning still visibly
        # present at 7:45.0 (right before the Submit click) and gone at 7:48.5 (right after) -- i.e.
        # it disappears exactly AT Submit succeeding, not one step earlier. Check it explicitly here
        # as an additional, independent success signal alongside "Upcoming Service" (not a
        # replacement -- "Upcoming Service" is still the one CONFIRMED-authoritative signal per the
        # user; this banner check is a second, corroborating data point, logged as a warning rather
        # than a hard failure if it disagrees, since the video is the more reliable source of truth).
        not_submitted_banner = page.locator("text=This Service Order has not been submitted").locator(
            "visible=true"
        )
        if await not_submitted_banner.count() > 0:
            logger.warning("Stage C: 'not submitted' banner is STILL visible after Submit+Yes for "
                            "%s -- the video shows this should have cleared; Submit may not have "
                            "actually succeeded despite reaching this point", wo)

        # Verify via "Upcoming Service" showing a new entry -- confirmed live this is the only signal
        # that reflects real server-side persistence. This is the exact signal the user pointed to
        # directly from the video at 7:53 as proof Submit succeeded.
        upcoming = await page.locator("text=Upcoming Service").locator("visible=true").count()
        if not upcoming:
            logger.warning("Stage C: could not find 'Upcoming Service' panel to verify %s", wo)
            return False
        confirmed = await page.get_by_text(order_no, exact=False).locator("visible=true").count() > 0
        if not confirmed:
            logger.warning("Stage C: %s (%s) not found in Upcoming Service after submit", wo, order_no)
            return False
        # "Observe data being logged" (2026-09-01), per the user's instruction: log the actual
        # Upcoming Service entry's text, not just a boolean confirmed/not-confirmed, so a human
        # reviewing the log can see exactly what got recorded before Stage D picks this up.
        upcoming_entry_text = await page.get_by_text(order_no, exact=False).locator(
            "visible=true"
        ).first.inner_text()
        logger.info("Stage C scheduled and submitted for %s (%s) -- Upcoming Service entry: %s",
                    wo, order_no, upcoming_entry_text.replace("\n", " "))
        return True

    async def _open_service_order_performance(self) -> None:
        """Navigate (logged in) to General Service -> Service Order Performance - LS2.

        Same re-navigate-fresh pattern as _open_service_quotation_list/_open_schedule_board.
        Confirmed live (2026-08-26) this page's own datatable takes noticeably longer to render than
        Service Quotation's or Schedule Board's -- callers should not assume the grid is ready
        immediately after this returns; the wait below already accounts for that.
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
            await self.page.get_by_text("Service Order Performance", exact=False).first.click()
            await self.page.wait_for_selector("th:has-text('Order No')", timeout=30000)
            await self.page.wait_for_timeout(6000)
            return
        raise RuntimeError("could not open Service Order Performance after re-login")

    async def _fulfil_stage_d(self, payload: WOPayload) -> bool:
        """Stage D: find the WO's Service Order in Service Order Performance, set each Billables
        row's Actual Qty to match its Quoted Qty, attach the WO PDF (best-effort), and Submit for
        billing -- workflow-doc step 28, the literal end of the whole pipeline.

        Discovered live (2026-08-26) on SV00008852 (WO-PO/000076625): Actual Qty defaults to 0.00
        even when Quoted Qty is correct, and every Billables total (Total/Sales Tax/Total After Tax
        Amount) reads 0.00 until it is set -- Fulfilling without this step would bill the customer
        $0.00, not the WO's authorised amount. Setting Actual Qty = Quoted Qty and pressing Enter (to
        fire the recalculation ajax) correctly recomputes the totals; confirmed live this exactly
        reproduces the WO's authorised net/grand total.

        The Submit button (title="Submit", id ending "submitButton", icon fa-vote-yea -- the SAME
        icon class used at every other stage-ending confirm in this app) triggers the usual
        PrimeFaces.confirm "Are you sure?" Yes/No dialog. Confirmed live that after a real Yes, the
        record disappears from Service Order Performance's own list ENTIRELY (searched with the
        Service Order Status filter set to "All" -- "No records found"), not just changes status
        within it -- that is the verification signal used here, since there is no confirmation toast
        captured live to key off instead.

        Attachments: confirmed live (2026-08-26) the Attachments panel's own file input
        (input[type=file], id ending "..._input", onchange="SynFaces.fileUpload.checkFileSize...")
        is already present in the DOM once the tab is opened -- no button click is needed to spawn
        it. The visible "+" icon next to it is actually "New Folder" (confirmed live: clicking it
        opens an inline folder-rename row, not a file picker) and must NOT be clicked when the goal
        is a file upload -- an earlier attempt did exactly that and got stuck on an unrelated rename
        row. set_input_files() on the existing hidden input is the correct, and only tested, path.
        Still best-effort -- a failure here is logged and does NOT block Submit, since the workflow
        doc's own step ordering (Billables -> Attachments -> Fulfil) treats it as a per-record filing
        step, not a hard gate the way Actual Qty defaulting to 0.00 is.

        Returns whether the Fulfil was confirmed to have gone through (the order no longer appearing
        anywhere in Service Order Performance's list, status filter "All").
        """
        assert self.page is not None
        page = self.page
        wo = payload.wo_po_number

        if _dry_guard(f"fulfil Stage D for {wo}"):
            return True

        await self._open_service_order_performance()

        # Match the order row by its Enquiry/Subject text containing the WO-PO number -- same
        # matching strategy as Stage C's order lookup on Schedule Board.
        order_row = page.locator("tr", has_text=wo.replace("WO-PO/", "")).locator("visible=true").first
        if not await order_row.count():
            logger.warning("Stage D: no Service Order Performance order found for %s", wo)
            return False
        order_no_cell = order_row.locator("td").nth(5)  # Order No column
        order_no = (await order_no_cell.inner_text()).strip()
        fulfil_btn = order_row.get_by_text("Fulfil", exact=True).locator("visible=true").first
        if not await fulfil_btn.count():
            logger.warning("Stage D: no Fulfil button on %s's row for %s", order_no, wo)
            return False
        await fulfil_btn.click(timeout=10000)
        await page.wait_for_timeout(6000)

        billables_tab = page.get_by_text("Billables", exact=True).locator("visible=true").first
        if not await billables_tab.count():
            logger.warning("Stage D: no Billables tab found for %s (%s)", wo, order_no)
            return False
        await billables_tab.click(timeout=10000)
        await page.wait_for_timeout(2000)

        # Set Actual Qty = Quoted Qty for every Billables row -- relationally, NOT by a fixed id
        # fragment count, so this holds for a WO with more than one line item. Confirmed live that
        # the Quoted Qty cell (readonly) and Actual Qty cell (editable) sit in the same <tr>, in that
        # column order, and are visually near-identical otherwise.
        billables_rows = await page.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('table')]
                  .flatMap(t => [...t.querySelectorAll('tbody > tr')])
                  .filter(tr => tr.querySelector('input.qty[readonly], input.qty:not([readonly])'));
                return rows.map(tr => {
                    const inputs = [...tr.querySelectorAll('input.qty')];
                    const quoted = inputs.find(i => i.readOnly);
                    const actual = inputs.find(i => !i.readOnly);
                    return quoted && actual ? [quoted.value, actual.id] : null;
                }).filter(Boolean);
            }"""
        )
        if not billables_rows:
            logger.warning("Stage D: no Billables rows found for %s (%s)", wo, order_no)
            return False
        for quoted_value, actual_id in billables_rows:
            actual_field = page.locator(f'[id="{actual_id}"]')
            await actual_field.fill(quoted_value)
            await actual_field.press("Enter")
            await page.wait_for_timeout(1500)

        # Verify the totals actually recalculated off of 0.00 -- same reconciliation spirit as
        # Stage B's _assert_details_filled: a per-cell value looking right is not proof the page
        # total (the only server-reflected signal) actually moved.
        total_after_tax = await page.evaluate(
            """() => {
                const label = [...document.querySelectorAll('.price-summary-label')]
                  .find(l => l.textContent.trim() === 'Total After Tax Amount');
                const value = label && label.parentElement.querySelector('.price-summary-value');
                return value ? value.textContent.trim() : null;
            }"""
        )
        if not total_after_tax or total_after_tax in ("0.00", ""):
            logger.warning("Stage D: Total After Tax still %r after setting Actual Qty for %s (%s)",
                            total_after_tax, wo, order_no)
            await self._screenshot(f"stage_d_zero_total_{wo.replace('/', '-')}")
            return False

        # Attachments: best-effort, not a hard gate -- see docstring. Do NOT click the "+" icon --
        # confirmed live it is "New Folder", not an upload trigger. The file input is already in the
        # DOM once the tab is open.
        try:
            attachments_tab = page.locator('[title="Attachments"]').locator("visible=true").first
            if await attachments_tab.count():
                await attachments_tab.click(timeout=10000)
                await page.wait_for_timeout(2000)
                file_input = page.locator('input[type="file"]').first
                if await file_input.count():
                    await file_input.set_input_files(payload.source_path)
                    await page.wait_for_timeout(3000)
                    logger.info("Stage D: attached %s for %s", payload.source_path, wo)
                else:
                    logger.warning("Stage D: no file input found on Attachments tab for %s "
                                    "-- skipping attachment", wo)
        except Exception:
            logger.exception("Stage D: attaching the WO PDF failed for %s -- continuing without it "
                              "(best-effort, not a hard gate)", wo)

        submit_btn = page.locator('[id*="submitButton"]').locator("visible=true").first
        if not await submit_btn.count():
            submit_btn = page.locator('button:has(span.fa-vote-yea)').locator("visible=true").first
        if not await submit_btn.count():
            logger.warning("Stage D: no Submit button found for %s (%s)", wo, order_no)
            return False
        await submit_btn.click(timeout=10000)
        await page.wait_for_timeout(1500)
        yes_btn = page.get_by_role("button", name="Yes").locator("visible=true")
        if await yes_btn.count():
            await yes_btn.first.click(timeout=10000)
        # Confirmed live (2026-08-26) that this ajax round-trip can take longer than most other
        # confirms in this file -- an earlier attempt hit "session expired" from waiting too briefly
        # here before checking the result.
        await page.wait_for_timeout(8000)

        # Verify via ground truth: search Service Order Performance directly for this order number
        # with the Service Order Status filter left at "All" -- confirmed live this is the strongest
        # available signal (the order disappears from the list ENTIRELY once Fulfilled, not just
        # changes status within it). The Order No filter input has an accessible label ("Filter by
        # Order No") -- confirmed live this is more reliable than th-then-descendant lookup, which
        # timed out (30s) matching the wrong/no input on this page's datatable header.
        await self._open_service_order_performance()
        order_no_filter = page.get_by_label("Filter by Order No").locator("visible=true").first
        if await order_no_filter.count():
            await order_no_filter.click()
            await order_no_filter.fill(order_no)
            await order_no_filter.press("Enter")
            await page.wait_for_timeout(3000)
        still_present = await page.locator("tr", has_text=order_no).locator("visible=true").count() > 0
        if still_present:
            logger.warning("Stage D: %s (%s) still present in Service Order Performance after "
                            "Submit+Yes", wo, order_no)
            await self._screenshot(f"stage_d_still_present_{wo.replace('/', '-')}")
            return False
        logger.info("Stage D fulfilled and submitted for %s (%s)", wo, order_no)
        return True

    async def _submit_quotation(self, payload: WOPayload) -> tuple[str | None, bool]:
        """Submit the filled draft (DRY_RUN-gated), then immediately Confirm the Variation Order
        on the SAME page. Returns (quotation ID if it can be read, whether the inline confirm
        succeeded -- callers should skip re-running _confirm_variation_order's fallback path if so,
        since re-clicking Confirm on an already-confirmed record is unverified territory).

        Confirmed live (2026-08-27) via frame-by-frame review of the client's own screen recording
        (JBTC WO Synergix.mp4): the client does NOT navigate away to a separate "Under Variation"
        list to confirm the VO. They stay on the just-submitted quotation record. Right after the
        "Confirm Submit?" Yes click, the SAME toolbar grows a new icon (title="Confirm", the
        fa-check-double checkmark, positioned after the pencil/edit icon) -- clicking it raises a
        second "Are you sure?" dialog, and clicking Yes on THAT produced the toast "SA0005: Service
        Order No.: SV00008851 is created successfully." -- one continuous flow, no re-navigation.

        This replaces the earlier approach (_confirm_variation_order navigating to Service
        Quotation -> "Under Variation" tab -> re-filtering -> re-opening the record from scratch),
        which is very likely WHY the Confirm button was intermittently "missing": re-opening the
        record via a fresh list navigation is a different code path/render timing than staying on
        the page, and is not what a human actually does. _confirm_variation_order is kept as a
        best-effort FALLBACK below, in case the inline Confirm icon genuinely isn't there yet.

        In DRY_RUN the Submit click is skipped and logged. Otherwise it clicks Submit, confirms the
        dialog, then immediately looks for and clicks the inline Confirm icon + its own dialog.
        """
        assert self.page is not None
        page = self.page
        quo_id = await self._current_quotation_id()

        if _dry_guard(f"submit quotation for {payload.wo_po_number} (draft {quo_id})"):
            return quo_id, False  # DRY_RUN: left as a draft, VO not confirmed

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

        # Inline Variation Order confirm -- see docstring. Best-effort: a miss here still leaves a
        # real, submitted quotation for _confirm_variation_order (or a human) to pick up.
        vo_confirmed = False
        try:
            confirm_icon = page.locator('[title="Confirm"]').locator("visible=true").first
            await confirm_icon.wait_for(state="visible", timeout=6000)
            await confirm_icon.click(timeout=5000)
            await page.wait_for_timeout(1500)
            yes_btn = page.get_by_role("button", name="Yes").locator("visible=true")
            if await yes_btn.count():
                await yes_btn.first.click(timeout=5000)
                # Same SA0005 toast signal as _confirm_variation_order -- see that method's
                # docstring for why "quotation left Under Variation" is NOT used here, and why this
                # polls toast.count() (DOM presence) rather than wait_for(state="visible"): confirmed
                # live (2026-08-30) that a stacked/animating toast can be missed by a strict
                # visibility wait even after a genuine success, which previously caused write() to
                # retry Confirm+Yes via the fallback and create a real duplicate Service Order.
                toast = page.locator("text=/SA0005.*Service Order No\\.?:?\\s*SV\\d+.*created successfully/i")
                for _ in range(16):  # ~8s
                    if await toast.count():
                        vo_confirmed = True
                        break
                    await page.wait_for_timeout(500)
                if vo_confirmed:
                    logger.info("Inline Variation Order confirm succeeded for %s (quotation %s)",
                                payload.wo_po_number, quo_id)
                else:
                    # Ground-truth fallback, same as _confirm_variation_order's own last check.
                    if quo_id:
                        vo_confirmed = await self._service_order_exists_for_quotation(quo_id)
                    if vo_confirmed:
                        logger.info("Inline confirm: no toast seen for %s (quotation %s), but a "
                                    "Service Order exists in Schedule Board -- treating as confirmed",
                                    payload.wo_po_number, quo_id)
                    else:
                        logger.warning("Inline Confirm+Yes clicked but no SA0005 toast seen and no "
                                        "Service Order found for %s (quotation %s) -- falling back "
                                        "to _confirm_variation_order",
                                        payload.wo_po_number, quo_id)
            else:
                logger.warning("Inline Confirm icon found but no 'Are you sure?' Yes button for "
                                "%s (quotation %s) -- falling back to _confirm_variation_order",
                                payload.wo_po_number, quo_id)
        except Exception:
            logger.warning("Inline Confirm icon not found right after Submit for %s (quotation "
                            "%s) -- falling back to _confirm_variation_order", payload.wo_po_number,
                            quo_id)
            await self._screenshot(f"inline_confirm_missing_{(quo_id or 'unknown')}")
        return quo_id, vo_confirmed

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

