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

logger = logging.getLogger(__name__)


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
    mode = " (DRY_RUN — not submitted)" if settings.DRY_RUN else ""
    msg = f"{icon} {wo_po_number}: {status.value}{mode}"
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
    if settings.DRY_RUN:
        lines.append("\n(DRY_RUN active — no Synergix submissions were executed.)")
    await send_text(bot, "\n".join(lines))
