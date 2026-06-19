"""Console approval gate — a Telegram-free stand-in for local pipeline verification.

Mirrors the parts of TelegramGate that main.py drives (`bot`, `app`, `send_for_approval`,
`shutdown`) so the full ingest -> extract -> validate -> dedup -> queue -> approve -> write loop can
be exercised before Telegram and Synergix are configured. Instead of inline buttons it prints each
WO's approval card to stdout and, in auto-approve mode, runs the (stubbed) Synergix write immediately
so you can see the end-to-end result. No bot token required.

Swap back to TelegramGate (main.py picks it automatically once TELEGRAM_BOT_TOKEN is set) for the
real human-in-the-loop flow.
"""
from __future__ import annotations

import logging

from src import db
from src.models import WOPayload, WOStatus
from src.synergix_driver import SynergixDriver
from src.telegram_gate import _format_message  # reuse the exact approval-card layout
from src.validator import build_remarks, resolve_project_code

logger = logging.getLogger(__name__)


class _NullBot:
    """Stands in for telegram.Bot; notifier calls route here and just log to console."""

    async def send_message(self, *_, text: str = "", **__) -> None:
        print(f"[notify] {text}")

    async def send_document(self, *_, **__) -> None:
        pass


class _NullApp:
    """Stands in for the telegram Application lifecycle used by main()."""

    class _Updater:
        async def start_polling(self) -> None: ...
        async def stop(self) -> None: ...

    def __init__(self) -> None:
        self.updater = _NullApp._Updater()

    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...


class ConsoleGate:
    """Console-only approval gate. `auto_approve=True` immediately runs the (stubbed) write."""

    def __init__(self, *, auto_approve: bool = True) -> None:
        self.app = _NullApp()
        self._bot = _NullBot()
        self._auto_approve = auto_approve
        self._driver: SynergixDriver | None = None
        self._driver_lock = None

    @property
    def bot(self) -> _NullBot:
        return self._bot

    async def _ensure_driver(self) -> SynergixDriver:
        import asyncio

        if self._driver_lock is None:
            self._driver_lock = asyncio.Lock()
        if self._driver is None:
            self._driver = SynergixDriver()
            await self._driver.start()
        return self._driver

    async def send_for_approval(self, payload: WOPayload) -> None:
        project_code = resolve_project_code(payload.job_sheet_number)
        remarks = build_remarks(payload)
        card = _format_message(payload, project_code, remarks)
        print("\n" + "=" * 78)
        print("PENDING APPROVAL")
        print(card)
        print(f"source file: {payload.source_path}")
        print("=" * 78)

        if not self._auto_approve:
            print("(auto-approve off — left PENDING_APPROVAL in DB)")
            return

        await db.set_status(payload.wo_po_number, WOStatus.APPROVED)
        await self._execute_approved(payload.wo_po_number)

    async def _execute_approved(self, wo_po_number: str) -> None:
        from src import notifier

        payload = await db.get_payload(wo_po_number)
        if payload is None:
            await db.set_status(wo_po_number, WOStatus.FAILED, error="payload not found")
            print(f"[FAILED] {wo_po_number}: payload not found in DB")
            return
        driver = await self._ensure_driver()
        async with self._driver_lock:
            result = await driver.write(payload)
        await db.set_status(wo_po_number, result.status, error=result.detail or None)
        await notifier.send_wo_result(self.bot, wo_po_number, result.status, result.detail)

    async def shutdown(self) -> None:
        if self._driver:
            await self._driver.close()
