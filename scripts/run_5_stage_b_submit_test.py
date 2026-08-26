"""One-off verification run: 5 real JBTC WOs, end to end through Stage B, WITH a real Submit.

Requested by the user to unblock Stage C discovery -- Schedule Board only shows orders whose Stage B
quotation has actually been SUBMITTED (a DRY_RUN draft never appears there). Every sample WO already
has some prior test-run state in data/state.db (partial draft, or a `duplicate` verdict against that
same prior draft) -- the user explicitly said to ignore those dedup hits ("those are self reported
and testing") for this run.

Deliberately bypasses check_duplicate() for this run only -- NOT a change to src/batch.py or
SynergixDriver, which keep the real dedup guard for actual production use. This script is scoped to
copy.taskhub.ls2.sg (non-production copy environment), never production.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_5_stage_b_submit_test
"""
from __future__ import annotations

import asyncio
import logging

from config import settings
from src import db
from src.extractor import extract_from_pdf
from src.models import WOStatus
from src.synergix_driver import SynergixDriver
from src.validator import build_remarks, check_extraction_trust, resolve_project_code, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PDF_PATHS = [
    "data/samples/JBTC/76625.pdf",
    "data/samples/JBTC/76627.pdf",
    "data/samples/JBTC/76639.pdf",
    "data/samples/JBTC/76640.pdf",
    "data/samples/JBTC/78228.pdf",
]


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- this run would not actually submit anything. "
              "Re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} does not look like the "
              "non-production copy environment. Aborting for safety.")
        return

    synergix = SynergixDriver()
    await synergix.start()
    try:
        for path in PDF_PATHS:
            try:
                payload = extract_from_pdf(path)
            except Exception as exc:
                print(f"{path}: EXTRACTION FAILED: {exc}")
                continue

            wo = payload.wo_po_number
            print(f"\n=== {wo} ({path}) ===")

            errors = validate(payload)
            if errors:
                print(f"{wo}: INVALID: {'; '.join(errors)}")
                continue

            trust_concerns = check_extraction_trust(payload)
            if trust_concerns:
                print(f"{wo}: NEEDS_REVIEW (extraction trust): {'; '.join(trust_concerns)}")
                continue

            project_code = resolve_project_code(payload.job_sheet_number)
            remarks = build_remarks(payload)
            await db.upsert_payload(payload, WOStatus.APPROVED, project_code=project_code, remarks=remarks)

            # Dedup deliberately skipped for this run -- see module docstring.
            print(f"{wo}: dedup check SKIPPED (test run, per user instruction) -- calling write()")
            result = await synergix.write(payload)
            await db.set_status(wo, result.status, error=result.detail or None)
            print(f"{wo}: -> {result.status.value}: {result.detail}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
