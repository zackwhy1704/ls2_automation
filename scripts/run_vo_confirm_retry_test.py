"""One-off live check: does the new reload-and-retry logic in _confirm_variation_order help against
a quotation already stuck with a missing Confirm button (QUO0006769, from an earlier run)?

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_vo_confirm_retry_test
"""
from __future__ import annotations

import asyncio

from config import settings
from src.synergix_driver import SynergixDriver

QUOTATION_ID = "QUO0006769"


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
        ok = await synergix._confirm_variation_order(QUOTATION_ID)
        print(f"{QUOTATION_ID}: confirm {'SUCCEEDED' if ok else 'FAILED'}")
    finally:
        await synergix.close()


if __name__ == "__main__":
    asyncio.run(main())
