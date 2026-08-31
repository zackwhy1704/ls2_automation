"""P1 differential diagnosis (work order 2026-08-31): capture a full state bundle -- DOM,
checklist-panel HTML, active toggle, network requests/responses -- at the exact moment Stage C's
employee checklist is found empty, so the actual difference between a populated and empty
checklist can be diffed from real evidence instead of theorized.

Network capture is the highest-value signal per the work order: if the PrimeFaces ajax call that
should populate the checklist never fires, fires with different params, or returns an
empty/error partial-response, that IS the answer.

Two modes:
  bot   -- drives the browser itself (Employee -> Filter, same sequence Stage C uses) and captures
           the bundle whether the checklist ends up populated or empty.
  human -- launches a headed browser and waits for YOU to manually click through to the same
           point (row selected, Employee filter open) with a populated checklist, then captures
           the identical bundle on your signal (press Enter in the terminal).

Usage:
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.stage_c_differential bot <wo_po_number>
    DRY_RUN=false SYNERGIX_HEADLESS=false python -m scripts.stage_c_differential human <wo_po_number>

Bundles are written to logs/stage_c_differential_<mode>_<timestamp-free-label>.json plus a
screenshot with the same base name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

from config import settings
from src.synergix_driver import SCHEDULE_EMPLOYEE, SynergixDriver

MODE = sys.argv[1] if len(sys.argv) > 1 else "bot"
WO = sys.argv[2] if len(sys.argv) > 2 else None


async def capture_bundle(driver: SynergixDriver, requests_log: list, label: str) -> dict:
    page = driver.page
    assert page is not None

    checklist_state = await page.evaluate(
        """() => {
            const panel = document.querySelector('[data-widget="panel-filter"]');
            const content = panel ? panel.querySelector('.ui-panel-content') : null;
            const employeeBtn = [...document.querySelectorAll('div.ui-button')]
              .find(b => b.textContent.trim() === 'Employee' && b.getBoundingClientRect().width > 0);
            const workTeamBtn = [...document.querySelectorAll('div.ui-button')]
              .find(b => b.textContent.trim() === 'Work Team' && b.getBoundingClientRect().width > 0);
            const filterInputs = [...document.querySelectorAll('[data-widget="panel-filter"] input[type=text], [data-widget="panel-filter"] input.ui-inputfield')]
              .map(i => ({id: i.id, value: i.value}));
            return {
                panel_visible: panel ? panel.offsetParent !== null : null,
                content_display: content ? content.style.display : null,
                content_computed_display: content ? getComputedStyle(content).display : null,
                content_html_length: content ? content.innerHTML.length : null,
                has_checkbox_html: content ? content.innerHTML.includes('ui-chkbox') : null,
                has_clear_all_html: content ? content.innerHTML.includes('Clear All') : null,
                employee_active: employeeBtn ? employeeBtn.classList.contains('ui-state-active') : null,
                work_team_active: workTeamBtn ? workTeamBtn.classList.contains('ui-state-active') : null,
                filter_inputs: filterInputs,
                viewport: {width: window.innerWidth, height: window.innerHeight},
                user_agent: navigator.userAgent,
            }
        }"""
    )
    checklist_panel_html = await page.evaluate(
        """() => {
            const panel = document.querySelector('[data-widget="panel-filter"] .ui-panel-content');
            return panel ? panel.innerHTML : null;
        }"""
    )

    bundle = {
        "label": label,
        "mode": MODE,
        "wo": WO,
        "checklist_state": checklist_state,
        "checklist_panel_html": checklist_panel_html,
        "network_requests": requests_log,
        "headless": settings.SYNERGIX_HEADLESS,
    }
    out_path = f"logs/stage_c_differential_{MODE}_{label}.json"
    with open(out_path, "w") as f:
        json.dump(bundle, f, indent=2, default=str)
    print(f"Bundle written to {out_path}")
    await driver._screenshot(f"differential_{MODE}_{label}")
    return bundle


async def run_bot_mode(driver: SynergixDriver, wo: str, requests_log: list) -> None:
    page = driver.page
    assert page is not None

    await driver._open_schedule_board()
    wo_bare = wo.replace("WO-PO/", "")
    header = page.locator("th:visible", has_text="Enquiry/Subject").first
    filter_input = header.locator("input.ui-column-filter").first
    await filter_input.click()
    await filter_input.fill(wo_bare)
    await filter_input.press("Enter")
    await page.wait_for_timeout(3000)

    order_row = page.locator("tr", has_text=wo_bare).locator("visible=true").first
    if not await order_row.count():
        print(f"ERROR: no row found for {wo} -- cannot proceed")
        return
    await order_row.click(timeout=10000)
    await page.wait_for_timeout(2000)
    await capture_bundle(driver, list(requests_log), "after_row_select")

    async def click_visible_text(text: str) -> bool:
        loc = page.locator(f"text={text}").locator("visible=true").first
        try:
            await loc.wait_for(state="visible", timeout=8000)
        except Exception:
            return False
        await loc.click()
        return True

    await click_visible_text("Employee")
    await page.wait_for_timeout(2000)
    await capture_bundle(driver, list(requests_log), "after_employee_click")

    await click_visible_text("Filter")
    await page.wait_for_timeout(5000)
    await capture_bundle(driver, list(requests_log), "after_filter_click")

    checkbox_id = await page.evaluate(
        """(name) => {
            const label = [...document.querySelectorAll('label')]
              .find(l => l.textContent.trim() === name);
            return label ? label.getAttribute('for') : null;
        }""",
        SCHEDULE_EMPLOYEE,
    )
    print(f"Checkbox for {SCHEDULE_EMPLOYEE!r} found: {checkbox_id}")


async def run_human_mode(driver: SynergixDriver, wo: str, requests_log: list) -> None:
    import os

    signal_path = "/tmp/stage_c_checklist_ready"
    try:
        os.remove(signal_path)
    except FileNotFoundError:
        pass

    print("\n=== HUMAN MODE ===")
    print(f"Browser is open. Manually navigate to Schedule Board, filter/find {wo}'s row, select "
          "it, click Employee, click Filter -- until you see a POPULATED employee checklist "
          f"(including {SCHEDULE_EMPLOYEE!r}).")
    print(f"When the checklist is visibly populated, run in a separate terminal:\n"
          f"    touch {signal_path}\n"
          f"This script polls for that file (checking every 2s, up to 10 minutes) and captures "
          "the bundle the moment it appears.")
    waited_s = 0
    while not os.path.exists(signal_path):
        await asyncio.sleep(2)
        waited_s += 2
        if waited_s >= 600:
            print("Timed out after 10 minutes waiting for the signal file -- exiting without capturing.")
            return
    os.remove(signal_path)
    await capture_bundle(driver, list(requests_log), "human_populated")
    print("Captured. You can keep the browser open or close it now.")


async def main() -> None:
    if settings.DRY_RUN:
        print("DRY_RUN is still true -- re-run with DRY_RUN=false.")
        return
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return
    if not WO:
        print("Usage: python -m scripts.stage_c_differential <bot|human> <wo_po_number>")
        return

    requests_log: list[dict] = []

    driver = SynergixDriver()
    await driver.start()
    page = driver.page
    assert page is not None

    def on_request(request):
        if "javax.faces" in request.url or "index.xhtml" in request.url:
            try:
                requests_log.append({
                    "event": "request",
                    "url": request.url,
                    "method": request.method,
                    "post_data": request.post_data,
                })
            except Exception:
                pass

    def on_response(response):
        async def _capture():
            try:
                if "javax.faces" in response.url or "index.xhtml" in response.url:
                    body = await response.text()
                    requests_log.append({
                        "event": "response",
                        "url": response.url,
                        "status": response.status,
                        "body_snippet": body[:2000],
                        "body_length": len(body),
                        "contains_chkbox": "ui-chkbox" in body,
                        "contains_employee_name": SCHEDULE_EMPLOYEE in body,
                    })
            except Exception:
                pass
        asyncio.create_task(_capture())

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await driver.login()
        if MODE == "bot":
            await run_bot_mode(driver, WO, requests_log)
        elif MODE == "human":
            await run_human_mode(driver, WO, requests_log)
        else:
            print(f"Unknown mode {MODE!r} -- use 'bot' or 'human'")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
