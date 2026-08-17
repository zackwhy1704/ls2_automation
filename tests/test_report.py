"""send_report's send=False path — used by --batch --no-telegram to test against real TCMS/Synergix
without posting to the production Telegram chat.

Regression coverage for a real bug: --batch --no-telegram previously parsed the flag but never
passed it anywhere in the --batch path (only the legacy non-batch flow read it), so a "silent" test
run posted a real batch report to the live ops Telegram chat anyway.
"""
import asyncio

from src import report
from src.batch import BatchResult, WOOutcome
from src.models import WOStatus


def _result() -> BatchResult:
    return BatchResult(outcomes=[WOOutcome("WO-PO/000000001", WOStatus.DUPLICATE, "already invoiced")])


def test_send_false_skips_every_channel(monkeypatch):
    called = []
    monkeypatch.setattr(report, "_send_telegram", lambda text: called.append("telegram") or True)
    monkeypatch.setattr(report, "_send_email", lambda result: called.append("email") or True)

    sent = asyncio.run(report.send_report(_result(), send=False))

    assert sent is False
    assert called == []


def test_send_true_still_delivers(monkeypatch):
    monkeypatch.setattr(report.settings, "REPORT_CHANNEL", "telegram")

    async def fake_send_telegram(text):
        return True

    monkeypatch.setattr(report, "_send_telegram", fake_send_telegram)

    sent = asyncio.run(report.send_report(_result(), send=True))

    assert sent is True


def test_send_defaults_to_true(monkeypatch):
    monkeypatch.setattr(report.settings, "REPORT_CHANNEL", "telegram")

    async def fake_send_telegram(text):
        return True

    monkeypatch.setattr(report, "_send_telegram", fake_send_telegram)

    sent = asyncio.run(report.send_report(_result()))

    assert sent is True
