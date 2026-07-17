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
from enum import Enum

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
        # JSF/PrimeFaces app keeps connections open, so wait on the login form, not networkidle.
        await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_selector(
            S.require("SYNERGIX_USERNAME_INPUT", S.SYNERGIX_USERNAME_INPUT)
        )
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
    def _dedup_search_value(self, payload: WOPayload) -> str:
        """The WO field to search Synergix on, per SYNERGIX_DEDUP_KEY config."""
        if settings.SYNERGIX_DEDUP_KEY == "job_sheet":
            return payload.job_sheet_number
        return payload.wo_po_number

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
            await self.login()
            assert self.page is not None
            await self.page.click(S.require("SYNERGIX_DEDUP_NAV", S.SYNERGIX_DEDUP_NAV))
            await self.page.wait_for_load_state("networkidle")
            await self.page.fill(
                S.require("SYNERGIX_DEDUP_SEARCH_INPUT", S.SYNERGIX_DEDUP_SEARCH_INPUT), search_value
            )
            await self.page.click(
                S.require("SYNERGIX_DEDUP_SEARCH_SUBMIT", S.SYNERGIX_DEDUP_SEARCH_SUBMIT)
            )
            await self.page.wait_for_load_state("networkidle")

            match_count = await self.page.locator(
                S.require("SYNERGIX_DEDUP_RESULT_ROW", S.SYNERGIX_DEDUP_RESULT_ROW)
            ).count()
            no_result = await self.page.locator(
                S.require("SYNERGIX_DEDUP_NO_RESULT_MARKER", S.SYNERGIX_DEDUP_NO_RESULT_MARKER)
            ).count()

            if match_count > 0:
                logger.info("Dedup %s: DUPLICATE (%d match(es) in Synergix)", search_value, match_count)
                return DedupResult.DUPLICATE
            if no_result > 0:
                logger.info("Dedup %s: NOT_DUPLICATE (Synergix reports no records)", search_value)
                return DedupResult.NOT_DUPLICATE
            # Neither a match nor an explicit "no records" marker — can't be sure. Fail safe.
            logger.warning("Dedup %s: UNCERTAIN (no result rows and no no-result marker)", search_value)
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
