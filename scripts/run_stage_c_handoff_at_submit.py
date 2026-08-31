"""Drive Stage C using the REAL driver logic (same helpers as _schedule_stage_c_attempt) up through
the Event Details Confirm click, then STOP and leave the browser open -- at exactly the point
where the automation finds Submit disabled and doesn't know what real action enables it. The user
will take over manually from here and identify which numbered step of Stage C this corresponds to.

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_stage_c_handoff_at_submit <wo_po_number_bare>

Example:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.run_stage_c_handoff_at_submit 99999x1
"""
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src.synergix_driver import SynergixDriver


async def mouse_click_button(page, label):
    rect = await page.evaluate(
        """(label) => {
            const btn = [...document.querySelectorAll('div.ui-button')]
              .find(b => b.textContent.trim() === label && b.getBoundingClientRect().width > 0);
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }""",
        label,
    )
    if not rect:
        return False
    await page.mouse.click(rect["x"], rect["y"])
    return True


async def wait_spinner(page, label, timeout_s=30):
    spinner = page.locator("img.js-ajax-spinner").first
    for _ in range(15):
        if await spinner.is_visible():
            break
        await page.wait_for_timeout(200)
    else:
        return
    for _ in range(int(timeout_s / 0.2)):
        if not await spinner.is_visible():
            print(f"  [{label}] spinner cleared")
            return
        await page.wait_for_timeout(200)
    print(f"  [{label}] spinner STILL visible after {timeout_s}s")


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.run_stage_c_handoff_at_submit <wo_po_number_bare>")
        return
    wo_bare = sys.argv[1]

    d = SynergixDriver()
    await d.start()
    page = d.page
    try:
        print("=== STEP: logging in ===")
        await d.login()
        print("=== STEP: opening Schedule Board ===")
        await d._open_schedule_board()
        print("=== STEP: Schedule Board opened, filtering grid ===")

        header = page.locator('th:visible', has_text='Enquiry/Subject').first
        filter_input = header.locator('input.ui-column-filter').first
        await filter_input.click(timeout=15000)
        print("=== STEP: filter input clicked ===")
        await filter_input.fill(wo_bare, timeout=15000)
        print("=== STEP: filter input filled ===")
        await filter_input.press('Enter', timeout=15000)
        print("=== STEP: Enter pressed, waiting for grid to narrow ===")
        await page.wait_for_timeout(3000)
        row = page.locator('tr', has_text=wo_bare).locator('visible=true').first
        if not await row.count():
            print(f"No row found for {wo_bare}")
            return
        print(f"=== STEP: row selected for {wo_bare} ===")
        await row.click(timeout=10000)
        await wait_spinner(page, "row select")
        await page.wait_for_timeout(500)

        print("=== STEP: clicking Employee toggle ===")
        await mouse_click_button(page, "Employee")
        await wait_spinner(page, "Employee toggle")

        print("=== STEP: waiting for calendar newEventButton ===")
        new_event_btn_id = None
        for _ in range(20):
            new_event_btn_id = await page.evaluate("""() => {
                const btn = document.querySelector('[id*="newEventButton"]');
                return btn ? btn.id : null;
            }""")
            if new_event_btn_id:
                break
            await page.wait_for_timeout(500)
        if not new_event_btn_id:
            print("No newEventButton found -- stopping here for manual inspection")
            print("Browser will stay open. Press Ctrl+C when done.")
            while True:
                await asyncio.sleep(3600)
        btn = page.locator(f'[id="{new_event_btn_id}"]')
        await d._click_when_clear(btn, timeout_ms=15000)
        await page.wait_for_timeout(3000)

        event_dialog = page.locator('[role="dialog"]:has-text("Event Details")').locator("visible=true").first
        if not await event_dialog.count():
            print("Event Details dialog did not open -- stopping here for manual inspection")
            print("Browser will stay open. Press Ctrl+C when done.")
            while True:
                await asyncio.sleep(3600)
        print("=== STEP: Event Details popup open ===")

        pair_checkbox_id = await event_dialog.evaluate(
            """(dialog) => {
                const input = [...dialog.querySelectorAll('input[type=checkbox]')]
                  .find(i => (i.value || '').includes('oa_code=INFIGO'));
                return input ? input.id : null;
            }"""
        )
        if pair_checkbox_id:
            print("=== STEP: ticking INFIGO in To Pair With ===")
            pair_checkbox_box = page.locator(f'input[id="{pair_checkbox_id}"]').locator(
                "xpath=ancestor::div[contains(@class,'ui-chkbox')][1]//div[contains(@class,'ui-chkbox-box')]"
            )
            await d._click_when_clear(pair_checkbox_box, timeout_ms=10000)
            await wait_spinner(page, "To Pair With tick")

        from datetime import date, timedelta
        # A real WO always dates the job in the past (billing for work already performed) -- a
        # hardcoded FUTURE date here was a real mistake (caught 2026-08-31), see the same fix and
        # its rationale in run_synthetic_stage_c_test.py.
        job_date_str = (date.today() - timedelta(days=3)).strftime("%d/%m/%Y")
        print(f"=== STEP: setting From/To dates to {job_date_str} ===")
        await d._fill_labeled_input("From", job_date_str)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(1000)
        await d._fill_labeled_input("To", job_date_str)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Close")').first.click(timeout=5000)
        await page.wait_for_timeout(1000)

        print("=== STEP: clicking Event Details Confirm (checkmark) ===")
        confirm_btn = event_dialog.locator('button:has(span.fa-check)').first
        await confirm_btn.click(timeout=10000)
        await wait_spinner(page, "Event Details confirm", timeout_s=30)

        try:
            await event_dialog.wait_for(state="hidden", timeout=20000)
            print("Event Details dialog closed")
        except Exception:
            print("Event Details dialog did NOT close in time")

        await page.wait_for_timeout(2000)
        submit_disabled = await page.evaluate("""() => {
            const btn = document.querySelector('[id*="submitButton"]');
            return btn ? btn.disabled : null;
        }""")
        print(f"\n{'=' * 70}")
        print(f"STOPPED HERE: Submit button disabled = {submit_disabled}")
        print("This is the exact point the automation gets stuck.")
        print("Please drive the rest of Stage C manually now and tell me which numbered")
        print("step (19, 20, 21, etc. per your own reference) this corresponds to, and")
        print("what action you take to get Submit to actually work.")
        print(f"{'=' * 70}\n")
        print("Browser will stay open. Press Ctrl+C in this terminal when done.")

        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nInterrupted -- closing browser.")
    finally:
        await d.close()


if __name__ == "__main__":
    asyncio.run(main())
