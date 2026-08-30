"""One-off cleanup: abort the two leftover duplicate, unsubmitted Service Orders
(SV00008879 + SV00008880, both for QUO0006818 / WO-PO/000078720) created by the pre-fix
duplicate-SO bug in a prior session, before continuing Stage C hard-reset testing on a clean WO.

Both were visually confirmed (2026-08-30 screenshot) as:
  - "This Service Order has not been submitted" banner visible
  - Same Quotation No. (QUO0006818), same customer/enquiry text
  - An "Abort" button present in the Order Details header (Schedule Board's own per-SO abort,
    distinct from abort_quotation() which targets the Service Quotation list instead)

This script opens Schedule Board, filters Unscheduled Service Orders by the WO number, and for
each matching row: opens it, re-confirms the "not submitted" banner and Quotation No. before
clicking Abort, and verifies the row is gone afterward. Refuses to touch anything that doesn't
match this exact quotation number or that has already been submitted.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.abort_duplicate_service_orders
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src.synergix_driver import SynergixDriver

WO_BARE = "000078720"
EXPECTED_QUOTATION = "QUO0006818"
EXPECTED_ORDER_NOS = {"SV00008879", "SV00008880"}


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
        await synergix.login()
        page = synergix.page
        assert page is not None

        aborted = []
        for expected_order_no in sorted(EXPECTED_ORDER_NOS):
            await synergix._open_schedule_board()
            header = page.locator("th:visible", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill(WO_BARE)
            await filter_input.press("Enter")
            await page.wait_for_timeout(3000)

            row = page.locator("tr", has_text=expected_order_no).locator("visible=true").first
            if not await row.count():
                print(f"{expected_order_no}: not found in Unscheduled grid -- already gone, skipping")
                continue

            row_text = await row.inner_text()
            if EXPECTED_QUOTATION not in row_text:
                print(f"{expected_order_no}: REFUSING -- row text does not contain "
                      f"{EXPECTED_QUOTATION}: {row_text!r}")
                continue

            await row.locator("td", has_text=expected_order_no).first.click(timeout=10000)
            for _ in range(6):
                await page.wait_for_timeout(1500)
                if await page.get_by_text("has not been submitted", exact=False).count():
                    break
            await synergix._screenshot(f"cleanup_check_{expected_order_no}")

            not_submitted = await page.get_by_text("has not been submitted", exact=False).count()
            if not not_submitted:
                print(f"{expected_order_no}: REFUSING -- 'not submitted' banner not present, "
                      "may already be submitted -- see screenshot for actual page state")
                continue

            header_text = await page.locator("text=/Order Details\\[.*\\]/").first.inner_text()
            if expected_order_no not in header_text:
                print(f"{expected_order_no}: REFUSING -- Order Details header shows {header_text!r}, "
                      f"expected {expected_order_no}")
                continue

            print(f"{expected_order_no}: verified unsubmitted, quotation {EXPECTED_QUOTATION} -- aborting")
            abort_btn = page.get_by_role("link", name="Abort").or_(page.get_by_role("button", name="Abort")).first
            await abort_btn.click(timeout=10000)
            await page.wait_for_timeout(1500)
            yes_btn = page.get_by_role("button", name="Yes")
            if await yes_btn.count() and await yes_btn.first.is_visible():
                await yes_btn.first.click(timeout=10000)
                await page.wait_for_timeout(3000)

            await synergix._open_schedule_board()
            filter_input2 = page.locator("th:visible", has_text="Enquiry/Subject").first.locator(
                "input.ui-column-filter").first
            await filter_input2.click()
            await filter_input2.fill(WO_BARE)
            await filter_input2.press("Enter")
            await page.wait_for_timeout(3000)
            still_there = await page.locator("tr", has_text=expected_order_no).locator("visible=true").count()
            if still_there:
                print(f"{expected_order_no}: still present after Abort+Yes -- NOT confirmed removed")
            else:
                print(f"{expected_order_no}: confirmed removed")
                aborted.append(expected_order_no)

        print(f"\nDone. Aborted: {aborted}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
