"""One-off live test: run the REAL, unattended write() pipeline (Stage A-B-B.5-C-D, DRY_RUN=false)
on a WO whose orphaned draft quotation (never submitted, Rev. No. 0, left over from an earlier test
session) was already manually verified and aborted.

WO-PO/000078714's draft (QUO0006815) was individually verified live (2026-08-30) -- Salesperson TAN
WEI YING, our item code SE-400212A, our exact remarks/bank-details format, Rev. No. 0, never
submitted -- and aborted with the user's explicit confirmation before this script runs. This script
does NOT abort anything itself; it only downloads the WO fresh from TCMS and runs write() on it, to
live-verify the _service_order_exists_for_quotation polling fix (src/synergix_driver.py) with full
logger output enabled this time (the first run of this script had no logging.basicConfig() call, so
_confirm_variation_order's own info/warning logs were silently dropped and the real decision path
that led to a duplicate Service Order (SV00008879 + SV00008880 for QUO0006818) was never visible).

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false TCMS_HEADLESS=false python -m scripts.run_1_after_abort_test
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src import db
from src.models import WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
from src.tcms_scraper import TCMSScraper
from src.validator import build_remarks, check_extraction_trust, resolve_project_code, validate

WO_PO_NUMBER = "WO-PO/000078714"


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
