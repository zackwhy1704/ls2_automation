"""IMAP mail poller — the automated front of the email funnel.

The client auto-forwards Work Order emails to a dedicated mailbox WE control. This module connects to
that mailbox over IMAP, fetches new messages (optionally filtered by sender/subject), and writes each
one as a .eml file into data/incoming_emails/. From there the existing email_ingestor + extractor
pipeline takes over unchanged — a forwarded email is just an .eml on disk.

Idempotency: every downloaded message's Message-ID is recorded in a small JSON ledger so the same
email is never ingested twice, regardless of the server's \\Seen flag handling. Optionally the message
is also marked \\Seen on the server (IMAP_MARK_SEEN) so it drops out of future UNSEEN searches.

This module is intentionally synchronous (imaplib is blocking); main.py runs it via asyncio.to_thread.
"""
from __future__ import annotations

import email
import imaplib
import json
import logging
import re
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# Ledger of already-ingested Message-IDs, so we never re-process a forwarded email.
_LEDGER_PATH = settings.INCOMING_EMAIL_DIR / ".ingested_ids.json"


def _load_ledger() -> set[str]:
    try:
        return set(json.loads(_LEDGER_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return set()


def _save_ledger(ids: set[str]) -> None:
    _LEDGER_PATH.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def _safe_name(message_id: str, subject: str) -> str:
    """Filesystem-safe .eml stem derived from the Message-ID (stable) + a subject hint."""
    mid = re.sub(r"[^A-Za-z0-9]+", "", message_id)[:40] or "msg"
    subj = re.sub(r"[^A-Za-z0-9._-]+", "_", subject).strip("_")[:60]
    return f"{mid}_{subj}" if subj else mid


def _build_search_criteria() -> list[str]:
    """IMAP SEARCH criteria. UNSEEN by default, narrowed by optional FROM/SUBJECT filters."""
    criteria: list[str] = ["UNSEEN"]
    if settings.IMAP_FROM_FILTER.strip():
        criteria += ["FROM", settings.IMAP_FROM_FILTER.strip()]
    if settings.IMAP_SUBJECT_FILTER.strip():
        criteria += ["SUBJECT", settings.IMAP_SUBJECT_FILTER.strip()]
    return criteria


def imap_configured() -> bool:
    """True once the dedicated mailbox is configured (host + credentials present)."""
    return bool(
        settings.IMAP_HOST.strip()
        and settings.IMAP_USERNAME.strip()
        and settings.IMAP_PASSWORD.strip()
    )


def _message_id(msg: Message, fallback: str) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    return mid or f"<no-id-{fallback}>"


def poll_once() -> list[str]:
    """Fetch new WO emails from the IMAP mailbox into data/incoming_emails/. Returns saved .eml paths.

    Idempotent: messages whose Message-ID is already in the ledger are skipped. Raises RuntimeError if
    IMAP is not configured; connection/auth errors propagate so the caller can surface them.
    """
    if not imap_configured():
        raise RuntimeError(
            "IMAP not configured — set IMAP_HOST / IMAP_USERNAME / IMAP_PASSWORD in .env"
        )

    ledger = _load_ledger()
    saved: list[str] = []

    logger.info("Connecting to IMAP %s:%s as %s", settings.IMAP_HOST, settings.IMAP_PORT,
                settings.IMAP_USERNAME)
    conn = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
    try:
        conn.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
        status, _ = conn.select(settings.IMAP_MAILBOX)
        if status != "OK":
            raise RuntimeError(f"could not select mailbox {settings.IMAP_MAILBOX!r}")

        criteria = _build_search_criteria()
        typ, data = conn.search(None, *criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        msg_nums = data[0].split()
        logger.info("IMAP search %s -> %d message(s)", criteria, len(msg_nums))

        for num in msg_nums:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                logger.warning("Failed to fetch message %s — skipping", num)
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue

            msg = email.message_from_bytes(raw)
            mid = _message_id(msg, num.decode())
            if mid in ledger:
                logger.debug("Message %s already ingested (ledger) — skipping", mid)
                continue

            subject = str(msg.get("Subject", "")).strip()
            dest = settings.INCOMING_EMAIL_DIR / f"{_safe_name(mid, subject)}.eml"
            dest.write_bytes(raw)
            saved.append(str(dest))
            ledger.add(mid)
            logger.info("Saved incoming email: %s (subject=%r)", dest.name, subject)

            if settings.IMAP_MARK_SEEN:
                conn.store(num, "+FLAGS", "\\Seen")

        _save_ledger(ledger)
    finally:
        # Best-effort cleanup: a dropped connection can make close()/logout() raise, which would
        # otherwise mask the real error being propagated.
        for step in (conn.close, conn.logout):
            try:
                step()
            except imaplib.IMAP4.error:
                pass

    logger.info("Mail poll complete: %d new email(s) downloaded", len(saved))
    return saved


if __name__ == "__main__":
    # Standalone: `python -m src.mail_poller`  — polls once, prints saved files. No LLM, no Synergix.
    settings.configure_logging()
    if not imap_configured():
        print("IMAP not configured. Set IMAP_HOST / IMAP_USERNAME / IMAP_PASSWORD in .env first.")
        raise SystemExit(2)
    files = poll_once()
    print(f"Downloaded {len(files)} new email(s) to {settings.INCOMING_EMAIL_DIR}:")
    for f in files:
        print(f"  - {f}")
