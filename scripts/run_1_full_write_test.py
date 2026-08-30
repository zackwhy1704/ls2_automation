"""One-off live verification of the FULL write() path (Stage B create + submit + Stage B.5 Variation
Order confirm + Stage C schedule + Stage D fulfil) on one fresh WO, end to end, in a single pass --
exercising the real production code path, unattended, with a real dedup check first (not skipped).

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_1_full_write_test
"""
from __future__ import annotations

import asyncio

from config import settings
from src import db
from src.extractor import extract_from_pdf
from src.models import WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
from src.validator import build_remarks, check_extraction_trust, resolve_project_code, validate

PDF_PATH = "data/pdfs/WO-PO-000066048.pdf"


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    payload = extract_from_pdf(PDF_PATH)
    wo = payload.wo_po_number
    print(f"=== {wo} ({PDF_PATH}) ===")

    errors = validate(payload)
    if errors:
        print(f"{wo}: INVALID: {'; '.join(errors)}")
        return
    trust_concerns = check_extraction_trust(payload)
    if trust_concerns:
        print(f"{wo}: NEEDS_REVIEW: {'; '.join(trust_concerns)}")
        return

    project_code = resolve_project_code(payload.job_sheet_number)
    remarks = build_remarks(payload)
    await db.upsert_payload(payload, WOStatus.APPROVED, project_code=project_code, remarks=remarks)

    synergix = SynergixDriver()
    await synergix.start()
    try:
        dedup = await synergix.check_duplicate(payload)
        print(f"{wo}: dedup check -> {dedup.value}")
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"{wo}: refusing to write -- dedup did not confirm NOT_DUPLICATE (got {dedup.value})")
            return

        result = await synergix.write(payload)
        await db.set_status(wo, result.status, error=result.detail or None)
        print(f"{wo}: -> {result.status.value}: {result.detail}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
