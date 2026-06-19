"""Telegram notifications: batch summary + per-WO result messages.

Kept separate from the approval gate so it can be reused by a decoupled scrape job in production.
Operates on a shared telegram.Bot instance and the configured admin chat id.
"""
from __future__ import annotations

import logging

from telegram import Bot

from config import settings
from src import db
from src.models import WOStatus
from src.synergix_driver import synergix_configured

logger = logging.getLogger(__name__)


def _mode_suffix() -> str:
    """Annotate result/summary lines with the active run mode (stub vs dry-run vs live)."""
    if not synergix_configured():
        return " (STUB — Synergix not configured, nothing written)"
    if settings.DRY_RUN:
        return " (DRY_RUN — not submitted)"
    return ""


async def send_text(bot: Bot, text: str) -> None:
    if not settings.TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set; skipping message: %s", text)
        return
    await bot.send_message(chat_id=settings.TELEGRAM_CHAT_ID, text=text)


async def send_no_new_wos(bot: Bot) -> None:
    await send_text(bot, "No new un-invoiced Work Orders found today.")


async def send_wo_result(bot: Bot, wo_po_number: str, status: WOStatus, detail: str = "") -> None:
    icon = {
        WOStatus.PROCESSED: "✅",
        WOStatus.PARTIAL: "⚠️",
        WOStatus.FAILED: "❌",
        WOStatus.REJECTED: "🚫",
        WOStatus.DUPLICATE: "♻️",
        WOStatus.INVALID: "⛔",
    }.get(status, "•")
    msg = f"{icon} {wo_po_number}: {status.value}{_mode_suffix()}"
    if detail:
        msg += f"\n{detail}"
    await send_text(bot, msg)


async def send_batch_summary(bot: Bot) -> None:
    """Send a status breakdown across all WOs in the DB."""
    counts = await db.status_counts()
    if not counts:
        await send_text(bot, "Batch complete. No Work Orders recorded.")
        return
    lines = ["📊 Batch summary:"]
    for status, n in sorted(counts.items()):
        lines.append(f"  • {status}: {n}")
    if not synergix_configured():
        lines.append("\n(STUB mode — Synergix not configured; no submissions were executed.)")
    elif settings.DRY_RUN:
        lines.append("\n(DRY_RUN active — no Synergix submissions were executed.)")
    await send_text(bot, "\n".join(lines))
