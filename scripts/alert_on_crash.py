"""Run a pipeline entry point; if the process itself dies (crash, not a per-WO failure), send one
Telegram alert before exiting non-zero.

Per-WO failures already surface via notifier.send_batch_summary — this only covers the case where
the whole run never gets that far (import error, unhandled exception, TCMS/Synergix login failing
outright, etc.), which would otherwise fail silently on an unattended scheduled machine.

Uses raw HTTP (urllib), not python-telegram-bot, so it still works even if the app failed to import.

Usage:
    python -m scripts.alert_on_crash -- -m src.main --batch
    python -m scripts.alert_on_crash -- -m src.main --batch --poll
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
import urllib.parse

# Substrings that, if present in the crash's tail output, get a distinct headline — so a login/MFA
# failure (the scenario flagged in project review: TCMS Conditional Access MFA is assumed, not
# confirmed, and would otherwise die silently once the persisted Entra session cookie expires) is
# visible in the Telegram alert itself, not just buried in a log file on the host.
_LOGIN_FAILURE_MARKERS = ("did not reach the dashboard", "login failed", "TCMS login", "Synergix login")

# How many trailing lines of the child's stderr to include in the alert, so the failure is legible
# without RDPing into the host. Telegram caps messages at 4096 chars; this stays well under that.
_TAIL_LINES = 15


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
    except Exception as exc:  # best-effort: never let the alerter itself crash the wrapper
        print(f"alert_on_crash: failed to send Telegram alert: {exc}", file=sys.stderr)


def main() -> None:
    if "--" not in sys.argv:
        print("usage: python -m scripts.alert_on_crash -- <python args...>", file=sys.stderr)
        sys.exit(2)
    idx = sys.argv.index("--")
    child_args = sys.argv[idx + 1 :]

    from config import settings  # imported late: a broken .env shouldn't block --help etc.

    # Capture stderr so its tail can ride in the Telegram alert, but still stream it live to our own
    # stderr (inherited by the caller's log redirect) so nothing is lost from the on-host log file.
    result = subprocess.run(
        [sys.executable, *child_args], stderr=subprocess.PIPE, text=True,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)

    if result.returncode != 0:
        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID
        tail_lines = (result.stderr or "").strip().splitlines()[-_TAIL_LINES:]
        tail = "\n".join(tail_lines)
        headline = (
            "\U0001F510 ls2_automation LOGIN FAILURE"
            if any(marker in tail for marker in _LOGIN_FAILURE_MARKERS)
            else "\U0001F6A8 ls2_automation run CRASHED"
        )
        message = (
            f"{headline} (exit code {result.returncode}). "
            f"Command: {' '.join(child_args)!r}.\n\n{tail or '(no stderr captured — check logs on host)'}"
        )
        if token and chat_id:
            _send_telegram(token, chat_id, message)
        else:
            print("alert_on_crash: TELEGRAM_BOT_TOKEN/CHAT_ID not set, cannot alert:", message, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
