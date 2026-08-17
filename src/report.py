"""Batch run report: build a human-readable summary of WO outcomes and email it to the team.

This report IS the human's checklist now that there's no Telegram gate: it lists what was submitted
(with quotation IDs), what was skipped as a duplicate, what failed validation, what needs manual
review (uncertain dedup), and what errored — so the team can spot-check and fix in Synergix.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from html import escape

from config import settings
from src.batch import BatchResult, WOOutcome
from src.models import WOStatus

logger = logging.getLogger(__name__)

# Report sections in priority order: things needing attention first.
_SECTIONS: list[tuple[WOStatus, str]] = [
    (WOStatus.NEEDS_REVIEW, "⚠️ Needs manual review (see reason per item)"),
    (WOStatus.FAILED, "❌ Failed"),
    (WOStatus.INVALID, "⛔ Invalid (bad/missing data)"),
    (WOStatus.DUPLICATE, "♻️ Skipped — already invoiced"),
    (WOStatus.PROCESSED, "✅ Submitted to Synergix"),
    (WOStatus.PARTIAL, "📝 Draft created (DRY_RUN — not submitted)"),
]


def _mode_line() -> str:
    if not settings.SYNERGIX_BASE_URL.strip():
        return "Synergix NOT configured — no writes performed."
    if settings.DRY_RUN:
        return "DRY_RUN active — quotations were drafted and filled but NOT submitted."
    return "LIVE — quotations were created and submitted in Synergix."


def _counts(result: BatchResult) -> str:
    parts = []
    for status, _ in _SECTIONS:
        n = len(result.by_status(status))
        if n:
            parts.append(f"{status.value}={n}")
    return ", ".join(parts) or "no WOs processed"


def _quotation_summary(result: BatchResult) -> tuple[int, int, float | None]:
    """(quotations generated, non-duplicate WOs attempted, success rate %).

    "Generated" counts both PROCESSED (submitted) and PARTIAL (DRY_RUN draft) — either way a real
    quotation now exists in Synergix for that WO. The rate is against non-duplicate WOs only: a
    DUPLICATE is a correct skip, not an attempt, so it shouldn't drag the rate down.
    """
    generated = len(result.by_status(WOStatus.PROCESSED)) + len(result.by_status(WOStatus.PARTIAL))
    attempted = len(result.outcomes) - len(result.by_status(WOStatus.DUPLICATE))
    rate = (generated / attempted * 100) if attempted else None
    return generated, attempted, rate


def _quotation_summary_line(result: BatchResult) -> str:
    generated, attempted, rate = _quotation_summary(result)
    if attempted == 0:
        return "Quotations generated: 0 (no non-duplicate WOs to process)"
    return f"Quotations generated: {generated}/{attempted} non-duplicate WO(s) — {rate:.0f}% success rate"


def build_subject(result: BatchResult) -> str:
    n = len(result.outcomes)
    submitted = len(result.by_status(WOStatus.PROCESSED))
    attention = (len(result.by_status(WOStatus.NEEDS_REVIEW))
                 + len(result.by_status(WOStatus.FAILED))
                 + len(result.by_status(WOStatus.INVALID)))
    flag = f" — {attention} need attention" if attention else ""
    return f"LS2 billing run: {n} WO(s), {submitted} submitted{flag}"


def build_text(result: BatchResult) -> str:
    lines = ["LS2 Adhoc Pest Control — Billing Run Report", "=" * 44, "",
             _mode_line(), f"Totals: {_counts(result)}",
             _quotation_summary_line(result), ""]
    for status, title in _SECTIONS:
        items = result.by_status(status)
        if not items:
            continue
        lines.append(f"{title}  ({len(items)})")
        for o in items:
            quo = f" -> {o.quotation_id}" if o.quotation_id else ""
            detail = f"  [{o.detail}]" if o.detail else ""
            lines.append(f"  - {o.wo_po_number}{quo}{detail}")
        lines.append("")
    lines.append("Review the flagged items in Synergix and correct any mistakes there.")
    return "\n".join(lines)


def build_html(result: BatchResult) -> str:
    rows = [f"<p style='font-family:sans-serif'><b>{escape(_mode_line())}</b><br>"
            f"<span style='color:#555'>Totals: {escape(_counts(result))}</span><br>"
            f"<span style='color:#555'>{escape(_quotation_summary_line(result))}</span></p>"]
    for status, title in _SECTIONS:
        items = result.by_status(status)
        if not items:
            continue
        rows.append(f"<h3 style='font-family:sans-serif;margin:16px 0 6px'>{escape(title)} ({len(items)})</h3>")
        rows.append("<ul style='font-family:sans-serif;margin:0'>")
        for o in items:
            quo = f" &rarr; <b>{escape(o.quotation_id)}</b>" if o.quotation_id else ""
            detail = f" <span style='color:#777'>[{escape(o.detail)}]</span>" if o.detail else ""
            rows.append(f"<li>{escape(o.wo_po_number)}{quo}{detail}</li>")
        rows.append("</ul>")
    rows.append("<p style='font-family:sans-serif;color:#555'>Review the flagged items in Synergix "
                "and correct any mistakes there.</p>")
    return "".join(rows)


async def send_report(result: BatchResult, *, send: bool = True) -> bool:
    """Send the batch report over the configured channel(s). Always logs it first.

    Async because the caller (the batch pipeline) runs inside an event loop. REPORT_CHANNEL:
    "telegram" (default), "email", or "both". Returns True if at least one channel delivered.

    send=False logs the report but skips every network send — used by --batch --no-telegram for a
    console-only dry run (e.g. this verification session), so it doesn't post real batch reports to
    the production Telegram chat while testing.
    """
    import asyncio

    text = build_text(result)
    logger.info("Batch report:\n%s", text)  # always log it
    if not send:
        return False

    channel = settings.REPORT_CHANNEL.strip().lower()
    sent = False
    if channel in ("telegram", "both"):
        sent = await _send_telegram(text) or sent
    if channel in ("email", "both"):
        sent = await asyncio.to_thread(_send_email, result) or sent  # smtplib is blocking
    if channel not in ("telegram", "email", "both"):
        logger.warning("Unknown REPORT_CHANNEL %r — report logged only.", settings.REPORT_CHANNEL)
    return sent


async def _send_telegram(text: str) -> bool:
    """Post the report to the configured Telegram chat (one-way; no bot polling). Never raises."""
    if not (settings.TELEGRAM_BOT_TOKEN.strip() and settings.TELEGRAM_CHAT_ID.strip()):
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — "
                       "report logged only, not sent to Telegram.")
        return False
    # Telegram caps messages at 4096 chars; trim defensively.
    body = text if len(text) <= 3900 else text[:3900] + "\n… (truncated; see logs for full report)"
    try:
        from telegram import Bot

        bot = Bot(settings.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=settings.TELEGRAM_CHAT_ID, text=body)
        logger.info("Batch report sent to Telegram chat %s", settings.TELEGRAM_CHAT_ID)
        return True
    except Exception:
        logger.exception("Failed to send batch report to Telegram")
        return False


def _send_email(result: BatchResult) -> bool:
    """Email the batch report over SMTP. Returns False (logs) if SMTP isn't configured."""
    if not (settings.SMTP_HOST.strip() and settings.REPORT_TO.strip()):
        logger.warning("SMTP not configured (SMTP_HOST / REPORT_TO) — report not emailed.")
        return False

    msg = EmailMessage()
    msg["Subject"] = build_subject(result)
    msg["From"] = settings.REPORT_FROM.strip() or settings.SMTP_USERNAME
    msg["To"] = settings.REPORT_TO
    msg.set_content(build_text(result))
    msg.add_alternative(f"<html><body>{build_html(result)}</body></html>", subtype="html")

    try:
        if settings.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
                s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
                if settings.SMTP_USE_TLS:
                    s.starttls()
                s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        logger.info("Batch report emailed to %s", settings.REPORT_TO)
        return True
    except Exception:
        logger.exception("Failed to email batch report")
        return False
