"""SQLite state store. All WO state persists here so the system is resumable and auditable.

Approval is asynchronous (the admin may approve hours after the scrape), so the DB — not in-memory
state — is the source of truth. Uses aiosqlite for async access from the bot/orchestrator.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import aiosqlite

from config import settings
from src.models import WOPayload, WOStatus

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    wo_po_number   TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    payload_json   TEXT,                 -- full WOPayload as JSON (null until extracted)
    project_code   TEXT,                 -- resolved project code, if known
    remarks        TEXT,                 -- built Synergix remarks string, if known
    error          TEXT,                 -- last error / reason (for INVALID / FAILED / PARTIAL)
    pdf_path       TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_po_number TEXT,
    event       TEXT NOT NULL,
    detail      TEXT,
    at          TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o)}")


async def init_db() -> None:
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
    logger.info("DB initialised at %s", settings.DB_PATH)


async def _audit(conn: aiosqlite.Connection, wo: str | None, event: str, detail: str = "") -> None:
    await conn.execute(
        "INSERT INTO audit_log (wo_po_number, event, detail, at) VALUES (?, ?, ?, ?)",
        (wo, event, detail, _now()),
    )


async def upsert_scraped(wo_po_number: str, pdf_path: str) -> None:
    """Record a freshly scraped WO (status SCRAPED) before extraction."""
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO work_orders (wo_po_number, status, pdf_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(wo_po_number) DO UPDATE SET
                pdf_path=excluded.pdf_path, status=excluded.status, updated_at=excluded.updated_at
            """,
            (wo_po_number, WOStatus.SCRAPED.value, pdf_path, _now(), _now()),
        )
        await _audit(conn, wo_po_number, "scraped", pdf_path)
        await conn.commit()


async def upsert_payload(
    payload: WOPayload,
    status: WOStatus,
    *,
    project_code: str | None = None,
    remarks: str | None = None,
    error: str | None = None,
) -> None:
    """Store/update the full payload + resolved fields with a given status."""
    payload_json = json.dumps(payload.model_dump(), default=_json_default)
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO work_orders
                (wo_po_number, status, payload_json, project_code, remarks, error, pdf_path,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wo_po_number) DO UPDATE SET
                status=excluded.status, payload_json=excluded.payload_json,
                project_code=excluded.project_code, remarks=excluded.remarks,
                error=excluded.error, pdf_path=excluded.pdf_path, updated_at=excluded.updated_at
            """,
            (
                payload.wo_po_number, status.value, payload_json, project_code, remarks, error,
                payload.pdf_path, _now(), _now(),
            ),
        )
        await _audit(conn, payload.wo_po_number, f"status:{status.value}", error or "")
        await conn.commit()


async def set_status(wo_po_number: str, status: WOStatus, *, error: str | None = None) -> None:
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        await conn.execute(
            "UPDATE work_orders SET status=?, error=?, updated_at=? WHERE wo_po_number=?",
            (status.value, error, _now(), wo_po_number),
        )
        await _audit(conn, wo_po_number, f"status:{status.value}", error or "")
        await conn.commit()


async def get(wo_po_number: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM work_orders WHERE wo_po_number=?", (wo_po_number,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_payload(wo_po_number: str) -> WOPayload | None:
    row = await get(wo_po_number)
    if not row or not row.get("payload_json"):
        return None
    return WOPayload.model_validate(json.loads(row["payload_json"]))


async def list_by_status(status: WOStatus) -> list[dict[str, Any]]:
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM work_orders WHERE status=? ORDER BY updated_at", (status.value,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def status_counts() -> dict[str, int]:
    """For the batch summary."""
    async with aiosqlite.connect(settings.DB_PATH) as conn:
        async with conn.execute(
            "SELECT status, COUNT(*) FROM work_orders GROUP BY status"
        ) as cur:
            return {status: count for status, count in await cur.fetchall()}
