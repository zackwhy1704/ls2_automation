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
import sys
from pathlib import Path

from config import selectors as S
from config import settings
from src import db, notifier
from src.email_ingestor import ingest_dir, ingest_file
from src.extractor import (
    ExtractionError,
    extract,
    extract_from_pdf,
    extract_from_text,
)
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
    """TCMS scrape path: download the WO PDF, then run the shared extract/validate/queue pipeline."""
    logger.info("Processing WO %s", wo_id)
    source_path = await scraper.download_pdf(wo_id)
    await db.upsert_scraped(wo_id, source_path)
    try:
        payload = extract(source_path)  # PDF -> WOPayload
    except ExtractionError as exc:
        logger.warning("Extraction failed for %s: %s", wo_id, exc)
        await db.set_status(wo_id, WOStatus.INVALID, error=f"extraction failed: {exc}")
        return
    await _validate_dedup_queue(payload, synergix, gate)


async def _validate_dedup_queue(
    payload, synergix: SynergixDriver, gate: TelegramGate
) -> None:
    """Shared tail used by both the TCMS and email flows: validate -> dedup -> queue for approval."""
    errors = validate(payload)
    if errors:
        logger.info("WO %s invalid: %s", payload.wo_po_number, errors)
        await db.upsert_payload(payload, WOStatus.INVALID, error="; ".join(errors))
        return

    project_code = resolve_project_code(payload.job_sheet_number)
    remarks = build_remarks(payload)

    if await synergix.is_duplicate(payload.wo_po_number):
        await db.upsert_payload(payload, WOStatus.DUPLICATE, project_code=project_code, remarks=remarks)
        return

    await db.upsert_payload(
        payload, WOStatus.PENDING_APPROVAL, project_code=project_code, remarks=remarks
    )
    await gate.send_for_approval(payload)


async def run_ingest_emails(gate: TelegramGate, eml_source: str) -> int:
    """Email path: ingest email(s) -> extract -> validate -> dedup -> queue. Returns count queued.

    `eml_source` is a single .msg/.eml file or a directory of them. Each PDF attachment is one WO
    (so one email may yield several). Per-WO error isolation: one bad WO never aborts the batch.
    """
    items = ingest_dir(eml_source) if Path(eml_source).is_dir() else ingest_file(eml_source)
    if not items:
        await notifier.send_no_new_wos(gate.bot)
        return 0

    synergix = SynergixDriver()
    queued = 0
    try:
        await synergix.start()
        for wo in items:
            label = wo.attachment_name or wo.subject or wo.primary_source_path
            try:
                # Prefer the PDF attachment; fall back to inline body text. The WO-PO number is not
                # known until after extraction, so the DB row is created keyed by it post-extract.
                if wo.has_pdf:
                    payload = extract_from_pdf(wo.pdf_path)
                else:
                    payload = extract_from_text(wo.body_text, source_path=wo.source_email_path)
                await db.upsert_scraped(payload.wo_po_number, payload.source_path)

                await _validate_dedup_queue(payload, synergix, gate)
                if (rec := await db.get(payload.wo_po_number)) and \
                        rec["status"] == WOStatus.PENDING_APPROVAL.value:
                    queued += 1
            except ExtractionError as exc:
                logger.warning("Extraction failed for email %r: %s", label, exc)
                await notifier.send_text(gate.bot, f"⛔ Could not extract WO from email '{label}': {exc}")
            except S.MissingSelectorError as exc:
                logger.error("MISSING SELECTOR: %s (email %r) — continuing", exc, label)
                await notifier.send_text(gate.bot, f"⚠️ Synergix selector '{exc}' not filled — '{label}' skipped.")
            except Exception as exc:
                logger.exception("Unhandled error processing email %r — continuing", label)
                await notifier.send_text(gate.bot, f"❌ Error processing email '{label}': {exc}")
    finally:
        await synergix.close()
    return queued


async def main(eml_source: str | None = None) -> None:
    """Entry point.

    eml_source given -> email flow (ingest .eml file/dir). Otherwise -> TCMS scrape flow.
    """
    settings.configure_logging()
    logger.info("Starting JBTC billing MVP — %s", settings.summary())
    await db.init_db()

    gate = TelegramGate()
    # Start the bot (polling for callbacks) without blocking, so we can run the source job too.
    await gate.app.initialize()
    await gate.app.start()
    await gate.app.updater.start_polling()
    logger.info("Telegram bot polling for approvals")

    try:
        if eml_source:
            logger.info("Email flow: ingesting from %s", eml_source)
            queued = await run_ingest_emails(gate, eml_source)
            logger.info("Ingest complete: %d WO(s) queued for approval", queued)
        else:
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
    # Usage:
    #   python -m src.main                       # TCMS scrape flow
    #   python -m src.main --emails path/to/dir  # email flow (dir of .eml or a single .eml)
    _eml: str | None = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--emails":
        _eml = sys.argv[2]
    elif len(sys.argv) == 2 and sys.argv[1].endswith((".eml",)):
        _eml = sys.argv[1]
    asyncio.run(main(_eml))
