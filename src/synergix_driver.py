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
from dataclasses import dataclass

from playwright.async_api import Page, async_playwright

from config import selectors as S
from config import settings
from src.models import WOPayload, WOStatus
from src.validator import build_remarks, resolve_project_code

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    status: WOStatus            # PROCESSED, PARTIAL, or FAILED
    detail: str = ""


def _dry_guard(action: str) -> bool:
    """Return True if the (final, mutating) action should be SKIPPED due to DRY_RUN.

    Logs the decision either way so the run log clearly shows what was/wasn't submitted.
    """
    if settings.DRY_RUN:
        logger.info("[DRY_RUN] SKIPPING final action: %s", action)
        return True
    logger.info("[LIVE] executing final action: %s", action)
    return False


class SynergixDriver:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._logged_in = False

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=settings.HEADLESS)
        self._context = await self._browser.new_context(accept_downloads=True)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        logger.info("Synergix browser launched (headless=%s)", settings.HEADLESS)

    async def close(self) -> None:
        for obj in (self._context, self._browser):
            if obj:
                await obj.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        if self._logged_in:
            return
        if not settings.SYNERGIX_BASE_URL:
            raise RuntimeError("SYNERGIX_BASE_URL is not set in .env")
        assert self.page is not None
        await self.page.goto(settings.SYNERGIX_BASE_URL)
        await self.page.wait_for_load_state("networkidle")
        await self.page.fill(
            S.require("SYNERGIX_USERNAME_INPUT", S.SYNERGIX_USERNAME_INPUT), settings.SYNERGIX_USERNAME
        )
        await self.page.fill(
            S.require("SYNERGIX_PASSWORD_INPUT", S.SYNERGIX_PASSWORD_INPUT), settings.SYNERGIX_PASSWORD
        )
        await self.page.click(S.require("SYNERGIX_LOGIN_BUTTON", S.SYNERGIX_LOGIN_BUTTON))
        await self.page.wait_for_selector(
            S.require("SYNERGIX_LOGIN_SUCCESS_MARKER", S.SYNERGIX_LOGIN_SUCCESS_MARKER)
        )
        self._logged_in = True
        logger.info("Synergix login successful")

    # ------------------------------------------------------------------ duplicate check
    async def is_duplicate(self, wo_po_number: str) -> bool:
        """Search Synergix for an existing record matching the WO-PO. True => already exists.

        # TODO(human): confirm the correct Synergix search screen + that WO-PO is the right search key.
        """
        await self.login()
        assert self.page is not None
        await self.page.click(S.require("SYNERGIX_DEDUP_NAV", S.SYNERGIX_DEDUP_NAV))
        await self.page.wait_for_load_state("networkidle")
        await self.page.fill(
            S.require("SYNERGIX_DEDUP_SEARCH_INPUT", S.SYNERGIX_DEDUP_SEARCH_INPUT), wo_po_number
        )
        await self.page.click(S.require("SYNERGIX_DEDUP_SEARCH_SUBMIT", S.SYNERGIX_DEDUP_SEARCH_SUBMIT))
        await self.page.wait_for_load_state("networkidle")

        result_row = self.page.locator(S.require("SYNERGIX_DEDUP_RESULT_ROW", S.SYNERGIX_DEDUP_RESULT_ROW))
        count = await result_row.count()
        dup = count > 0
        logger.info("Duplicate check for %s: %s (matches=%d)", wo_po_number, dup, count)
        return dup

    # ------------------------------------------------------------------ write path
    async def write(self, payload: WOPayload) -> WriteResult:
        """Run stages B, C, D for one WO. Returns a WriteResult; never raises for a WO-level error."""
        try:
            await self.login()
            project_code = resolve_project_code(payload.job_sheet_number)
            remarks = build_remarks(payload)

            await self._stage_b_create_quotation(payload, project_code, remarks)
            schedule_ok = await self._stage_c_schedule_board(payload)
            await self._stage_d_attach_and_fulfil(payload)

            if not schedule_ok:
                return WriteResult(
                    WOStatus.PARTIAL,
                    "schedule board needs manual action (stage C was best-effort and did not complete)",
                )
            return WriteResult(WOStatus.PROCESSED, "all stages completed")
        except S.MissingSelectorError as exc:
            logger.error("MISSING SELECTOR: %s — fill it in config/selectors.py", exc)
            return WriteResult(WOStatus.FAILED, f"missing selector: {exc}")
        except Exception as exc:
            logger.exception("Synergix write failed for %s", payload.wo_po_number)
            await self._screenshot(payload.wo_po_number)
            return WriteResult(WOStatus.FAILED, str(exc))
        finally:
            await self._back_to_home()

    async def _stage_b_create_quotation(
        self, payload: WOPayload, project_code: str, remarks: str
    ) -> None:
        assert self.page is not None
        logger.info("Stage B: create quotation for %s", payload.wo_po_number)
        await self.page.click(S.require("SYNERGIX_NEW_QUOTATION_NAV", S.SYNERGIX_NEW_QUOTATION_NAV))
        await self.page.wait_for_load_state("networkidle")

        # Copy From a fixed template quotation.
        if not settings.SYNERGIX_TEMPLATE_QUO_ID:
            raise RuntimeError("SYNERGIX_TEMPLATE_QUO_ID is not set in .env")
        await self.page.click(S.require("SYNERGIX_COPY_FROM_BUTTON", S.SYNERGIX_COPY_FROM_BUTTON))
        await self.page.fill(
            S.require("SYNERGIX_COPY_FROM_ID_INPUT", S.SYNERGIX_COPY_FROM_ID_INPUT),
            settings.SYNERGIX_TEMPLATE_QUO_ID,
        )
        await self.page.click(S.require("SYNERGIX_COPY_FROM_CONFIRM", S.SYNERGIX_COPY_FROM_CONFIRM))
        await self.page.wait_for_load_state("networkidle")

        # Fill the ~8 fields.
        await self.page.fill(
            S.require("SYNERGIX_FIELD_SERVICE_LOCATION", S.SYNERGIX_FIELD_SERVICE_LOCATION),
            payload.service_location,
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_CUSTOMER_CONTACT", S.SYNERGIX_FIELD_CUSTOMER_CONTACT),
            payload.prepared_by,
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_REFERENCE_NO", S.SYNERGIX_FIELD_REFERENCE_NO), payload.gl_number
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_PROJECT_CODE", S.SYNERGIX_FIELD_PROJECT_CODE), project_code
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_JOB_DATE", S.SYNERGIX_FIELD_JOB_DATE),
            payload.job_date.strftime("%d/%m/%Y"),
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_QUANTITY", S.SYNERGIX_FIELD_QUANTITY), str(payload.quantity)
        )
        await self.page.fill(
            S.require("SYNERGIX_FIELD_UNIT_PRICE", S.SYNERGIX_FIELD_UNIT_PRICE), str(payload.unit_price)
        )
        await self.page.fill(S.require("SYNERGIX_FIELD_REMARKS", S.SYNERGIX_FIELD_REMARKS), remarks)

        # Final submit — gated by DRY_RUN.
        submit_sel = S.require("SYNERGIX_QUOTATION_SUBMIT", S.SYNERGIX_QUOTATION_SUBMIT)
        if not _dry_guard(f"submit quotation for {payload.wo_po_number}"):
            await self.page.click(submit_sel)
            await self.page.wait_for_load_state("networkidle")

    async def _stage_c_schedule_board(self, payload: WOPayload) -> bool:
        """MOST FRAGILE step. Best-effort: return False (don't raise) if it can't complete.

        # TODO(human): the schedule board is likely drag/drop in D365 — confirm the real interaction.
        """
        assert self.page is not None
        logger.info("Stage C: schedule board update for %s (best-effort)", payload.wo_po_number)
        try:
            await self.page.click(S.require("SYNERGIX_SCHEDULE_BOARD_NAV", S.SYNERGIX_SCHEDULE_BOARD_NAV))
            await self.page.wait_for_load_state("networkidle")
            await self.page.click(
                S.require("SYNERGIX_SCHEDULE_BOARD_ENTRY", S.SYNERGIX_SCHEDULE_BOARD_ENTRY)
            )
            save_sel = S.require("SYNERGIX_SCHEDULE_BOARD_SAVE", S.SYNERGIX_SCHEDULE_BOARD_SAVE)
            if not _dry_guard(f"save schedule board entry for {payload.wo_po_number}"):
                await self.page.click(save_sel)
                await self.page.wait_for_load_state("networkidle")
            return True
        except Exception as exc:
            logger.warning(
                "Stage C (schedule board) did not complete for %s: %s — marking PARTIAL",
                payload.wo_po_number, exc,
            )
            return False

    async def _stage_d_attach_and_fulfil(self, payload: WOPayload) -> None:
        assert self.page is not None
        logger.info("Stage D: attach PDF + fulfil SO for %s", payload.wo_po_number)
        await self.page.click(S.require("SYNERGIX_ATTACH_PDF_BUTTON", S.SYNERGIX_ATTACH_PDF_BUTTON))
        await self.page.set_input_files(
            S.require("SYNERGIX_ATTACH_PDF_INPUT", S.SYNERGIX_ATTACH_PDF_INPUT), payload.source_path
        )
        await self.page.wait_for_load_state("networkidle")

        fulfil_sel = S.require("SYNERGIX_FULFIL_SO_BUTTON", S.SYNERGIX_FULFIL_SO_BUTTON)
        if not _dry_guard(f"fulfil service order for {payload.wo_po_number}"):
            await self.page.click(fulfil_sel)
            await self.page.wait_for_load_state("networkidle")

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
