"""One-off run: fill a single fresh WO into Synergix through client workflow-doc step 16
("put in the unit price per the job cost and remarks") and STOP there -- leaving a real,
unsubmitted draft quotation on screen for manual review.

Maps to JBTC_Service Quotation & Order Workflow.docx (paragraph numbering):
  step 4-16: create quotation, fill Customer/date/Subject/Reference No./Project Site/Item
             code/Qty/UOM/unit price/remarks -- THIS is what this script runs.
  step 17-20 (Payment/Shipment info check, Submit) and step 21-23 (Variation Order confirm) are
  deliberately NOT run here -- left for manual completion in the browser this script leaves open.

Calls _stage_b_create_quotation + _assert_details_filled directly (bypassing write()'s own
Submit/Variation Order/Schedule/Fulfil chain entirely). Neither of these two methods submits
anything, so this is safe to run even with DRY_RUN left at its default (true).

Usage:
    SYNERGIX_HEADLESS=false python -m scripts.run_stage_b_draft_only
"""
from __future__ import annotations

import asyncio

from config import settings
from src.extractor import extract_from_pdf
from src.synergix_driver import SynergixDriver

PDF_PATH = "data/pdfs/WO-PO-000066684.pdf"


async def main() -> None:
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    payload = extract_from_pdf(PDF_PATH)
    print(f"=== {payload.wo_po_number} ({PDF_PATH}) ===")

    synergix = SynergixDriver()
    await synergix.start()
    await synergix.login()
    await synergix._stage_b_create_quotation(payload)
    await synergix._assert_details_filled(payload)
    quo_id = await synergix._current_quotation_id()
    print(f"{payload.wo_po_number}: draft {quo_id} filled through step 16 (unit price + remarks). "
          "NOT submitted. Browser left open -- continue manually from Payment/Shipment info check "
          "(step 17-20) through Submit and Variation Order confirm (step 21-23).")
    print("This script will not close the browser -- press Ctrl+C here when you're done reviewing.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
