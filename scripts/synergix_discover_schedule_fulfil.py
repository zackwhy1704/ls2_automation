"""Interactive selector-discovery harness for Synergix Stage C (Schedule Board) and Stage D
(Service Order Performance -> Fulfil), a.k.a. workflow-doc step 28 ("Press Fulfil to submit the
service order for billing").

Neither stage has ever been automated -- config/selectors.py has TODO_SELECTOR placeholders for
both, and the original work order explicitly marked them OUT OF SCOPE. Before writing any driver
code, this script gets us looking at the real DOM together, the same way scripts/tcms_discover.py
did for the (also-never-seen) TCMS portal.

Read-only by design: reuses SynergixDriver.start()/login() for the browser + session, but never
clicks, fills, or submits anything itself -- it only navigates (when you tell it to) and dumps
candidate selectors. Any real click/fill during the session is you, driving manually, with the
inspector available to interrogate whatever's on screen.

Usage:
    HEADLESS=false python -m scripts.synergix_discover_schedule_fulfil

Then at the "synergix>" prompt:
    dump              # print stable candidate selectors for the current page (inputs/buttons/links)
    dump input        # only <input> elements   (also: button | link | all)
    grep <text>       # elements whose visible text / label / placeholder contains <text>
    url               # print the current page URL + title
    shot [name]        # save a screenshot to logs/ for reference
    html [name]        # dump the current page HTML to logs/ for offline inspection
    click <text>      # click the first visible element whose text contains <text>
    fill <label> | <value>   # fill the input near a label/placeholder containing <label>
    enter             # press Enter on whatever's currently focused
    scroll <text>     # scroll the element containing <text> into view
    wait <ms>         # wait the given number of milliseconds
    help              # show commands
    quit              # close the browser and exit

Navigation itself (clicking into Schedule Board, opening an event, opening Service Order
Performance, etc.) is done BY HAND in the visible browser window -- this script does not know the
nav path yet, which is exactly what we're here to find out.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import Page

from config import settings
from src.synergix_driver import SynergixDriver


def _candidates(page: Page, kind: str = "all") -> "asyncio.Future[list[str]]":
    js = r"""
    (kind) => {
      const out = [];
      const wanted = (tag) => kind === 'all'
        || (kind === 'input' && (tag === 'input' || tag === 'textarea'))
        || (kind === 'button' && (tag === 'button' || tag === 'a'))
        || (kind === 'link' && tag === 'a');
      const seen = new Set();
      const push = (sel, label) => { const k = sel+'||'+label; if (sel && !seen.has(k)) { seen.add(k); out.push(sel + '   # ' + label); } };
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
      };
      const labelFor = (el) => {
        if (el.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lab) return lab.innerText.trim();
        }
        const row = el.closest('tr');
        if (row) { const cell = row.querySelector('td, th'); if (cell) return cell.innerText.trim().slice(0, 60); }
        return '';
      };
      document.querySelectorAll('input, textarea, button, a, [role="button"], [role="combobox"], [role="application"], td').forEach(el => {
        const tag = el.tagName.toLowerCase();
        if (!wanted(tag)) return;
        if (!visible(el)) return;
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 60);
        const label = labelFor(el);
        const parts = [];
        if (el.id) parts.push(`[id="${el.id}"]`);
        const desc = [tag, label && `label=${JSON.stringify(label)}`, text && `text=${JSON.stringify(text)}`, el.className && `class=${JSON.stringify(el.className.toString().slice(0,80))}`]
          .filter(Boolean).join(' ');
        if (parts.length) push(parts[0], desc);
        else if (text) push(`text="${text}"`, desc);
      });
      return out;
    }
    """
    return page.evaluate(js, kind)


async def main() -> None:
    driver = SynergixDriver()
    if driver.stubbed:
        print("SYNERGIX_* not configured in .env -- nothing to discover against. Aborting.")
        return
    await driver.start()
    await driver.login()
    page = driver.page
    assert page is not None
    print(
        "\nLogged in. Drive the browser BY HAND to Schedule Board / Service Order Performance.\n"
        "Use this prompt to inspect whatever's currently on screen. Type 'help' for commands.\n"
    )
    while True:
        try:
            line = (await asyncio.to_thread(input, "synergix> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        cmd, *rest = line.split(maxsplit=1)
        arg = rest[0] if rest else ""
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(__doc__)
            elif cmd == "home":
                await page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                print("back at home")
            elif cmd == "url":
                print(f"{page.url}\n{await page.title()}")
            elif cmd == "dump":
                kind = arg or "all"
                for line_ in await _candidates(page, kind):
                    print(line_)
            elif cmd == "grep":
                if not arg:
                    print("usage: grep <text>")
                    continue
                needle = arg.lower()
                for line_ in await _candidates(page, "all"):
                    if needle in line_.lower():
                        print(line_)
            elif cmd == "shot":
                name = arg or "synergix_discover"
                path = settings.PROJECT_ROOT / "logs" / f"{name}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(path), full_page=False)
                print(f"saved {path}")
            elif cmd == "html":
                name = arg or "synergix_discover"
                path = settings.PROJECT_ROOT / "logs" / f"{name}.html"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(await page.content(), encoding="utf-8")
                print(f"saved {path}")
            elif cmd == "fillid":
                if "|" not in arg:
                    print("usage: fillid <id substring> | <value>")
                    continue
                id_sub, value = (p.strip() for p in arg.split("|", 1))
                loc = page.locator(f'input[id*="{id_sub}"], textarea[id*="{id_sub}"]').first
                await loc.click()
                await loc.fill(value)
                print(f"filled id~={id_sub!r} = {value!r}")
            elif cmd == "fill":
                if "|" not in arg:
                    print("usage: fill <label> | <value>")
                    continue
                label, value = (p.strip() for p in arg.split("|", 1))
                # Same relational lookup as SynergixDriver._fill_labeled_input, extended to also
                # match the div-based "synfaces-grid-label" layout (confirmed live elsewhere in this
                # app, e.g. External Remarks) alongside the classic <tr>-based one. Exact label match
                # (not substring) so "Customer" doesn't also match "Customer Type" or a grid column
                # header named "Customer" -- confirmed live these coexist on the Schedule Board page.
                js = """(label) => {
                        const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                        const want = norm(label).toLowerCase();
                        const host = [...document.querySelectorAll('td,div,span,label')]
                          .find(e => e.children.length === 0
                                  && norm(e.textContent).toLowerCase() === want
                                  && !e.closest('th')            // exclude grid column headers
                                  && !e.closest('.ui-datatable')); // exclude grid body/filter rows entirely
                        if (!host) return null;
                        const scope = host.closest('.synfaces-grid-item') || host.closest('tr') || host.parentElement;
                        const input = scope && scope.querySelector('input:not([type=hidden]):not([readonly]), textarea');
                        return input ? input.id : null;
                    }"""
                input_id = await page.evaluate(js, label)
                if not input_id:
                    print(f"could not locate an input for label containing {label!r}")
                    continue
                field = page.locator(f'[id="{input_id}"]')
                await field.click()
                await field.fill(value)
                print(f"filled {label!r} = {value!r} (id={input_id})")
            elif cmd == "scroll":
                if not arg:
                    print("usage: scroll <text>")
                    continue
                loc = page.locator(f"text={arg}").locator("visible=true").first
                await loc.scroll_into_view_if_needed(timeout=8000)
                print(f"scrolled {arg!r} into view")
            elif cmd == "enter":
                await page.keyboard.press("Enter")
                print("pressed Enter")
            elif cmd == "escape":
                await page.keyboard.press("Escape")
                print("pressed Escape")
            elif cmd == "wait":
                await page.wait_for_timeout(int(arg or "2000"))
                print("waited")
            elif cmd == "viewport":
                w, h = (int(x) for x in (arg or "1920x1080").split("x"))
                await page.set_viewport_size({"width": w, "height": h})
                print(f"viewport set to {w}x{h}")
            elif cmd == "fillcss":
                if "|" not in arg:
                    print("usage: fillcss <css selector> | <value>")
                    continue
                sel, value = (p.strip() for p in arg.split("|", 1))
                loc = page.locator(sel).first
                await loc.click()
                await loc.fill(value)
                print(f"filled css={sel!r} = {value!r}")
            elif cmd == "js":
                if not arg:
                    print("usage: js <expression, e.g. document.title>")
                    continue
                result = await page.evaluate(f"() => {{ return {arg}; }}")
                print(f"js result: {result!r}")
            elif cmd == "css":
                if not arg:
                    print("usage: css <css selector>")
                    continue
                loc = page.locator(arg).first
                await loc.click(timeout=8000)
                print(f"clicked css={arg!r}")
            elif cmd == "clickid":
                if not arg:
                    print("usage: clickid <id substring> [force]")
                    continue
                parts = arg.rsplit(maxsplit=1)
                force = len(parts) == 2 and parts[1] == "force"
                id_sub = parts[0] if force else arg
                loc = page.locator(f'[id*="{id_sub}"]').first
                await loc.click(timeout=8000, force=force)
                print(f"clicked [id*={id_sub!r}]" + (" (forced)" if force else ""))
            elif cmd == "title":
                if not arg:
                    print("usage: title <title attribute value>")
                    continue
                loc = page.locator(f'[title="{arg}"]').first
                await loc.click(timeout=8000)
                print(f"clicked [title={arg!r}]")
            elif cmd == "click":
                if not arg:
                    print("usage: click <text>")
                    continue
                # text= can match a hidden element elsewhere on the page (e.g. a hidden nav link)
                # before the visible one we actually want -- filter to visible matches first.
                loc = page.locator(f"text={arg}").locator("visible=true").first
                await loc.wait_for(state="visible", timeout=8000)
                await loc.click()
                print(f"clicked {arg!r}")
            else:
                print(f"unknown command: {cmd!r} (try 'help')")
        except Exception as exc:
            print(f"error: {exc}")

    await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
