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

    result = subprocess.run([sys.executable, *child_args])

    if result.returncode != 0:
        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID
        message = (
            "\U0001F6A8 ls2_automation run CRASHED (exit code "
            f"{result.returncode}). Command: {' '.join(child_args)!r}. Check logs on the host."
        )
        if token and chat_id:
            _send_telegram(token, chat_id, message)
        else:
            print("alert_on_crash: TELEGRAM_BOT_TOKEN/CHAT_ID not set, cannot alert:", message, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
