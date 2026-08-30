"""One-off live test: abort a confirmed-orphaned draft quotation (never submitted, Rev. No. 0, left
over from an earlier test session), then run the REAL, unattended write() pipeline (Stage
A-B-B.5-C-D, DRY_RUN=false) on the WO it was blocking dedup for.

Confirmed live (2026-08-30) that WO-PO/000080935's dedup DUPLICATE was QUO0006569 -- a fully-filled
but never-submitted draft (Rev. No. 0) from an earlier test run, not real client billing history.
Safe to abort per abort_quotation()'s own contract (only works on an un-submitted draft).

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false TCMS_HEADLESS=false python -m scripts.run_1_after_abort_test
"""
from __future__ import annotations

import asyncio

from config import settings
from src import db
from src.models import WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
from src.tcms_scraper import TCMSScraper
from src.validator import build_remarks, check_extraction_trust, resolve_project_code, validate

QUOTATION_TO_ABORT = "QUO0006569"
WO_PO_NUMBER = "WO-PO/000080935"


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
    try:
        print(f"=== Aborting orphaned draft {QUOTATION_TO_ABORT} ===")
        aborted = await synergix.abort_quotation(QUOTATION_TO_ABORT)
        print(f"{QUOTATION_TO_ABORT}: abort {'succeeded' if aborted else 'FAILED'}")
        if not aborted:
            print("Refusing to continue -- abort did not succeed.")
            return

        print(f"\n=== Downloading {WO_PO_NUMBER}'s WO PDF from TCMS ===")
        async with TCMSScraper() as tcms:
            await tcms.login()
            downloaded = await tcms.download_pdf(WO_PO_NUMBER)
        print(f"Downloaded: {downloaded.path}")

        from src.extractor import extract_from_pdf
        payload = extract_from_pdf(str(downloaded.path))
        wo = payload.wo_po_number
        print(f"=== {wo} ({downloaded.path}) ===")

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

        dedup = await synergix.check_duplicate(payload)
        print(f"{wo}: dedup check (post-abort) -> {dedup.value}")
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"{wo}: refusing to write -- dedup did not confirm NOT_DUPLICATE (got {dedup.value})")
            return

        result = await synergix.write(payload)
        await db.set_status(wo, result.status, error=result.detail or None)
        print(f"\n{wo}: FINAL RESULT -> {result.status.value}: {result.detail}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
