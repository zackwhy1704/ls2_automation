"""TCMS (JBTC, Dynamics 365) scraper: login, list un-invoiced WOs, download PDFs.

Every selector is a placeholder in config/selectors.py. A placeholder selector raises
MissingSelectorError, which the caller turns into a clear `MISSING SELECTOR: <name>` log line.

D365 re-renders late and lazily — we use wait_for_load_state("networkidle") and explicit
wait_for_selector, never fixed sleeps.
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config import selectors as S
from config import settings

logger = logging.getLogger(__name__)


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
        """Log into the D365 portal via Microsoft Entra, using the password (no-MFA) fallback path.

        Idempotent: if the persisted session is still valid we land straight on the dashboard and skip
        the credential steps. The account uses password auth via the "Use your password instead" link,
        so no Authenticator push is required.
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

    async def list_uninvoiced(self) -> list[str]:
        """Return the WO/PO numbers of all un-invoiced work orders.

        The D365 grid (FixedDataTable) is virtualized: only ~26 rows are in the DOM at a time and the
        cell inputs aren't directly clickable. We mouse-wheel over the grid and read the WO/PO values
        via JS after each scroll, accumulating until the whole list (grid aria-rowcount) is seen.
        """
        assert self.page is not None
        await self._open_uninvoiced_list()

        seen: set[str] = set(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
        rect = await self._wo_grid_rect()
        if rect:
            await self.page.mouse.move(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
            stagnant = 0
            for _ in range(200):  # generous cap; exits on stagnation
                await self.page.mouse.wheel(0, 300)
                await self.page.wait_for_timeout(300)
                before = len(seen)
                seen.update(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
                stagnant = 0 if len(seen) > before else stagnant + 1
                if stagnant >= 15:  # ~4.5s of no new rows -> reached the end
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

    async def download_pdf(self, wo_po_number: str) -> str:
        """Open a WO by its WO/PO number and download its PDF via Preview/Print -> Original preview.

        Returns the saved path. Re-navigates to the list afterwards so the next WO starts from a known
        state. Caller handles per-WO error isolation.
        """
        assert self.page is not None
        safe_name = wo_po_number.replace("/", "-")
        dest = settings.PDF_DIR / f"{safe_name}.pdf"
        try:
            # Scroll the WO into the virtualized grid, then open it. The cell inputs aren't directly
            # clickable (D365 layering), so we open via a JS-dispatched double click on the input.
            await self._scroll_to_wo(wo_po_number)
            opened = await self.page.evaluate(
                "(wo) => { const inp = [...document.querySelectorAll('[role=\"grid\"] input')]"
                ".find(i => (i.value||'').trim() === wo); if (!inp) return false; "
                "inp.scrollIntoView({block:'center'}); "
                "for (const t of ['mousedown','mouseup','click','dblclick']) "
                "inp.dispatchEvent(new MouseEvent(t, {bubbles:true})); return true; }",
                wo_po_number,
            )
            if not opened:
                raise RuntimeError(f"WO {wo_po_number} not found in the un-invoiced grid")
            await self.page.wait_for_timeout(8000)

            # Preview/Print -> Original preview -> SSRS PDF viewer -> Export (triggers the download).
            await self.page.click(S.require("TCMS_WO_PREVIEW_PRINT", S.TCMS_WO_PREVIEW_PRINT))
            await self.page.wait_for_timeout(3000)
            await self.page.click(S.require("TCMS_WO_ORIGINAL_PREVIEW", S.TCMS_WO_ORIGINAL_PREVIEW))
            await self.page.wait_for_timeout(10000)  # SSRS report render

            async with self.page.expect_download() as dl_info:
                await self.page.click(S.require("TCMS_WO_PDF_EXPORT", S.TCMS_WO_PDF_EXPORT))
                await self.page.wait_for_timeout(2000)
                pdf_item = self.page.get_by_text("PDF", exact=False)
                if await pdf_item.count():
                    await pdf_item.first.click()
            download = await dl_info.value
            await download.save_as(str(dest))
            logger.info("Downloaded PDF for %s -> %s", wo_po_number, dest)
            return str(dest)
        finally:
            await self._back_to_list()

    async def _scroll_to_wo(self, wo_po_number: str) -> None:
        """Reopen the list and wheel-scroll the grid until the given WO/PO id is rendered.

        Idempotent starting point: always reopens the list so the grid is at the top, then scrolls
        until the target id appears in the rendered set (or the grid ends).
        """
        assert self.page is not None
        await self._open_uninvoiced_list()

        async def rendered() -> bool:
            ids = await self.page.evaluate(self._COLLECT_WO_IDS_JS)
            return wo_po_number in ids

        if await rendered():
            return
        rect = await self._wo_grid_rect()
        if not rect:
            return
        await self.page.mouse.move(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        stagnant = 0
        prev_seen = 0
        seen: set[str] = set()
        for _ in range(200):
            await self.page.mouse.wheel(0, 300)
            await self.page.wait_for_timeout(300)
            if await rendered():
                return
            seen.update(await self.page.evaluate(self._COLLECT_WO_IDS_JS))
            stagnant = 0 if len(seen) > prev_seen else stagnant + 1
            prev_seen = len(seen)
            if stagnant >= 15:
                return

    async def _back_to_list(self) -> None:
        """Best-effort return to a known state — re-navigate to the base dashboard.

        The SSRS PDF viewer opens as a modal over the workspace; rather than depend on a fragile
        close/back control, we just re-navigate. The next download_pdf() reopens the list as needed.
        """
        assert self.page is not None
        try:
            await self.page.goto(settings.TCMS_BASE_URL, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(3000)
        except Exception:
            logger.exception("Failed to recover TCMS to a known state")
