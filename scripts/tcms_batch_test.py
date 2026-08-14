"""One-off proof run: full TCMS -> extract -> validate -> dedup -> Telegram approval, capped to N WOs.

Runs the SAME production pipeline functions as main.run_scrape (download_pdf, extract,
_validate_dedup_queue) so it proves the real chain, but limits to the first N un-invoiced WOs so the
test doesn't download 140 PDFs or flood Telegram. Synergix is stubbed; approvals appear in Telegram.

    HEADLESS=false python -m scripts.tcms_batch_test 3
"""
from __future__ import annotations

import asyncio
import logging
import sys

from config import settings
from src import db, notifier
from src.main import _validate_dedup_queue
from src.extractor import ExtractionError, extract
from src.models import WOStatus
from src.synergix_driver import SynergixDriver
from src.tcms_scraper import TCMSScraper
from src.telegram_gate import TelegramGate

logger = logging.getLogger(__name__)


async def main(limit: int) -> None:
    settings.configure_logging()
    logger.info("TCMS batch test (limit=%d) — %s", limit, settings.summary())
    await db.init_db()

    gate = TelegramGate()
    await gate.app.initialize()
    await gate.app.start()
    await gate.app.updater.start_polling()
    logger.info("Telegram polling; sending up to %d WO(s) for approval", limit)

    synergix = SynergixDriver()
    queued = 0
    try:
        await synergix.start()
        async with TCMSScraper() as scraper:
            await scraper.login()
            wo_ids = (await scraper.list_uninvoiced())[:limit]
            logger.info("Processing %d WO(s): %s", len(wo_ids), wo_ids)
            for wo_id in wo_ids:
                try:
                    downloaded = await scraper.download_pdf(wo_id)
                    await db.upsert_scraped(wo_id, downloaded.path)
                    payload = extract(downloaded.path)
                    payload.property_officer = downloaded.property_officer or None
                    await _validate_dedup_queue(payload, synergix, gate)
                    if (rec := await db.get(payload.wo_po_number)) and \
                            rec["status"] == WOStatus.PENDING_APPROVAL.value:
                        queued += 1
                except ExtractionError as exc:
                    logger.warning("Extraction failed for %s: %s", wo_id, exc)
                    await notifier.send_text(gate.bot, f"⛔ Could not extract {wo_id}: {exc}")
                except Exception as exc:
                    logger.exception("Error processing %s", wo_id)
                    await notifier.send_text(gate.bot, f"❌ Error on {wo_id}: {exc}")
        await notifier.send_batch_summary(gate.bot)
        logger.info("Queued %d WO(s). Waiting for approvals — Ctrl-C to stop.", queued)
        if queued:
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await synergix.close()
        await gate.shutdown()
        await gate.app.updater.stop()
        await gate.app.stop()
        await gate.app.shutdown()
        logger.info("Stopped.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    asyncio.run(main(n))
