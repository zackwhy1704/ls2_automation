"""Live batch verification of the FULL write() path (Stage A extract -> B create+submit -> B.5
Variation Order confirm -> C schedule -> D fulfil) across 5 fresh WOs, one Synergix session, one
WO at a time -- exercising the real production write() path end to end, not individual stages in
isolation.

All 5 WOs below are confirmed (via docs/synergix_workflow.md history) to have no prior Synergix
quotation from earlier testing sessions -- picked specifically to avoid creating duplicate
quotations against WOs already exercised. (Second run of this script, 2026-08-26: the first 5
-- 79157/79145/80321/80418/78989 -- all got stuck at Stage B.5, so this run uses a fresh set to
avoid piling more duplicate drafts onto already-stuck WOs.)

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_5_full_write_batch
"""
from __future__ import annotations

import asyncio

from config import settings
from src import db
from src.extractor import extract_from_pdf
from src.models import WOStatus
from src.synergix_driver import SynergixDriver
from src.validator import build_remarks, check_extraction_trust, resolve_project_code, validate

PDF_PATHS = [
    "data/pdfs/WO-PO-000076329.pdf",
    "data/pdfs/WO-PO-000065535.pdf",
    "data/pdfs/WO-PO-000065536.pdf",
    "data/pdfs/WO-PO-000078561.pdf",
    "data/pdfs/WO-PO-000070456.pdf",
]


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    synergix = SynergixDriver()
    await synergix.start()
    results: list[tuple[str, str, str]] = []
    try:
        for pdf_path in PDF_PATHS:
            payload = extract_from_pdf(pdf_path)
            wo = payload.wo_po_number
            print(f"\n=== {wo} ({pdf_path}) ===")

            errors = validate(payload)
            if errors:
                msg = "; ".join(errors)
                print(f"{wo}: INVALID: {msg}")
                results.append((wo, "INVALID", msg))
                continue
            trust_concerns = check_extraction_trust(payload)
            if trust_concerns:
                msg = "; ".join(trust_concerns)
                print(f"{wo}: NEEDS_REVIEW: {msg}")
                results.append((wo, "NEEDS_REVIEW", msg))
                continue

            project_code = resolve_project_code(payload.job_sheet_number)
            remarks = build_remarks(payload)
            await db.upsert_payload(payload, WOStatus.APPROVED, project_code=project_code, remarks=remarks)

            print(f"{wo}: dedup check SKIPPED (test run) -- calling write()")
            result = await synergix.write(payload)
            await db.set_status(wo, result.status, error=result.detail or None)
            print(f"{wo}: -> {result.status.value}: {result.detail}")
            results.append((wo, result.status.value, result.detail))
    finally:
        await synergix.close()

    print("\n=== BATCH SUMMARY ===")
    for wo, status, detail in results:
        print(f"{wo}: {status} -- {detail}")


if __name__ == "__main__":
    asyncio.run(main())
