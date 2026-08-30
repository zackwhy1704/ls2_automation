"""One-off live verification of _schedule_stage_c + _fulfil_stage_d against an existing, already
Variation-Order-confirmed Service Order (SV00008877, WO-PO/000080935 / QUO0006817) -- bypasses
Stage B/B.5 since they're already done for this WO, to isolate testing to Stage C and D.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_stage_c_direct_test
"""
from __future__ import annotations

import asyncio

from config import settings
from src.extractor import extract_from_pdf
from src.synergix_driver import SynergixDriver

PDF_PATH = "data/pdfs/WO-PO-000080935.pdf"


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    payload = extract_from_pdf(PDF_PATH)
    print(f"=== {payload.wo_po_number} ({PDF_PATH}) ===")

    synergix = SynergixDriver()
    await synergix.start()
    try:
        ok = await synergix._schedule_stage_c(payload)
        print(f"{payload.wo_po_number}: Stage C {'SUCCEEDED' if ok else 'FAILED'}")
        if ok:
            ok_d = await synergix._fulfil_stage_d(payload)
            print(f"{payload.wo_po_number}: Stage D {'SUCCEEDED' if ok_d else 'FAILED'}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
