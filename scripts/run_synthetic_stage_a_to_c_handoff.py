"""Same as run_stage_a_to_c_handoff.py, but skips the slow TCMS scan and mints a synthetic test
WO instead (per the user's instruction, 2026-08-31: "stop trying to find new WO fresh just make
up one for testing and dont waste anymore time"). Runs the REAL Stage B (create quotation) and
Stage B.5 (submit + confirm Variation Order) against Synergix, then opens Schedule Board filtered
to the resulting Service Order's row and STOPS there, leaving the browser open for the user to
manually drive Stage C.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_synthetic_stage_a_to_c_handoff [suffix]
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src.models import LineItem, WOPayload
from src.synergix_driver import DedupResult, SynergixDriver

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "h2"
WO_NUMBER = f"WO-PO/99999{SUFFIX}"


def build_payload() -> WOPayload:
    return WOPayload(
        wo_po_number=WO_NUMBER,
        town_council="JALAN BESAR TOWN COUNCIL",
        job_sheet_number=f"TEST{SUFFIX}",
        service_location="Blk 52 Chin Swee Road (SYNTHETIC TEST RECORD)",
        nature_of_work="SYNTHETIC TEST -- manual Stage C handoff, safe to delete",
        job_date=date(2026, 8, 31),
        prepared_by="test-harness",
        gl_number="431-KK-KKR3P1-160052-0-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        line_items=[
            LineItem(
                description="SYNTHETIC TEST line item -- manual Stage C handoff",
                quantity=1.0,
                unit_price=30.0,
                discount_percent=10.0,
                discount_amount=3.0,
                net_amount=33.0,
            )
        ],
        discount_percent=10.0,
        discount_amount=3.0,
        net_amount=33.0,
        gst_percent=9.0,
        grand_total=35.97,
        source_path=f"synthetic-handoff-{SUFFIX}",
    )


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    payload = build_payload()
    wo = payload.wo_po_number
    print(f"=== Synthetic test WO {wo} ===")

    synergix = SynergixDriver()
    await synergix.start()
    try:
        dedup = await synergix.check_duplicate(payload)
        print(f"{wo}: dedup check -> {dedup.value}")
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"{wo}: refusing -- dedup did not confirm NOT_DUPLICATE (got {dedup.value})")
            return

        print(f"\n=== Stage B: creating quotation for {wo} ===")
        await synergix.login()
        await synergix._stage_b_create_quotation(payload)
        await synergix._assert_details_filled(payload)

        print(f"=== Stage B.5: submitting + confirming Variation Order for {wo} ===")
        quo_id, vo_confirmed = await synergix._submit_quotation(payload)
        print(f"{wo}: quotation {quo_id}, VO confirmed (inline) = {vo_confirmed}")
        if quo_id and not vo_confirmed:
            vo_confirmed = await synergix._confirm_variation_order(quo_id)
            print(f"{wo}: VO confirmed (fallback) = {vo_confirmed}")
        if not vo_confirmed:
            print(f"{wo}: STOPPING -- Variation Order never confirmed, no Service Order exists yet")
            return

        print(f"\n=== Stage C handoff: opening Schedule Board and filtering to {wo}'s order ===")
        page = synergix.page
        assert page is not None
        await synergix._open_schedule_board()
        wo_bare = wo.replace("WO-PO/", "")
        header = page.locator("th:visible", has_text="Enquiry/Subject").first
        filter_input = header.locator("input.ui-column-filter").first
        await filter_input.click()
        await filter_input.fill(wo_bare)
        await filter_input.press("Enter")
        await page.wait_for_timeout(3000)

        row_count = await page.locator("tr", has_text=wo_bare).locator("visible=true").count()
        print(f"\n{'=' * 70}")
        print(f"READY FOR MANUAL STAGE C: {wo} (quotation {quo_id})")
        print(f"Schedule Board is filtered to this WO -- {row_count} row(s) match.")
        print("The browser will stay open. Drive Stage C manually now.")
        print(f"{'=' * 70}\n")
        print("Press Ctrl+C in this terminal when you're done to close the browser cleanly.")

        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nInterrupted -- closing browser.")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
