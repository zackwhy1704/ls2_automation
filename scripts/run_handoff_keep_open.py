"""Run the REAL, current Stage A-D pipeline (write()) on a fresh synthetic WO, using today's actual
driver code (all of today's fixes included), but keep the browser open indefinitely at the end --
whether Stage C succeeds, fails, or gets stuck -- so the user can inspect the live page directly.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_handoff_keep_open <suffix>
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

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "hoff1"
# Fixed-length numeric WO number derived from SUFFIX (2026-09-01) -- see the detailed comment in
# run_synthetic_stage_c_test.py for why a shared "99999" prefix + varying suffix is unsafe: Synergix's
# grid filter does substring matching, so "99999m2" also matches "99999m22" etc., causing real,
# confirmed cross-contamination between supposedly-separate test WOs (stray Event Details dialogs,
# calendar entries) within the same session.
_char_sum = sum(ord(c) * (i + 1) for i, c in enumerate(SUFFIX))
_suffix_digits = f"{_char_sum % 90000000 + 10000000:08d}"
WO_NUMBER = f"WO-PO/9{_suffix_digits}"
# Widened % 300 -> % 3650 (2026-09-01), see the detailed comment in run_synthetic_stage_c_test.py:
# a narrow date range + every test WO sharing the same hardcoded ASSIGNED_WORK_TEAM ("800SUPER")
# caused real, confirmed date collisions across a single day's testing (the same team can't
# logically be booked twice on the same date, which Synergix correctly refuses to render an "add
# event" cell for). Reuses the better-distributed _char_sum above instead of a separate hash.
_day_offset = _char_sum % 3650
JOB_DATE = date.today() - timedelta(days=1 + _day_offset)


def build_payload() -> WOPayload:
    return WOPayload(
        wo_po_number=WO_NUMBER,
        town_council="JALAN BESAR TOWN COUNCIL",
        job_sheet_number=f"TEST{SUFFIX}",
        service_location="Blk 52 Chin Swee Road (SYNTHETIC TEST RECORD)",
        nature_of_work="SYNTHETIC TEST -- live handoff for manual inspection, safe to delete",
        job_date=JOB_DATE,
        prepared_by="test-harness",
        gl_number="431-KY-KYR5P1-160052-0-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        line_items=[
            LineItem(
                description="SYNTHETIC TEST line item -- live handoff, safe to delete",
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
        source_path=f"handoff-{SUFFIX}",
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
    print(f"=== Live handoff run for {payload.wo_po_number} (job_date={JOB_DATE}) ===")

    synergix = SynergixDriver()
    await synergix.start()
    try:
        dedup = await synergix.check_duplicate(payload)
        print(f"{payload.wo_po_number}: dedup check -> {dedup.value}")
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"{payload.wo_po_number}: refusing to write -- dedup did not confirm NOT_DUPLICATE")
            return

        result = await synergix.write(payload)
        print(f"\n{payload.wo_po_number}: FINAL RESULT -> {result.status.value}: {result.detail}")
    except Exception as exc:
        print(f"\n{payload.wo_po_number}: RAISED -> {exc!r}")
    finally:
        print(f"\n{'=' * 70}")
        print("Browser staying open indefinitely for manual inspection.")
        print("Press Ctrl+C in this terminal when done -- that will close the browser.")
        print(f"{'=' * 70}\n")
        try:
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            print("\nInterrupted -- closing browser.")
            await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
