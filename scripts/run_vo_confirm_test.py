"""One-off live verification of SynergixDriver._confirm_variation_order against the remaining
quotations left sitting in "Under Variation" from the earlier 5-WO submit test
(scripts/run_5_stage_b_submit_test.py): QUO0006750-QUO0006753.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_vo_confirm_test
"""
from __future__ import annotations

import asyncio

from config import settings
from src.synergix_driver import SynergixDriver

QUOTATION_IDS = ["QUO0006750", "QUO0006751", "QUO0006752", "QUO0006753"]


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- _confirm_variation_order would no-op. Re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    synergix = SynergixDriver()
    await synergix.start()
    try:
        await synergix.login()
        for quo_id in QUOTATION_IDS:
            print(f"\n=== {quo_id} ===")
            ok = await synergix._confirm_variation_order(quo_id)
            print(f"{quo_id}: confirm {'SUCCEEDED' if ok else 'FAILED'}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
