"""Orchestrator / entry point.

MVP simplification: a single long-running process. We scrape, extract, validate, dedup, and queue
each surviving WO for Telegram approval; the bot then stays alive listening for approvals and
executes the Synergix write on each callback.

----------------------------------------------------------------------------------------------------
PRODUCTION NOTE (out of scope for MVP):
  Decouple this into two pieces that share the SQLite state, so nothing has to hold a browser session
  open for hours waiting on an async human approval:
    1. A SCHEDULED scrape+notify job: scrape TCMS, extract, validate, dedup, write PENDING_APPROVAL
       rows to the DB, and send Telegram approval requests. Then exit.
    2. A separate APPROVAL-EXECUTOR (e.g. a small webhook service) triggered by the Telegram callback:
       it reads the approved WO from the DB and performs the Synergix write on demand.
  Both read/write the same state.db. Add a real secret manager in place of local .env.
----------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import asyncio
import logging

from config import selectors as S
from config import settings
from src import db, notifier
from src.extractor import ExtractionError, extract
from src.models import WOStatus
from src.synergix_driver import SynergixDriver
from src.tcms_scraper import TCMSScraper
from src.telegram_gate import TelegramGate
from src.validator import build_remarks, resolve_project_code, validate

logger = logging.getLogger(__name__)


async def run_scrape(gate: TelegramGate) -> int:
    """Scrape -> extract -> validate -> dedup -> queue for approval. Returns count queued."""
    # Duplicate check uses its own Synergix session (read-only search).
    synergix = SynergixDriver()
    queued = 0
    try:
        await synergix.start()
        async with TCMSScraper() as scraper:
            try:
                await scraper.login()
                wo_ids = await scraper.list_uninvoiced()
            except S.MissingSelectorError as exc:
                logger.error("MISSING SELECTOR: %s — fill it in config/selectors.py before scraping", exc)
                await notifier.send_text(
                    gate.bot, f"⚠️ Scrape blocked: selector '{exc}' is not filled in yet."
                )
                return 0

            if not wo_ids:
                await notifier.send_no_new_wos(gate.bot)
                return 0

            for wo_id in wo_ids:
                # Per-WO error isolation: one failure never aborts the batch.
                try:
                    await _process_one(wo_id, scraper, synergix, gate)
                    if (rec := await db.get(wo_id)) and rec["status"] == WOStatus.PENDING_APPROVAL.value:
                        queued += 1
                except S.MissingSelectorError as exc:
                    logger.error("MISSING SELECTOR: %s (WO %s) — marking FAILED, continuing", exc, wo_id)
                    await db.set_status(wo_id, WOStatus.FAILED, error=f"missing selector: {exc}")
                except Exception as exc:
                    logger.exception("Unhandled error processing WO %s — marking FAILED, continuing", wo_id)
                    await db.set_status(wo_id, WOStatus.FAILED, error=str(exc))
    finally:
        await synergix.close()
    return queued


async def _process_one(
    wo_id: str, scraper: TCMSScraper, synergix: SynergixDriver, gate: TelegramGate
) -> None:
    logger.info("Processing WO %s", wo_id)
    pdf_path = await scraper.download_pdf(wo_id)
    await db.upsert_scraped(wo_id, pdf_path)

    # Extract
    try:
        payload = extract(pdf_path)
    except ExtractionError as exc:
        logger.warning("Extraction failed for %s: %s", wo_id, exc)
        await db.set_status(wo_id, WOStatus.INVALID, error=f"extraction failed: {exc}")
        return

    # Validate
    errors = validate(payload)
    if errors:
        logger.info("WO %s invalid: %s", payload.wo_po_number, errors)
        await db.upsert_payload(payload, WOStatus.INVALID, error="; ".join(errors))
        return

    project_code = resolve_project_code(payload.job_sheet_number)
    remarks = build_remarks(payload)

    # Duplicate check
    if await synergix.is_duplicate(payload.wo_po_number):
        await db.upsert_payload(payload, WOStatus.DUPLICATE, project_code=project_code, remarks=remarks)
        return

    # Queue for approval
    await db.upsert_payload(
        payload, WOStatus.PENDING_APPROVAL, project_code=project_code, remarks=remarks
    )
    await gate.send_for_approval(payload)


async def main() -> None:
    settings.configure_logging()
    logger.info("Starting JBTC billing MVP — %s", settings.summary())
    await db.init_db()

    gate = TelegramGate()
    # Start the bot (polling for callbacks) without blocking, so we can run the scrape too.
    await gate.app.initialize()
    await gate.app.start()
    await gate.app.updater.start_polling()
    logger.info("Telegram bot polling for approvals")

    try:
        queued = await run_scrape(gate)
        logger.info("Scrape complete: %d WO(s) queued for approval", queued)
        await notifier.send_batch_summary(gate.bot)

        if queued == 0:
            logger.info("Nothing pending approval; shutting down.")
        else:
            logger.info("Waiting for approvals via Telegram. Ctrl-C to stop.")
            # Keep the process alive so callbacks can be handled. The admin may approve hours later.
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested")
    finally:
        await notifier.send_batch_summary(gate.bot)
        await gate.shutdown()
        await gate.app.updater.stop()
        await gate.app.stop()
        await gate.app.shutdown()
        logger.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
