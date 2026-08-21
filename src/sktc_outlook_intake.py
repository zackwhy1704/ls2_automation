"""SKTC intake by reading the mailbox directly through CLASSIC Outlook (COM).

WHY THIS EXISTS
    IMAP is unusable for this mailbox: Basic Auth for IMAP is disabled tenant-wide on LS2's M365
    (Microsoft's default since Oct 2022) and there is no admin available to enable app passwords or
    OAuth2 — confirmed by a live login test, not assumed. The documented fallback was Power Automate
    writing attachments into a OneDrive-synced folder (see sktc_folder_intake), but that adds two
    independent things that can silently stop — a cloud flow anyone can edit or disable, and a sync
    client that can pause or conflict — plus a sidecar JSON whose "sender" field has to be trusted
    for the allowlist check.

    Classic Outlook is already signed in to this mailbox with modern auth, which is exactly what
    IMAP cannot do. COM attaches to that session, so:
      - no app password, no admin consent, no Azure app registration
      - no cloud flow and no OneDrive sync in the path
      - no sidecar: sender, subject, received time and attachments come from the mail item itself,
        so the allowlist check is authoritative rather than trusting a file a cloud flow wrote
    The cost is that the host must stay powered on and logged in — which this deployment already
    requires anyway, because the TCMS scraper runs non-headless and needs a real desktop.

    NOTE the new Outlook for Windows (the Store app, Microsoft.OutlookForWindows) has NO COM
    interface and is not usable here. It also refused this account outright ("your account does not
    have permission to access the new Outlook on Windows") because it wants an M365 subscription
    entitlement the mailbox lacks. Classic Outlook is licensed separately by the Office
    Home & Business retail install on this host, which is why it works where the new app does not.

SAFETY / IDEMPOTENCY
    - Read-only against the mailbox: nothing is marked read, moved, or deleted. Re-processing is
      prevented by a JSON ledger of message ids, the same pattern as mail_poller and
      sktc_folder_intake, so the mailbox itself is never mutated to track state.
    - Sender allowlist is re-checked here and FAILS CLOSED on an empty SKTC_SENDER_ALLOWLIST, same
      as the folder adapter.
    - A PDF that does not look like a WO is NOT silently dropped; it is returned as a review item so
      it shows up in the batch report. Real mail from @sktc.sg carries plenty of non-WO PDFs — job
      sheet photo reports, invoices, and (observed) a WhatsApp group chat screenshot — and quietly
      discarding attachments is how a real WO would eventually go missing.
    - Outlook being unavailable raises RuntimeError. It must never look like "no new mail today":
      that is the failure mode that lets billing stop silently for days.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# olFolderInbox; olMail
_OL_FOLDER_INBOX = 6
_OL_MAIL = 43

# PR_INTERNET_MESSAGE_ID / PR_SENDER_SMTP_ADDRESS, via the MAPI property accessor.
_PROP_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
_PROP_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"

# How many recent inbox items to inspect per poll. The inbox is a working mailbox, not an archive,
# and the ledger makes re-scanning cheap, so this only needs to comfortably exceed one poll's worth
# of mail.
_SCAN_LIMIT = 200

# A WO attachment from SKTC is named after the WO number: "000061116.pdf". Everything else observed
# in real mail is not a WO — "25973-  PIGEON TREATMENT ... .pdf" (a job sheet photo report),
# "Jenny Ang_WO-PO000060666_SIN0006063_Jul 26.pdf" (an invoice), "LS2 Whatsapp Group Chat SS ....pdf".
# Also accept an explicit WO-PO-prefixed name, since that spelling is plausible and harmless to allow.
_WO_FILENAME_RE = re.compile(r"^(?:wo[-_ ]?po[/\-_ ]?)?0*\d{6,}$", re.I)


@dataclass
class IntakeReviewItem:
    """A PDF this adapter would not auto-ingest. Mirrors sktc_folder_intake.IntakeReviewItem so
    main.py can fold either adapter's output into the batch report identically."""

    identifier: str
    reason: str


def _ledger_path() -> Path:
    return Path(settings.INCOMING_EMAIL_DIR) / ".outlook_ingested_message_ids.json"


def _load_ledger() -> set[str]:
    try:
        return set(json.loads(_ledger_path().read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError, OSError):
        return set()


def _save_ledger(ids: set[str]) -> None:
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def _sender_allowed(sender: str) -> bool:
    """Same fail-closed rule as sktc_folder_intake: an unset allowlist trusts nobody. A forgotten
    .env line before go-live must not silently ingest WO PDFs from any sender."""
    allowlist = [s.strip().lower() for s in settings.SKTC_SENDER_ALLOWLIST.split(",") if s.strip()]
    if not allowlist:
        logger.warning("SKTC_SENDER_ALLOWLIST is empty — routing all senders to review until it is set")
        return False
    sender_l = (sender or "").strip().lower()
    return any(allowed in sender_l for allowed in allowlist)


def looks_like_wo_pdf(filename: str) -> bool:
    name = (filename or "").strip()
    if not name.lower().endswith(".pdf"):
        return False
    return bool(_WO_FILENAME_RE.match(Path(name).stem.strip()))


def _safe_dest_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file.pdf"


def _sender_of(item) -> str:
    """The sender's real SMTP address. item.SenderEmailAddress is an opaque EX Distinguished Name
    for internal senders, so it cannot be allowlist-matched directly."""
    try:
        addr = item.Sender.GetExchangeUser().PrimarySmtpAddress
        if addr:
            return str(addr)
    except Exception:
        pass
    for prop in (_PROP_SENDER_SMTP, _PROP_INTERNET_MESSAGE_ID):
        if prop != _PROP_SENDER_SMTP:
            break
        try:
            addr = item.PropertyAccessor.GetProperty(prop)
            if addr:
                return str(addr)
        except Exception:
            pass
    return str(getattr(item, "SenderEmailAddress", "") or "")


def _message_id_of(item) -> str:
    """Stable per-message key for the ledger. Prefers the RFC Message-ID; falls back to Outlook's
    EntryID, which is stable within this store."""
    try:
        mid = item.PropertyAccessor.GetProperty(_PROP_INTERNET_MESSAGE_ID)
        if mid:
            return str(mid)
    except Exception:
        pass
    return str(getattr(item, "EntryID", "") or "")


def poll_outlook_once() -> tuple[list[str], list[IntakeReviewItem]]:
    """Pull new SKTC WO PDFs out of the Outlook inbox. Returns (pdf_paths, review_items).

    PDFs are written into settings.INCOMING_EMAIL_DIR — the same directory the IMAP and folder paths
    feed — so nothing downstream changes. Raises RuntimeError if Outlook cannot be reached.
    """
    try:
        import pythoncom  # noqa: PLC0415  (COM must be initialised in the calling thread)
        import win32com.client  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pywin32 is not installed, so Outlook intake cannot run (pip install pywin32)"
        ) from exc

    # main.py invokes this via asyncio.to_thread, i.e. on a worker thread. COM must be initialised
    # per-thread or Dispatch fails with CoInitialize-not-called.
    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")
            inbox = ns.GetDefaultFolder(_OL_FOLDER_INBOX)
            items = inbox.Items
            items.Sort("[ReceivedTime]", True)
            count = items.Count
        except Exception as exc:
            raise RuntimeError(
                "could not reach Outlook via COM. Classic Outlook must be running and signed in on "
                f"this host (the new Outlook Store app has no COM interface): {exc}"
            ) from exc

        dest_dir = Path(settings.INCOMING_EMAIL_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        ledger = _load_ledger()
        saved: list[str] = []
        review: list[IntakeReviewItem] = []
        seen_now: set[str] = set()

        for idx in range(1, min(_SCAN_LIMIT, count) + 1):
            try:
                item = items.Item(idx)
                if getattr(item, "Class", _OL_MAIL) != _OL_MAIL:
                    continue
                msg_id = _message_id_of(item)
                if not msg_id or msg_id in ledger:
                    continue
                sender = _sender_of(item)
                subject = (getattr(item, "Subject", "") or "").strip()

                pdfs = []
                for att in item.Attachments:
                    nm = att.FileName or ""
                    if nm.lower().endswith(".pdf"):
                        pdfs.append((nm, att))
                if not pdfs:
                    continue

                if not _sender_allowed(sender):
                    review.append(IntakeReviewItem(
                        identifier=subject[:60] or "(no subject)",
                        reason=f"sender {sender or '(unknown)'} is not in SKTC_SENDER_ALLOWLIST",
                    ))
                    seen_now.add(msg_id)
                    continue

                for nm, att in pdfs:
                    if not looks_like_wo_pdf(nm):
                        review.append(IntakeReviewItem(
                            identifier=nm,
                            reason=("attachment does not look like a WO PDF (expected a WO-number "
                                    f"filename like 000061116.pdf) — from {sender}, "
                                    f"subject {subject[:60]!r}"),
                        ))
                        continue
                    dest = dest_dir / _safe_dest_name(nm)
                    try:
                        att.SaveAsFile(str(dest))
                    except Exception as exc:
                        review.append(IntakeReviewItem(
                            identifier=nm, reason=f"could not save attachment: {exc}"))
                        continue
                    saved.append(str(dest))
                    logger.info("SKTC intake: saved %s from %s", nm, sender)
                seen_now.add(msg_id)
            except Exception:
                logger.exception("SKTC intake: error on inbox item %d — continuing", idx)

        if seen_now:
            _save_ledger(ledger | seen_now)
        logger.info("SKTC Outlook intake: %d PDF(s) ingested, %d for review, %d message(s) marked "
                    "processed", len(saved), len(review), len(seen_now))
        return saved, review
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
