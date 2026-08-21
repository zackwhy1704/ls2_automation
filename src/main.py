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
from src.console_gate import ConsoleGate
from src.email_ingestor import ingest_dir, ingest_file
from src.extractor import (
    ExtractionError,
    extract,
    extract_from_pdf,
    extract_from_text,
)
from src.models import WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
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
    from src.tcms_scraper import WOAlreadyProcessedError

    logger.info("Processing WO %s", wo_id)
    try:
        downloaded = await scraper.download_pdf(wo_id)
    except WOAlreadyProcessedError as exc:
        logger.info("WO %s TCMS status=%r, not Received — skipping", wo_id, exc.status)
        await db.set_status(wo_id, WOStatus.DUPLICATE, error=f"TCMS shows status={exc.status!r}")
        return
    await db.upsert_scraped(wo_id, downloaded.path)
    try:
        payload = extract(downloaded.path)  # PDF -> WOPayload
        payload.property_officer = downloaded.property_officer or None
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

    # Checkpoint 1 of 2: is this WO already invoiced in Synergix? (JBTC's un-invoiced list is
    # hand-maintained and can be stale.) Fail-safe: an uncertain check is flagged for a human, never
    # auto-queued for billing. The write path re-checks again just before creating the invoice.
    dedup = await synergix.check_duplicate(payload)
    if dedup is DedupResult.DUPLICATE:
        logger.info("WO %s already invoiced in Synergix — skipping", payload.wo_po_number)
        await db.upsert_payload(payload, WOStatus.DUPLICATE, project_code=project_code, remarks=remarks)
        return
    if dedup is DedupResult.UNCERTAIN:
        logger.warning("WO %s: could not verify invoiced status — flagging for review", payload.wo_po_number)
        await db.upsert_payload(
            payload, WOStatus.NEEDS_REVIEW, project_code=project_code, remarks=remarks,
            error="Synergix duplicate check inconclusive — verify manually it is NOT already invoiced",
        )
        await notifier.send_text(
            gate.bot,
            f"⚠️ {payload.wo_po_number}: could NOT confirm whether it's already invoiced in Synergix. "
            "Left as NEEDS_REVIEW — verify manually before billing to avoid a double invoice.",
        )
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


def _build_gate(force_console: bool):
    """Pick the approval gate: real Telegram when configured, else a console stand-in.

    Falls back to ConsoleGate (auto-approving through the stubbed Synergix driver) when
    TELEGRAM_BOT_TOKEN is unset or --no-telegram is passed, so the pipeline is runnable today.
    """
    if force_console or not settings.TELEGRAM_BOT_TOKEN.strip():
        if not force_console:
            logger.warning("TELEGRAM_BOT_TOKEN not set — using console approval gate (auto-approve).")
        return ConsoleGate(auto_approve=True)
    return TelegramGate()


async def _poll_mailbox(gate) -> str:
    """Pull new WO emails from the IMAP mailbox into INCOMING_EMAIL_DIR; return that dir to ingest.

    Always returns the incoming dir so a `--poll` run stays in the email flow (and ingests any
    already-downloaded .eml left there) rather than falling through to the TCMS scrape. Runs the
    blocking imaplib poll in a thread; an unconfigured mailbox or a connection/auth error is reported
    but never aborts the batch.
    """
    from src import mail_poller

    if not mail_poller.imap_configured():
        await notifier.send_text(gate.bot, "⚠️ Poll requested but IMAP is not configured in .env.")
    else:
        try:
            saved = await asyncio.to_thread(mail_poller.poll_once)
            logger.info("Mailbox poll downloaded %d new email(s)", len(saved))
        except Exception as exc:
            logger.exception("Mailbox poll failed")
            await notifier.send_text(gate.bot, f"❌ Mailbox poll failed: {exc}")
    return str(settings.INCOMING_EMAIL_DIR)


async def main(
    eml_source: str | None = None, *, no_telegram: bool = False, poll: bool = False
) -> None:
    """Entry point.

    poll=True       -> pull emails from the IMAP mailbox, then run the email flow on them.
    eml_source given -> email flow (ingest .msg/.eml file/dir). Otherwise -> TCMS scrape flow.
    """
    settings.configure_logging()
    logger.info("Starting JBTC billing MVP — %s", settings.summary())
    await db.init_db()

    gate = _build_gate(no_telegram)
    # Start the bot (polling for callbacks) without blocking, so we can run the source job too.
    await gate.app.initialize()
    await gate.app.start()
    await gate.app.updater.start_polling()
    logger.info("Approval gate ready (%s)", type(gate).__name__)

    try:
        # poll pulls new emails from the IMAP mailbox into INCOMING_EMAIL_DIR, then ingests that dir.
        if poll and not eml_source:
            eml_source = await _poll_mailbox(gate)

        if eml_source:
            logger.info("Email flow: ingesting from %s", eml_source)
            queued = await run_ingest_emails(gate, eml_source)
            logger.info("Ingest complete: %d WO(s) queued for approval", queued)
        else:
            queued = await run_scrape(gate)
            logger.info("Scrape complete: %d WO(s) queued for approval", queued)
        await notifier.send_batch_summary(gate.bot)

        # The console gate approves synchronously above, so only the Telegram gate needs to stay
        # alive waiting on async human callbacks.
        if queued == 0 or not isinstance(gate, TelegramGate):
            logger.info("No pending async approvals; shutting down.")
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


async def run_batch(
    source: str | None, *, poll: bool = False, limit: int | None = None, skip: int = 0,
    max_new: int | None = None, no_telegram: bool = False,
) -> None:
    """Batch pipeline (the recommended path): intake -> auto-submit -> report.

    source: an emails file/dir to ingest, or None to scrape TCMS. `poll` pulls the IMAP mailbox first.
    `limit` caps how many WOs are handled (TCMS path) for sampling; `skip` drops that many from the
    front first, so consecutive runs can cover consecutive slices (e.g. skip=0/limit=50, then
    skip=50/limit=50) instead of re-processing the same WOs. `max_new` instead stops once that many
    non-duplicate WOs have been attempted — for a live run that wants N real quotations without
    scanning the whole mostly-already-invoiced list. Every valid, non-duplicate WO is
    auto-submitted (gated by DRY_RUN); the outcome of every WO is reported (Telegram/email) for the
    team to spot-check in Synergix. `no_telegram` (the `--no-telegram` CLI flag) suppresses that
    report send — logs the report as usual but posts nothing — for testing against production
    Synergix/TCMS without also spamming the real ops Telegram chat.
    """
    from src.batch import WOOutcome, run_batch_from_emails, run_batch_from_tcms
    from src.models import WOStatus
    from src import report as report_mod

    settings.configure_logging()
    logger.info("Batch pipeline — %s%s", settings.summary(),
                f" (skip={skip}, limit={limit}, max_new={max_new})"
                if (skip or limit or max_new) else "")
    await db.init_db()

    intake_review: list = []
    if poll and not source:
        mode = (settings.SKTC_INTAKE_MODE or "imap").strip().lower()
        # "outlook" and "folder" share a contract: (pdf_paths, review_items), PDFs written into
        # INCOMING_EMAIL_DIR, and an exception on an unreachable source rather than a quiet zero.
        # Both are handled identically here so the report reads the same whichever is configured.
        intake = None
        if mode == "outlook":
            from src.sktc_outlook_intake import poll_outlook_once
            intake = ("outlook", poll_outlook_once)
        elif mode == "folder":
            from src.sktc_folder_intake import poll_folder_once
            intake = ("folder", poll_folder_once)

        if intake:
            label, poll_fn = intake
            try:
                _pdfs, review_items = await asyncio.to_thread(poll_fn)
            except Exception as exc:
                # Loud, not swallowed: an unreachable intake source must not look like "ran fine,
                # found 0 WOs". For Outlook that means a signed-out or closed Outlook; for the
                # folder, a paused OneDrive sync. Either would otherwise stop billing silently.
                logger.exception("SKTC %s intake failed", label)
                intake_review.append(
                    WOOutcome("SKTC-INTAKE", WOStatus.FAILED, f"{label} intake failed: {exc}")
                )
            else:
                intake_review.extend(
                    WOOutcome(item.identifier, WOStatus.INVALID, item.reason) for item in review_items
                )
        else:
            from src import mail_poller
            if mail_poller.imap_configured():
                await asyncio.to_thread(mail_poller.poll_once)
        source = str(settings.INCOMING_EMAIL_DIR)

    if source:
        logger.info("Batch: ingesting WOs from %s", source)
        result = await run_batch_from_emails(source)
    else:
        logger.info("Batch: scraping + processing un-invoiced WOs from TCMS (streaming)")
        result = await run_batch_from_tcms(limit=limit, skip=skip, max_new=max_new)

    result.outcomes[:0] = intake_review  # surface orphans/unverified senders in the same report

    await report_mod.send_report(result, send=not no_telegram)
    logger.info("Batch complete: %d WO(s) processed", len(result.outcomes))


if __name__ == "__main__":
    # Usage:
    #   python -m src.main --batch                       # RECOMMENDED: TCMS scrape -> auto-submit -> report
    #   python -m src.main --batch --limit 20            # cap to the first 20 WOs (sampling)
    #   python -m src.main --batch --skip 50 --limit 50  # cover the NEXT 50 (e.g. after a prior
    #                                                         --limit 50 run), not the same 50 again
    #   python -m src.main --batch --max-new 10          # stop once 10 NON-duplicate WOs have been
    #                                                         attempted, however far down the list that
    #                                                         is (most of the list is already invoiced)
    #   python -m src.main --batch --poll                # pull IMAP emails, then batch-process
    #   python -m src.main --batch --emails path/to/dir  # batch-process a dir/file of .msg/.eml/.pdf
    #   python -m src.main --batch --no-telegram --limit 3  # dry-run against prod TCMS/Synergix
    #                                                         # without posting to the real Telegram chat
    #   --- legacy Telegram-approval flows: ---
    #   python -m src.main                               # TCMS scrape -> Telegram approval
    #   python -m src.main --emails path/to/dir          # email flow -> Telegram approval
    #   python -m src.main --emails DIR --no-telegram    # console approval gate
    argv = sys.argv[1:]
    _batch = "--batch" in argv
    _no_tg = "--no-telegram" in argv
    _poll = "--poll" in argv

    _limit: int | None = None
    if "--limit" in argv:
        i = argv.index("--limit")
        if i + 1 < len(argv):
            _limit = int(argv[i + 1])
            del argv[i:i + 2]
    _skip = 0
    if "--skip" in argv:
        i = argv.index("--skip")
        if i + 1 < len(argv):
            _skip = int(argv[i + 1])
            del argv[i:i + 2]
    _max_new: int | None = None
    if "--max-new" in argv:
        i = argv.index("--max-new")
        if i + 1 < len(argv):
            _max_new = int(argv[i + 1])
            del argv[i:i + 2]
    argv = [a for a in argv if a not in ("--no-telegram", "--poll", "--batch")]

    _eml: str | None = None
    if len(argv) >= 2 and argv[0] == "--emails":
        _eml = argv[1]
    elif len(argv) == 1 and argv[0].endswith((".eml", ".msg")):
        _eml = argv[0]

    if _batch:
        asyncio.run(run_batch(_eml, poll=_poll, limit=_limit, skip=_skip, max_new=_max_new,
                              no_telegram=_no_tg))
    else:
        asyncio.run(main(_eml, no_telegram=_no_tg, poll=_poll))
