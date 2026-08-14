"""TCMS (JBTC, Dynamics 365) scraper: login, list un-invoiced WOs, download PDFs.

Every selector is a placeholder in config/selectors.py. A placeholder selector raises
MissingSelectorError, which the caller turns into a clear `MISSING SELECTOR: <name>` log line.

D365 re-renders late and lazily — we use wait_for_load_state("networkidle") and explicit
wait_for_selector, never fixed sleeps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config import selectors as S
from config import settings

logger = logging.getLogger(__name__)


class WOAlreadyProcessedError(RuntimeError):
    """Raised when a WO's live TCMS "WO/PO status" is anything other than "Received" — e.g. already
    Invoiced or Cancelled by someone else since JBTC's (hand-maintained, can-be-stale) Un-Invoiced WO
    list was scraped. Callers should treat this as a duplicate, not a failure.
    """

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"status={status!r}, not Received — already processed, skipping")


@dataclass
class DownloadedWO:
    """Result of TCMSScraper.download_pdf: the PDF plus fields only visible on the TCMS detail
    page (not on the printed WO PDF itself)."""

    path: str
    status: str              # the "WO/PO status" field, e.g. "Received", "Invoiced", "Cancelled"
    property_officer: str = ""  # the "Property officer" field — the TC staff member who raised the WO


class TCMSScraper:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None

    async def __aenter__(self) -> "TCMSScraper":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # Persistent context so the Entra session cookie survives across runs — we log in once and
        # reuse it until it expires (D365 sessions last hours/days), avoiding a re-auth every poll.
        settings.TCMS_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._context = await self._pw.chromium.launch_persistent_context(
            str(settings.TCMS_SESSION_DIR), headless=settings.HEADLESS, accept_downloads=True
        )
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        logger.info("TCMS browser launched (headless=%s, persistent session)", settings.HEADLESS)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        """Log into the D365 portal via Microsoft Entra, preferring the password (no-MFA) fallback path.

        Idempotent: if the persisted session is still valid we land straight on the dashboard and skip
        the credential steps. As observed so far, this account can bypass an Authenticator push via
        the "Use your password instead" link. That has NOT been confirmed as a guaranteed property of
        JBTC's tenant Conditional Access policy — if the tenant ever enforces MFA with no fallback link
        offered at all, _detect_mfa_dead_end below raises a specific, diagnosable error instead of the
        generic dashboard-timeout below (which would otherwise look identical to any other login hiccup
        and could mean the persisted Entra session cookie stops renewing until a human intervenes).
        """
        if not settings.TCMS_BASE_URL:
            raise RuntimeError("TCMS_BASE_URL is not set in .env")
        assert self.page is not None
        await self.page.goto(settings.TCMS_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(4000)

        if self._on_dashboard():
            logger.info("TCMS session still valid — already on dashboard")
            return

        # Entra step 1: email.
        if await self.page.locator("#i0116").count():
            await self.page.fill("#i0116", settings.TCMS_USERNAME)
            await self.page.click("#idSIButton9")
            await self.page.wait_for_timeout(5000)

        # A first password field may appear directly, or the tenant may push MFA first.
        await self._enter_password_if_present()

        # If MFA (Authenticator push) is shown, switch to the password fallback and enter it there.
        pw_instead = self.page.get_by_text("Use your password instead", exact=False)
        if await pw_instead.count():
            await pw_instead.first.click()
            await self.page.wait_for_timeout(4000)
            await self._enter_password_if_present()
        else:
            await self._detect_mfa_dead_end()

        # "Stay signed in?" -> Yes, to persist the session cookie.
        for _ in range(3):
            if await self.page.get_by_text("Stay signed in", exact=False).count():
                await self.page.click("#idSIButton9")
                await self.page.wait_for_timeout(8000)
                break
            await self.page.wait_for_timeout(3000)

        await self.page.wait_for_timeout(6000)
        if not self._on_dashboard():
            raise RuntimeError(f"TCMS login did not reach the dashboard (at {self.page.url[:80]})")
        logger.info("TCMS login successful")

    async def _detect_mfa_dead_end(self) -> None:
        """Raise a specific, diagnosable error if Entra is asking for an Authenticator approval with
        NO password-fallback link offered — i.e. Conditional Access MFA is mandatory for this account
        and the automation genuinely cannot proceed unattended. Without this, that scenario falls
        through to the generic "did not reach the dashboard" timeout ~15s later, which looks the same
        as any other transient login hiccup and gives the person checking the alert no signal that this
        needs a tenant policy change (a JBTC Conditional Access exemption or a dedicated non-MFA service
        account), not a retry.
        """
        assert self.page is not None
        awaiting_approval = self.page.get_by_text("Approve sign in", exact=False)
        if await awaiting_approval.count():
            raise RuntimeError(
                "TCMS login is blocked on an Authenticator push approval with no password-fallback "
                "link available — Conditional Access MFA appears mandatory for this account. This "
                "cannot be automated unattended; ask JBTC for an MFA exemption on the service account "
                "used here, or a dedicated non-MFA service account."
            )

    def _on_dashboard(self) -> bool:
        """True when the current URL is the D365 app (not the login domain)."""
        assert self.page is not None
        url = self.page.url
        return "dynamics.com" in url and "login" not in url

    async def _enter_password_if_present(self) -> None:
        assert self.page is not None
        if await self.page.locator("#i0118").count():
            await self.page.fill("#i0118", settings.TCMS_PASSWORD)
            await self.page.click("#idSIButton9")
            await self.page.wait_for_timeout(6000)

    async def _open_uninvoiced_list(self) -> None:
        """Navigate dashboard -> Work order workspace -> "Un-Invoiced WO" list, to a known state."""
        assert self.page is not None
        await self.page.goto(settings.TCMS_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(10000)  # D365 renders lazily
        await self.page.click(S.require("TCMS_WORKSPACE_TILE", S.TCMS_WORKSPACE_TILE))
        await self.page.wait_for_timeout(10000)
        await self.page.click(S.require("TCMS_UNINVOICED_NAV", S.TCMS_UNINVOICED_NAV))
        await self.page.wait_for_timeout(12000)
        await self.page.wait_for_selector(S.require("TCMS_WO_ROW", S.TCMS_WO_ROW))

    # JS that reads every rendered WO/PO id from any D365 grid cell input on the page.
    _COLLECT_WO_IDS_JS = (
        "() => { const out = []; "
        "document.querySelectorAll('[role=\"grid\"] input').forEach(i => { "
        "const v = (i.value || '').trim(); if (v.startsWith('WO-PO/')) out.push(v); }); "
        "return out; }"
    )

    def _wo_input_locator(self, wo_po_number: str):
        """Locator for the WO/PO cell input holding this exact id, matched by its `value` ATTRIBUTE
        (present in the DOM per live inspection, not just the JS property) via XPath.

        NOT Playwright's role/description locator: confirmed live (2026-08-04) that the row's
        accessible description (derived from its `title` attribute, since aria-label already claims
        the accessible name) is no longer the bare WO-PO id but has a trailing tooltip appended, e.g.
        "WO-PO/000076908\n\r\nClick to follow link" — an exact-match role locator against the old
        bare-id format matches NO current row, and a regex-based description doesn't work either
        (Playwright serialises it into a `/pattern/`-delimited attribute selector, which breaks on the
        literal "/" inside every WO-PO id). A manual `getBoundingClientRect()` also isn't reliable
        here (observed all-zero for an otherwise visible, in-viewport cell), so let Playwright's own
        `.click()` do the scroll-into-view + actionability work instead of computing coordinates.
        """
        assert self.page is not None
        return self.page.locator(f'xpath=//input[@value="{wo_po_number}"]')

    async def _wo_row_visible(self, wo_po_number: str) -> bool:
        """True only once the row is genuinely on-screen and clickable, not merely present in the DOM.

        Confirmed live (2026-08-04) via a working `playwright codegen` recording that
        `get_by_role("textbox", name="WO/PO", description=<id>)` (no `exact=True`) DOES select a real
        row when a human clicks one that's already on-screen — so the row-matching mechanism itself
        is fine. The failure mode this guards against is virtualization overscan: `_COLLECT_WO_IDS_JS`
        (and a bare DOM-presence check) can see a row's `value` before it has scrolled into the actual
        visible viewport, at which point Playwright correctly reports it "not visible" and any click
        times out. `.is_visible()` checks real, non-zero geometry, so waiting on this (not just
        presence) is what makes `_scroll_to_wo` stop scrolling at the right point.
        """
        loc = self._wo_input_locator(wo_po_number)
        if not await loc.count():
            return False
        return await loc.first.is_visible()

    # The FixedDataTable's own scrollbar widget (not the grid/viewport). Keyboard ArrowDown presses
    # against this focused element are what actually advance the virtualization window — mouse.wheel
    # and scrollTop manipulation on the grid do NOT (confirmed via a user codegen recording on
    # 2026-07-31: clicking this element once then repeatedly pressing ArrowDown reached row 252/252,
    # something ~30 prior scripted-scroll attempts using wheel/scrollTop never achieved).
    _SCROLLBAR_FACE = ".ScrollbarLayout_main.ScrollbarLayout_mainVertical.public_Scrollbar_main"

    async def _focus_scrollbar(self) -> bool:
        """Click the WO grid's own vertical scrollbar to focus it for ArrowDown-driven scrolling.

        The page can have more than one `.ScrollbarLayout_main.ScrollbarLayout_mainVertical` element
        (e.g. a stale/hidden one from another panel) — `.first` isn't reliable. Pick the visible one.
        """
        assert self.page is not None
        candidates = self.page.locator(self._SCROLLBAR_FACE)
        count = await candidates.count()
        for i in range(count):
            candidate = candidates.nth(i)
            if await candidate.is_visible():
                await candidate.click(timeout=5000)
                return True
        return False

    async def list_uninvoiced(self) -> list[str]:
        """Return the WO/PO numbers of all un-invoiced work orders.

        The D365 grid (FixedDataTable) is virtualized: only ~26-43 rows are in the DOM at a time.
        We focus the grid's scrollbar widget and press ArrowDown repeatedly (not mouse.wheel — see
        `_SCROLLBAR_FACE`), reading the WO/PO values via JS after each press, accumulating until the
        whole list stops growing.
        """
        assert self.page is not None
        await self._open_uninvoiced_list()

        seen: set[str] = set(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
        if await self._focus_scrollbar():
            stagnant = 0
            for _ in range(600):  # generous cap (verified reaching 252/252 rows); exits on stagnation
                await self.page.keyboard.press("ArrowDown")
                await self.page.wait_for_timeout(150)
                before = len(seen)
                seen.update(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
                stagnant = 0 if len(seen) > before else stagnant + 1
                if stagnant >= 40:  # ~6s of no new rows -> reached the end
                    break

        ids = sorted(seen)
        logger.info("Found %d un-invoiced WO(s)", len(ids))
        return ids

    async def _wo_grid_rect(self) -> dict | None:
        """Bounding rect of the un-invoiced WO grid (the one whose aria-rowcount is the WO count)."""
        assert self.page is not None
        return await self.page.evaluate(
            "() => { const gs = [...document.querySelectorAll('[role=\"grid\"]')]; "
            "const g = gs.find(x => (x.getAttribute('aria-label')||'').includes('header versions')) "
            "|| gs.sort((a,b)=>(+b.getAttribute('aria-rowcount'||0))-(+a.getAttribute('aria-rowcount'||0)))[0]; "
            "if (!g) return null; const r = g.getBoundingClientRect(); "
            "return r.height > 0 ? {x:r.x, y:r.y, w:r.width, h:r.height} : null; }"
        )

    async def _read_wo_detail_fields(self) -> tuple[str, str]:
        """Read (status, property_officer) from the currently-open WO detail page.

        Both are `<label>` fields not present on the printed WO PDF. Matched by restricting the
        search to `<label>` elements specifically — a broader `label,div,span,td` search (as used for
        other fields) picks up decoy elements first here: a hidden quick-filter menu template also
        contains the literal text "WO/PO status" earlier in DOM order than the real field (confirmed
        live, 2026-08-14).
        """
        assert self.page is not None
        result = await self.page.evaluate(
            """() => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const valueFor = (labelText) => {
                    const label = [...document.querySelectorAll('label')]
                        .find(l => norm(l.textContent) === labelText);
                    if (!label) return '';
                    const parent = label.closest('[data-dyn-controlname]') || label.parentElement;
                    const input = parent ? parent.querySelector('input') : null;
                    return input ? input.value.trim() : '';
                };
                return {status: valueFor('WO/PO status'), officer: valueFor('Property officer')};
            }"""
        )
        return result.get("status", ""), result.get("officer", "")

    async def download_pdf(self, wo_po_number: str) -> DownloadedWO:
        """Select a WO by its WO/PO number and download its PDF via Preview/Print -> Copy preview.

        Verified flow (2026-07-31, via a user codegen recording — see git history for the full
        investigation): on the Un-Invoiced WO list, click the WO's `WO/PO` textbox (role=textbox,
        accessible name "WO/PO", description=<the WO-PO value>) to select it, then Preview/Print ->
        Copy preview. Copy preview triggers an async "Processing operation" that transitions the
        CURRENT page in place (its title becomes the WO-PO and an Export button appears) — no
        navigation needed. Critically, do NOT call page.goto() during this: an early goto() tears
        down the in-flight async operation and strands the page on the workspace dashboard, which
        earlier caused this method to silently export a stale/wrong WO's PDF (a billing-critical bug;
        see the content-verification guard below and in batch.self_process_one).

        Also re-checks the WO's live "WO/PO status" right after selecting it — the "Un-Invoiced WO"
        list this WO/PO was found in is hand-maintained on JBTC's side and can be stale (confirmed via
        a live SOP audit finding 3 quotations raised against WOs TCMS already showed Invoiced); this
        is the second, TCMS-side half of the duplicate check, independent of Synergix's own dedup.
        Raises RuntimeError with a status-prefixed message if it's anything other than "Received" —
        the caller (batch.self_process_one) treats that as a duplicate, not a failure.

        Returns the saved path plus Property officer. Clicks "Back" afterwards so the next WO starts
        from a known state (the list). Caller handles per-WO error isolation.
        """
        assert self.page is not None
        safe_name = wo_po_number.replace("/", "-")
        dest = settings.PDF_DIR / f"{safe_name}.pdf"
        try:
            if not await self._wo_row_visible(wo_po_number):
                await self._scroll_to_wo(wo_po_number)
            if not await self._wo_row_visible(wo_po_number):
                raise RuntimeError(f"WO {wo_po_number} not found in the un-invoiced list")
            await self._wo_input_locator(wo_po_number).first.click(timeout=15000)
            # Wait for the selection to actually settle (Preview/Print becoming clickable) rather than
            # a fixed delay — under sustained batch load the page can take longer than 2s to react.
            preview_print = S.require("TCMS_WO_PREVIEW_PRINT", S.TCMS_WO_PREVIEW_PRINT)
            await self.page.wait_for_selector(preview_print, state="visible", timeout=20000)
            await self.page.wait_for_timeout(1000)

            status, property_officer = await self._read_wo_detail_fields()
            if status and status.strip().lower() != "received":
                raise WOAlreadyProcessedError(status)

            await self.page.click(preview_print, timeout=20000)
            await self.page.wait_for_timeout(2000)
            await self.page.click(S.require("TCMS_WO_ORIGINAL_PREVIEW", S.TCMS_WO_ORIGINAL_PREVIEW))

            # Wait for the in-place transition to finish: the Export button appearing is the signal
            # (title also becomes the WO-PO, but the button is what we click next).
            await self.page.wait_for_selector('button:has-text("Export")', timeout=30000)
            await self.page.wait_for_timeout(1500)

            # CRITICAL content check: confirm the page title reflects THIS WO before exporting.
            # A mismatch means the transition landed on the wrong record — abort rather than export
            # stale data (billing-critical; re-verified again after download via extraction).
            title = await self.page.title()
            if wo_po_number not in title:
                raise RuntimeError(
                    f"page title {title!r} does not match {wo_po_number} — aborting export "
                    "(would have exported the wrong WO's PDF)")

            async with self.page.expect_download(timeout=20000) as dl_info:
                await self.page.click(S.require("TCMS_WO_PDF_EXPORT", S.TCMS_WO_PDF_EXPORT))
                await self.page.wait_for_timeout(2000)
                pdf_item = self.page.get_by_role("menuitem", name="PDF")
                if await pdf_item.count():
                    await pdf_item.first.click()
            download = await dl_info.value
            await download.save_as(str(dest))
            logger.info("Downloaded PDF for %s -> %s", wo_po_number, dest)
            return DownloadedWO(path=str(dest), status=status, property_officer=property_officer)
        finally:
            await self._back_to_list()

    async def _scroll_to_wo(self, wo_po_number: str) -> None:
        """Reopen the list and scroll (via focused-scrollbar ArrowDown) until the given WO/PO id is
        selectable.

        Idempotent starting point: always reopens the list so the grid is at the top, then scrolls
        until the target's row is genuinely VISIBLE (`_wo_row_visible`) — not merely present in the
        DOM, which can happen well before a deeply-scrolled row enters the real viewport (see
        `_wo_row_visible`'s docstring). Uses ArrowDown on the focused scrollbar widget, not
        mouse.wheel — see `_SCROLLBAR_FACE` for why.
        """
        assert self.page is not None
        await self._open_uninvoiced_list()

        async def rendered() -> bool:
            return await self._wo_row_visible(wo_po_number)

        if await rendered():
            return
        if not await self._focus_scrollbar():
            return
        # Patience budget: confirmed live (2026-08-04) that ArrowDown-driven scrolling reaches a
        # moderately-deep target after real scrolling (not just the top few rows). BUT also confirmed,
        # via direct instrumentation, that some rows near the tail of the ~233-item list NEVER get a
        # real, visible bounding box no matter how far scrolling continues — the DOM's `seen` set
        # reaches the full 233 (proving the scroll genuinely traverses the entire list) while the
        # target still never becomes clickable, even across 1500 presses / ~225s. That is a TCMS/D365
        # rendering limitation this loop cannot out-wait; more patience here only wastes time on rows
        # that were never going to succeed. This budget is sized for the "genuinely reachable but
        # deep" case, not the structurally-stuck one — see docs/operational_expectations.md.
        stagnant = 0
        prev_seen = 0
        seen: set[str] = set()
        for _ in range(900):
            await self.page.keyboard.press("ArrowDown")
            await self.page.wait_for_timeout(150)
            if await rendered():
                return
            seen.update(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
            stagnant = 0 if len(seen) > prev_seen else stagnant + 1
            prev_seen = len(seen)
            if stagnant >= 60:
                return

    async def _back_to_list(self) -> None:
        """Return to the Un-Invoiced WO list via the in-app "Back" button (known-state recovery).

        Prefer the "Back" button over page.goto(): a goto() mid-flow tears down D365's async
        client-side state (this caused the stale-PDF bug — see download_pdf's docstring). Falls back
        to goto() only if Back isn't available (e.g. nothing was ever opened this session).
        """
        assert self.page is not None
        try:
            back = self.page.get_by_role("button", name="Back")
            if await back.count():
                await back.first.click(timeout=10000)
                await self.page.wait_for_timeout(3000)
                return
        except Exception:
            logger.warning("'Back' button recovery failed — falling back to full navigation")
        try:
            await self.page.goto(settings.TCMS_BASE_URL, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(3000)
        except Exception:
            logger.exception("Failed to recover TCMS to a known state")
