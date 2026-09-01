"""Create a genuinely fresh, synthetic test WO (bypassing TCMS/extraction entirely) and run it
through the REAL write() pipeline (Stage A-D), to get an unbiased Stage C data point when JBTC's
real un-invoiced queue has been exhausted of fresh WOs (confirmed 2026-08-30: a full scan of all
273 un-invoiced WOs found zero non-duplicate ones).

Stage C only cares that a real, Variation-Order-confirmed Service Order exists on Schedule Board --
it doesn't care how the quotation was created. Building a WOPayload directly in Python (skipping
PDF/extraction) is safe and representative for THIS specific test, since Stage C's own code has no
dependency on how Stage A/B got there.

Uses a WO-PO number far outside JBTC's real numeric range (9999999xx) so it can never collide with
a real WO and is unambiguously identifiable as a test record in Synergix's own quotation list.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_synthetic_stage_c_test [suffix]

`suffix` (default "01") lets you mint several distinct synthetic WOs in one session, e.g.
`... 02` for a second one, so a clean sample of N attempts doesn't reuse the same WO number.
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

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "01"
# CRITICAL (2026-09-01, caught live by the user): do NOT build test WO numbers as a shared prefix
# plus an incrementing/varying suffix (the old scheme was "WO-PO/99999" + SUFFIX, e.g. 99999m2 vs.
# 99999m22) -- Synergix's own grid filtering does SUBSTRING matching, not exact matching, so a
# filter for "99999m2" also matches "99999m22", "99999m23", etc. This caused real, confirmed
# cross-contamination all session: stray Event Details dialogs and calendar entries from ONE test
# WO kept interfering with a DIFFERENT, later test WO's Stage C run, purely because they shared the
# "99999" prefix. Fixed by deriving a fixed-length numeric block from SUFFIX's characters (NOT
# Python's hash() -- that's randomized per-process by default and gave real collisions across
# separate script invocations, the same mistake already caught and fixed for the job-date offset
# below) instead of concatenating SUFFIX onto a constant prefix -- no two distinct SUFFIX values can
# ever produce a WO number that is a substring of another's, since every one is the same length.
_char_sum = sum(ord(c) * (i + 1) for i, c in enumerate(SUFFIX))  # position-weighted, order-sensitive
_suffix_digits = f"{_char_sum % 90000000 + 10000000:08d}"  # fixed 8 digits, never starts with 0
WO_NUMBER = f"WO-PO/9{_suffix_digits}"
# A unique job date per run, derived from the suffix's characters (NOT Python's hash() -- that's
# randomized per-process by default and gave collisions across separate script invocations).
# Multiple synthetic test WOs sharing the SAME job_date all collide on Schedule Board with
# Synergix's own genuine "SV9104: you can only book one task on the same Timeslot" validation
# once any one of them gets scheduled (confirmed live 2026-08-31, twice, on WO-PO/99999i1 and
# WO-PO/99999i2 after earlier same-dated synthetic WOs). Spread runs across different dates so
# this class of test doesn't self-collide.
#
# CORRECTED (2026-08-31, same day, caught by the user): the first fix to this offset it FORWARD
# from 2026-09-01, landing several test WOs in 2027 -- a real, avoidable mistake. A Work Order
# represents billing for a job ALREADY PERFORMED; every real WO this project has ever seen has a
# job date in the past or, at latest, today. Pushing test dates a year into the future is not just
# cosmetically wrong, it risks masking date-dependent Synergix behavior (e.g. validation rules that
# only apply to past/current dates) that a real WO would never trigger. Offset BACKWARD from
# "yesterday" instead, so every synthetic test WO looks like a real one: recent, distinct, never
# future-dated.
_day_offset = sum(ord(c) for c in SUFFIX) % 300
JOB_DATE = date.today() - timedelta(days=1 + _day_offset)


def build_payload() -> WOPayload:
    return WOPayload(
        wo_po_number=WO_NUMBER,
        town_council="JALAN BESAR TOWN COUNCIL",
        job_sheet_number=f"TEST{SUFFIX}",
        service_location="Blk 52 Chin Swee Road (SYNTHETIC TEST RECORD)",
        nature_of_work="SYNTHETIC TEST -- Stage C clean-sample testing, safe to delete",
        job_date=JOB_DATE,
        prepared_by="test-harness",
        gl_number="431-KK-KKR3P1-160052-0-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        line_items=[
            LineItem(
                description="SYNTHETIC TEST line item -- Stage C clean-sample testing",
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
        source_path=f"synthetic-test-{SUFFIX}",
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
    print(f"=== Synthetic test WO {payload.wo_po_number} ===")

    synergix = SynergixDriver()
    await synergix.start()
    try:
        dedup = await synergix.check_duplicate(payload)
        print(f"{payload.wo_po_number}: dedup check -> {dedup.value}")
        if dedup is not DedupResult.NOT_DUPLICATE:
            print(f"{payload.wo_po_number}: refusing to write -- dedup did not confirm "
                  f"NOT_DUPLICATE (got {dedup.value})")
            return

        result = await synergix.write(payload)
        print(f"\n{payload.wo_po_number}: FINAL RESULT -> {result.status.value}: {result.detail}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
