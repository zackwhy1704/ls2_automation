"""Telegram approval gate. Inline Approve / Edit / Reject buttons are the entire human interface.

For each PENDING_APPROVAL WO we send one message (extracted fields + project code + remarks preview,
PDF attached) with inline buttons. The callback handler updates DB state; on Approve it runs the
Synergix write for that WO and reports the result back into the same thread.

The shared Synergix browser context is guarded by an asyncio.Lock so writes run one at a time.
"""
from __future__ import annotations

import logging
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from config import settings
from src import db, notifier
from src.models import WOPayload, WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
from src.validator import build_remarks, resolve_project_code

logger = logging.getLogger(__name__)

# Callback data format: "<action>:<wo_po_number>"
_APPROVE = "approve"
_REJECT = "reject"
_EDIT = "edit"


def _keyboard(wo_po_number: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"{_APPROVE}:{wo_po_number}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"{_EDIT}:{wo_po_number}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"{_REJECT}:{wo_po_number}"),
            ]
        ]
    )


def _format_message(payload: WOPayload, project_code: str, remarks: str) -> str:
    conf = payload.extraction_confidence
    flag = ""
    if conf is not None and conf < settings.EXTRACTION_CONFIDENCE_THRESHOLD:
        flag = (
            f"\n⚠️ low confidence ({conf:.2f}) — verify all fields against the PDF, "
            "especially gl_number"
        )
    # Pricing line: show the full breakdown when present so the admin can sanity-check.
    price = f"Qty x Rate: {payload.quantity} x {payload.unit_price}"
    if payload.discount_amount:
        price += f"  (−{payload.discount_amount} disc)"
    if payload.net_amount is not None:
        price += f"  → net {payload.net_amount}"
    if payload.grand_total is not None:
        price += f"  | grand total {payload.grand_total}"

    sr = f"\nSR: {payload.sr_number}" if payload.sr_number else ""
    council = f"{payload.town_council} | " if payload.town_council else ""

    return (
        f"🧾 *Work Order for approval*\n"
        f"{council}WO-PO: {payload.wo_po_number}{sr}\n"
        f"Job Sheet: {payload.job_sheet_number}  →  Project code: {project_code}\n"
        f"Location: {payload.service_location}\n"
        f"Nature: {payload.nature_of_work}\n"
        f"Job date: {payload.job_date.strftime('%d/%m/%Y')}\n"
        f"Prepared by: {payload.prepared_by}\n"
        f"GL: {payload.gl_number}\n"
        f"{price}\n"
        f"\nRemarks preview:\n{remarks}"
        f"{flag}"
    )


class TelegramGate:
    """Owns the Telegram Application and the shared Synergix driver."""

    def __init__(self) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
        self.app: Application = (
            Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        )
        self.app.add_handler(CallbackQueryHandler(self._on_callback))
        # Shared Synergix browser + lock are created on demand (first approval).
        self._driver: SynergixDriver | None = None
        self._driver_lock = None  # asyncio.Lock, created lazily inside the running loop

    @property
    def bot(self):
        return self.app.bot

    async def _ensure_driver(self) -> SynergixDriver:
        import asyncio

        if self._driver_lock is None:
            self._driver_lock = asyncio.Lock()
        if self._driver is None:
            self._driver = SynergixDriver()
            await self._driver.start()
        return self._driver

    async def send_for_approval(self, payload: WOPayload) -> None:
        """Send one WO's approval request (fields + remarks + PDF attachment + buttons)."""
        if not settings.TELEGRAM_CHAT_ID:
            logger.warning("TELEGRAM_CHAT_ID not set; cannot send approval for %s", payload.wo_po_number)
            return
        project_code = resolve_project_code(payload.job_sheet_number)
        remarks = build_remarks(payload)
        text = _format_message(payload, project_code, remarks)

        # Send the PDF first (caption carries the fields), then the buttons message.
        try:
            src = payload.source_path
            with open(src, "rb") as fh:
                await self.bot.send_document(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    document=InputFile(fh, filename=Path(src).name),
                    caption=f"WO source: {payload.wo_po_number}",
                )
        except FileNotFoundError:
            logger.warning("WO source file not found for %s at %s; sending without attachment",
                           payload.wo_po_number, payload.source_path)

        await self.bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=_keyboard(payload.wo_po_number),
        )
        logger.info("Sent approval request for %s", payload.wo_po_number)

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()  # stop the button spinner
        action, _, wo_po_number = (query.data or "").partition(":")

        if action == _REJECT:
            await db.set_status(wo_po_number, WOStatus.REJECTED)
            await query.edit_message_text(f"🚫 {wo_po_number}: rejected. Fix at source if needed.")
            return

        if action == _EDIT:
            # MVP: Edit just instructs fixing at source (full inline editing is post-MVP).
            await query.edit_message_text(
                f"✏️ {wo_po_number}: please correct the data in TCMS at source, then re-run the scrape. "
                "(Inline editing is not available in this MVP.)"
            )
            return

        if action == _APPROVE:
            await db.set_status(wo_po_number, WOStatus.APPROVED)
            await query.edit_message_text(f"✅ {wo_po_number}: approved — writing to Synergix…")
            await self._execute_approved(wo_po_number)
            return

        logger.warning("Unknown callback action: %r", action)

    async def _execute_approved(self, wo_po_number: str) -> None:
        payload = await db.get_payload(wo_po_number)
        if payload is None:
            await notifier.send_wo_result(self.bot, wo_po_number, WOStatus.FAILED, "payload not found in DB")
            await db.set_status(wo_po_number, WOStatus.FAILED, error="payload not found")
            return

        driver = await self._ensure_driver()
        async with self._driver_lock:  # serialise browser writes
            # Checkpoint 2 of 2: time passed between queuing and this approval, during which the WO
            # may have been invoiced manually. Re-verify right before writing so a stale approval
            # can't create a duplicate invoice. Only a confirmed NOT_DUPLICATE proceeds.
            dedup = await driver.check_duplicate(payload)
            if not dedup.safe_to_bill:
                if dedup is DedupResult.DUPLICATE:
                    await db.set_status(wo_po_number, WOStatus.DUPLICATE,
                                        error="already invoiced in Synergix at approval time")
                    await notifier.send_wo_result(self.bot, wo_po_number, WOStatus.DUPLICATE,
                                                  "already invoiced in Synergix — write aborted")
                else:  # UNCERTAIN
                    await db.set_status(wo_po_number, WOStatus.NEEDS_REVIEW,
                                        error="dedup inconclusive at approval time — write aborted")
                    await notifier.send_wo_result(self.bot, wo_po_number, WOStatus.NEEDS_REVIEW,
                                                  "could not confirm it is un-invoiced — write aborted, verify manually")
                return
            result = await driver.write(payload)

        await db.set_status(wo_po_number, result.status, error=result.detail or None)
        await notifier.send_wo_result(self.bot, wo_po_number, result.status, result.detail)

    async def shutdown(self) -> None:
        if self._driver:
            await self._driver.close()
