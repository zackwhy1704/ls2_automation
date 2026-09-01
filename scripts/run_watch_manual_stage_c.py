"""Create a fresh WO through Stage A/B/B.5 only, then get onto Schedule Board and stop right after
selecting the order row -- holding the browser open so the user can manually demonstrate the exact
Stage C clicks (tick the specific employee, wait for calendar to load, click a cell). We do NOT
click Employee or anything else automatically here -- we want to watch the user's exact sequence.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_watch_manual_stage_c <suffix>
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src.models import LineItem, WOPayload
from src.synergix_driver import DedupResult, SynergixDriver

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "watch1"
_char_sum = sum(ord(c) * (i + 1) for i, c in enumerate(SUFFIX))
_suffix_digits = f"{_char_sum % 90000000 + 10000000:08d}"
WO_NUMBER = f"WO-PO/9{_suffix_digits}"
WO_BARE = WO_NUMBER.replace("WO-PO/", "")
JOB_DATE = date.today() - timedelta(days=1 + (_char_sum % 3650))


def build_payload() -> WOPayload:
    return WOPayload(
        wo_po_number=WO_NUMBER,
        town_council="JALAN BESAR TOWN COUNCIL",
        job_sheet_number=f"TEST{SUFFIX}",
        service_location="Blk 52 Chin Swee Road (SYNTHETIC TEST RECORD)",
        nature_of_work="SYNTHETIC TEST -- watch manual Stage C, safe to delete",
        job_date=JOB_DATE,
        prepared_by="test-harness",
        gl_number="431-KY-KYR5P1-160052-0-721010-0000",
        quantity=1.0, unit_price=30.0,
        line_items=[LineItem(description="SYNTHETIC TEST -- watch manual Stage C",
                             quantity=1.0, unit_price=30.0, discount_percent=10.0,
                             discount_amount=3.0, net_amount=33.0)],
        discount_percent=10.0, discount_amount=3.0, net_amount=33.0, gst_percent=9.0,
        grand_total=35.97, source_path=f"watch-{SUFFIX}",
    )


async def main() -> None:
    if settings.DRY_RUN or "copy." not in settings.SYNERGIX_BASE_URL:
        print("DRY_RUN true or not copy env -- aborting.", flush=True)
        return

    payload = build_payload()
    print(f"=== Creating {WO_NUMBER}, job_date={JOB_DATE} ===", flush=True)
    d = SynergixDriver()
    await d.start()
    page = d.page
    try:
        dedup = await d.check_duplicate(payload)
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"dedup not clean ({dedup.value}) -- aborting", flush=True)
            return

        # Stage B + B.5 only, via the real helpers, stopping before Stage C.
        await d.login()
        print("=== Stage B: creating quotation ===", flush=True)
        await d._stage_b_create_quotation(payload)
        await d._assert_details_filled(payload)
        quo_id, vo_confirmed = await d._submit_quotation(payload)
        print(f"=== Submitted {quo_id}, vo_confirmed={vo_confirmed} ===", flush=True)
        if quo_id and not vo_confirmed:
            vo_confirmed = await d._confirm_variation_order(quo_id)
        print(f"=== Stage B/B.5 done (VO confirmed={vo_confirmed}) ===", flush=True)

        # Get onto Schedule Board and select the row, then STOP.
        await d._open_schedule_board()
        header = page.locator("th:visible", has_text="Enquiry/Subject").first
        fi = header.locator("input.ui-column-filter").first
        await fi.click(); await fi.fill(WO_BARE); await fi.press("Enter")
        await page.wait_for_timeout(5000)
        row = page.locator("tr", has_text=WO_BARE).locator("visible=true").first
        if await row.count():
            cb = row.locator(".ui-chkbox-box").locator("visible=true").first
            if await cb.count():
                await d._click_when_clear(cb, timeout_ms=10000)
            else:
                await row.click(timeout=10000)
            await page.wait_for_timeout(3000)
            print(f"=== Row {WO_BARE} selected. Job date is {JOB_DATE.strftime('%d/%m/%Y')} ===", flush=True)

        print(f"\n{'='*70}", flush=True)
        print("STOPPED after row selection. Please now demonstrate Stage C MANUALLY:", flush=True)
        print("  1. Click the Employee toggle (and/or tick the specific employee 800SUPER)", flush=True)
        print("  2. WAIT for the calendar to fully load", flush=True)
        print(f"  3. Navigate to the job date {JOB_DATE.strftime('%d/%m/%Y')} and click a cell", flush=True)
        print("  4. Fill the popup, Confirm, Submit", flush=True)
        print("Tell me what you clicked and I'll capture the exact elements.", flush=True)
        print(f"{'='*70}\n", flush=True)
        print("Browser open indefinitely. Ctrl+C when done.", flush=True)
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nInterrupted -- closing.", flush=True)
    finally:
        await d.close()


if __name__ == "__main__":
    asyncio.run(main())
