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
        self._browser = await self._pw.chromium.launch(headless=settings.HEADLESS)
        self._context = await self._browser.new_context(accept_downloads=True)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        logger.info("TCMS browser launched (headless=%s)", settings.HEADLESS)

    async def close(self) -> None:
        for obj in (self._context, self._browser):
            if obj:
                await obj.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        if not settings.TCMS_BASE_URL:
            raise RuntimeError("TCMS_BASE_URL is not set in .env")
        assert self.page is not None
        await self.page.goto(settings.TCMS_BASE_URL)
        await self.page.wait_for_load_state("networkidle")

        # TODO(human): handle MFA — confirm with JBTC whether the service account has MFA / IP whitelist.
        # If MFA is required, this simple username/password flow will not be sufficient.
        await self.page.fill(S.require("TCMS_USERNAME_INPUT", S.TCMS_USERNAME_INPUT), settings.TCMS_USERNAME)
        await self.page.fill(S.require("TCMS_PASSWORD_INPUT", S.TCMS_PASSWORD_INPUT), settings.TCMS_PASSWORD)
        await self.page.click(S.require("TCMS_LOGIN_BUTTON", S.TCMS_LOGIN_BUTTON))
        await self.page.wait_for_selector(S.require("TCMS_LOGIN_SUCCESS_MARKER", S.TCMS_LOGIN_SUCCESS_MARKER))
        logger.info("TCMS login successful")

    async def list_uninvoiced(self) -> list[str]:
        """Return a list of WO row identifiers for un-invoiced work orders."""
        assert self.page is not None
        await self.page.click(S.require("TCMS_UNINVOICED_NAV", S.TCMS_UNINVOICED_NAV))
        await self.page.wait_for_load_state("networkidle")
        await self.page.wait_for_selector(S.require("TCMS_WO_ROW", S.TCMS_WO_ROW))

        rows = await self.page.locator(S.require("TCMS_WO_ROW", S.TCMS_WO_ROW)).all()
        ids: list[str] = []
        for row in rows:
            # TODO(human): adjust extraction depending on whether the id lives in an attribute or child text.
            attr_or_sel = S.require("TCMS_WO_ROW_ID_ATTR", S.TCMS_WO_ROW_ID_ATTR)
            wo_id = await row.get_attribute(attr_or_sel)
            if wo_id is None:
                wo_id = (await row.locator(attr_or_sel).inner_text()).strip()
            if wo_id:
                ids.append(wo_id)
        logger.info("Found %d un-invoiced WO(s)", len(ids))
        return ids

    async def download_pdf(self, wo_po_number: str) -> str:
        """Open the WO by id and download its PDF. Returns the saved path.

        Caller is responsible for per-WO error isolation. We always try to return to the list
        afterwards so the next WO starts from a known state.
        """
        assert self.page is not None
        safe_name = wo_po_number.replace("/", "-")
        dest = settings.PDF_DIR / f"{safe_name}.pdf"
        try:
            # Open the specific WO. The id may need to be used to locate its row/link first.
            await self.page.click(S.require("TCMS_WO_OPEN_LINK", S.TCMS_WO_OPEN_LINK))
            await self.page.wait_for_load_state("networkidle")

            async with self.page.expect_download() as dl_info:
                await self.page.click(
                    S.require("TCMS_WO_PDF_DOWNLOAD_BUTTON", S.TCMS_WO_PDF_DOWNLOAD_BUTTON)
                )
            download = await dl_info.value
            await download.save_as(str(dest))
            logger.info("Downloaded PDF for %s -> %s", wo_po_number, dest)
            return str(dest)
        finally:
            await self._back_to_list()

    async def _back_to_list(self) -> None:
        """Best-effort return to the un-invoiced list (known state)."""
        assert self.page is not None
        try:
            await self.page.click(S.require("TCMS_WO_BACK_TO_LIST", S.TCMS_WO_BACK_TO_LIST))
            await self.page.wait_for_load_state("networkidle")
        except Exception as exc:  # selector missing or click failed — recover by re-navigating
            logger.warning("Could not use back-to-list control (%s); re-navigating to base URL", exc)
            try:
                await self.page.goto(settings.TCMS_BASE_URL)
                await self.page.wait_for_load_state("networkidle")
            except Exception:
                logger.exception("Failed to recover TCMS to a known state")
