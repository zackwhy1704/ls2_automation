"""Interactive selector-discovery harness for the JBTC TCMS (Dynamics 365) portal.

We have never seen this portal, so config/selectors.py is all placeholders. This script opens a
VISIBLE browser, logs in with the TCMS_* creds from .env, and then lets you dump stable candidate
selectors for whatever page you navigate to — so config/selectors.py gets filled from the real DOM
instead of guesses.

Dynamics 365 specifics this harness leans on:
  * D365 controls carry `data-dyn-controlname` — by far the most STABLE selector on these pages
    (ids are generated and change per session). We surface those first.
  * The login is a Microsoft Entra redirect (login.microsoftonline.com) even without MFA, so the
    login DOM is Microsoft's, not D365's — the harness reports whichever login form is actually shown.
  * The session is persisted to .tcms_session/ so you only log in once; delete that dir to re-auth.

Usage:
    HEADLESS=false python -m scripts.tcms_discover

Then at the "tcms>" prompt:
    dump              # print stable candidate selectors for the current page (inputs/buttons/links)
    dump input        # only <input> elements   (also: button | link | dyn | all)
    grep <text>       # elements whose visible text / label / controlname contains <text>
    url               # print the current page URL + title
    shot [name]       # save a screenshot to logs/ for reference
    html [name]       # dump the current page HTML to logs/ for offline inspection
    login             # (re)run the automated login using .env creds
    help              # show commands
    quit              # close the browser and exit

Nothing here writes to config/selectors.py — you (with me) decide which candidate to use for each
constant after seeing the real DOM.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from config import settings

_SESSION_DIR = settings.PROJECT_ROOT / ".tcms_session"


# ------------------------------------------------------------------ candidate-selector extraction
def _candidates(page: Page, kind: str = "all") -> list[str]:
    """Return human-readable candidate selectors for interactive picking.

    Prefers D365's data-dyn-controlname, then id/name/aria-label/placeholder/role+text. Each line is
    formatted so it can be pasted (or lightly adapted) straight into config/selectors.py.
    """
    js = r"""
    (kind) => {
      const out = [];
      const wanted = (tag) => kind === 'all'
        || (kind === 'input' && tag === 'input')
        || (kind === 'button' && (tag === 'button' || tag === 'a'))
        || (kind === 'link' && tag === 'a')
        || (kind === 'dyn');
      const seen = new Set();
      const push = (sel, label) => { const k = sel+'||'+label; if (sel && !seen.has(k)) { seen.add(k); out.push(sel + '   # ' + label); } };
      const els = document.querySelectorAll('input, button, a, [role="button"], [data-dyn-controlname]');
      for (const el of els) {
        const tag = el.tagName.toLowerCase();
        const dyn = el.getAttribute('data-dyn-controlname');
        if (kind === 'dyn' && !dyn) continue;
        if (kind !== 'dyn' && kind !== 'all' && !wanted(tag)) continue;
        const rect = el.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0;
        const text = (el.innerText || el.value || '').trim().slice(0, 40).replace(/\s+/g, ' ');
        const label = [tag, dyn ? 'dyn=' + dyn : '', el.getAttribute('aria-label') ? 'aria=' + el.getAttribute('aria-label') : '',
                       el.name ? 'name=' + el.name : '', el.type ? 'type=' + el.type : '',
                       text ? 'text="' + text + '"' : '', visible ? '' : '(hidden)'].filter(Boolean).join(' ');
        if (dyn) push(`[data-dyn-controlname="${dyn}"]`, label);
        else if (el.id) push(`#${el.id}`, label);
        else if (el.name) push(`${tag}[name="${el.name}"]`, label);
        else if (el.getAttribute('aria-label')) push(`${tag}[aria-label="${el.getAttribute('aria-label')}"]`, label);
        else if (text) push(`${tag}:has-text("${text}")`, label);
      }
      return out;
    }
    """
    return page.evaluate(js, kind)


def _grep(page: Page, needle: str) -> list[str]:
    lines = _candidates(page, "all")
    n = needle.lower()
    return [ln for ln in lines if n in ln.lower()]


# ------------------------------------------------------------------ login
def _login(page: Page) -> None:
    """Best-effort automated login using .env creds against the Microsoft Entra / D365 login form.

    Uses generic Microsoft-login field names (they are stable across tenants). If the flow differs,
    just log in by hand in the visible window — the persisted session is what matters.
    """
    if not settings.TCMS_BASE_URL:
        print("TCMS_BASE_URL not set in .env — cannot navigate."); return
    print(f"navigating to {settings.TCMS_BASE_URL}")
    page.goto(settings.TCMS_BASE_URL, wait_until="domcontentloaded")

    # Microsoft Entra login: email field -> Next -> password -> Sign in -> "Stay signed in?"
    try:
        if page.locator('input[type="email"]').count() and settings.TCMS_USERNAME:
            page.fill('input[type="email"]', settings.TCMS_USERNAME)
            page.click('input[type="submit"], #idSIButton9')
            page.wait_for_timeout(2500)
        if page.locator('input[type="password"]').count() and settings.TCMS_PASSWORD:
            page.fill('input[type="password"]', settings.TCMS_PASSWORD)
            page.click('input[type="submit"], #idSIButton9')
            page.wait_for_timeout(2500)
            # "Stay signed in?" prompt — click Yes to persist the session.
            if page.locator('#idSIButton9').count():
                page.click('#idSIButton9')
        print("login flow attempted — check the browser window for its current state.")
    except Exception as exc:
        print(f"auto-login step raised ({exc}); finish logging in manually in the window.")


# ------------------------------------------------------------------ REPL
def _repl(page: Page) -> None:
    print(__doc__.split("Usage:")[1])
    while True:
        try:
            raw = input("tcms> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        cmd, _, arg = raw.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()

        if cmd in ("quit", "q", "exit"):
            break
        elif cmd == "help":
            print(__doc__.split("Usage:")[1])
        elif cmd == "url":
            print(f"URL:   {page.url}\nTitle: {page.title()}")
        elif cmd == "login":
            _login(page)
        elif cmd == "dump":
            for ln in _candidates(page, arg or "all"):
                print("  " + ln)
        elif cmd == "grep":
            if not arg:
                print("usage: grep <text>"); continue
            for ln in _grep(page, arg):
                print("  " + ln)
        elif cmd == "shot":
            dest = settings.LOGS_DIR / f"tcms_{arg or 'shot'}.png"
            page.screenshot(path=str(dest), full_page=True)
            print(f"saved {dest}")
        elif cmd == "html":
            dest = settings.LOGS_DIR / f"tcms_{arg or 'page'}.html"
            dest.write_text(page.content(), encoding="utf-8")
            print(f"saved {dest}")
        else:
            print(f"unknown command {cmd!r} — type 'help'")


def main() -> None:
    if settings.HEADLESS:
        print("NOTE: HEADLESS is true; run with `HEADLESS=false` to watch/drive the browser.")
    _SESSION_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        # Persistent context so the login session survives across runs.
        ctx = p.chromium.launch_persistent_context(
            str(_SESSION_DIR), headless=settings.HEADLESS, accept_downloads=True
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        _login(page)
        _repl(page)
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
