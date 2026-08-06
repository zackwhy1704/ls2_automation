"""SKTC folder intake — Power Automate -> synced folder, alternative to IMAP (src/mail_poller.py).

IMAP Basic Auth is blocked on LS2's Microsoft 365 tenant with no admin available to re-enable it
(see docs/automation_context.md). Instead, a Power Automate flow the mailbox owner can configure
without admin consent watches the SKTC intake mailbox and drops each WO PDF, plus one sidecar JSON
per email, into a OneDrive/SharePoint folder synced locally (see docs/power_automate_sktc_setup.md
for the flow itself — that's a cloud config, not code).

Contract mirrors mail_poller.poll_once(): scan the configured folder, hand back a directory of
ready-to-ingest WO PDFs. From there, src.email_ingestor.ingest_dir() takes over completely unchanged
(it already accepts bare .pdf files, so this adapter does not re-implement any WO parsing).

The sidecar is NOT optional even though Power Automate's own trigger can filter by sender — that
cloud-side filter is a config a future person could loosen without anyone here noticing, so this
module independently re-checks the sender against SKTC_SENDER_ALLOWLIST using the sidecar's own
"sender" field. A PDF without a valid sidecar, or a sidecar with a non-allowlisted sender, is never
silently ingested — both are surfaced as review-worthy outcomes the caller folds into the batch report.

OneDrive sync introduces two new failure modes IMAP never had:
  - Files-On-Demand can leave a cloud-only placeholder that exists in a directory listing but has
    zero bytes locally, or a file can still be mid-write when this scans. Both are handled by a
    stability check (same size across two reads, a few seconds apart) before a PDF is considered ready.
  - The sidecar can land before, after, or never (if Power Automate errors mid-flow) relative to its
    PDF(s). A PDF is given SKTC_INTAKE_SIDECAR_WAIT_SECONDS to find its sidecar before being treated
    as orphaned — logged and routed to review, not silently skipped and not silently ingested without
    sender verification either.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

_STABILITY_CHECK_DELAY_S = 2  # gap between the two size reads used to confirm a file isn't still syncing/writing


@dataclass
class IntakeReviewItem:
    """A PDF or sidecar this adapter could NOT safely auto-ingest. Caller folds these into the batch
    report the same way src.batch already reports extraction failures — by filename, not WO-PO
    (no WO-PO is known yet at this stage, extraction hasn't run)."""

    identifier: str    # filename, used as the report's WO-PO-shaped identifier slot
    reason: str


def _load_ledger(ledger_path: Path) -> set[str]:
    try:
        return set(json.loads(ledger_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return set()


def _save_ledger(ledger_path: Path, ids: set[str]) -> None:
    ledger_path.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def _sidecar_dir(folder: Path) -> Path:
    return folder / "_meta"


def _is_stable(path: Path) -> bool:
    """True if the file's size is non-zero and unchanged across two reads a few seconds apart —
    distinguishes a fully-synced file from a mid-sync OneDrive write or a cloud-only placeholder."""
    try:
        size1 = path.stat().st_size
    except OSError:
        return False
    if size1 == 0:
        return False
    time.sleep(_STABILITY_CHECK_DELAY_S)
    try:
        size2 = path.stat().st_size
    except OSError:
        return False
    return size1 == size2 and size2 > 0


def _sender_allowed(sender: str) -> bool:
    allowlist = [s.strip().lower() for s in settings.SKTC_SENDER_ALLOWLIST.split(",") if s.strip()]
    if not allowlist:
        # No allowlist configured: fail open with a loud warning rather than block everything, since
        # an empty allowlist is very likely a not-yet-configured deployment, not an intentional
        # "trust nobody" policy. Configure SKTC_SENDER_ALLOWLIST before relying on this in production.
        logger.warning("SKTC_SENDER_ALLOWLIST is empty — sender verification is NOT active")
        return True
    sender_l = (sender or "").strip().lower()
    return any(allowed in sender_l for allowed in allowlist)


def _safe_dest_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"


def poll_folder_once() -> tuple[list[str], list[IntakeReviewItem]]:
    """Scan SKTC_INTAKE_FOLDER for new, ready WO PDFs. Returns (pdf_paths, review_items).

    pdf_paths are copied into settings.INCOMING_EMAIL_DIR (the same directory the IMAP path already
    feeds into src.email_ingestor.ingest_dir()) so the rest of the pipeline is untouched. Idempotent
    via a JSON ledger of message-ids, same pattern as src.mail_poller. Raises RuntimeError if the
    configured folder doesn't exist or isn't readable — this must be loud, not "0 WOs found", since a
    signed-out/paused OneDrive sync would otherwise look identical to "no new mail today".
    """
    if not settings.SKTC_INTAKE_FOLDER.strip():
        raise RuntimeError("SKTC_INTAKE_FOLDER is not set in .env")

    folder = Path(settings.SKTC_INTAKE_FOLDER)
    if not folder.is_dir():
        raise RuntimeError(
            f"SKTC intake folder unreachable: {folder} does not exist or is not a directory — "
            "check OneDrive sync status on this machine (signed out? sync paused?) before assuming "
            "there are simply no new WOs"
        )

    meta_dir = _sidecar_dir(folder)
    processed_dir = folder / settings.SKTC_INTAKE_PROCESSED_SUBFOLDER
    processed_dir.mkdir(exist_ok=True)
    (processed_dir / "_meta").mkdir(exist_ok=True)

    ledger_path = folder / ".ingested_message_ids.json"
    ledger = _load_ledger(ledger_path)

    all_pdfs = sorted(p for p in folder.glob("*.pdf") if p.is_file())
    saved: list[str] = []
    review: list[IntakeReviewItem] = []
    consumed_sidecars: set[Path] = set()

    for pdf_path in all_pdfs:
        if not _is_stable(pdf_path):
            logger.info("SKTC intake: %s is not yet stable (still syncing?) — will retry next poll",
                        pdf_path.name)
            continue

        sidecar_path, message_id, sender = _find_sidecar(meta_dir, pdf_path, ledger)

        if sidecar_path is None:
            # No sidecar (yet). Only treat as orphaned once the wait window has elapsed — a fresh
            # PDF whose sidecar simply hasn't synced down yet gets more chances on later polls.
            age_s = time.time() - pdf_path.stat().st_mtime
            if age_s < settings.SKTC_INTAKE_SIDECAR_WAIT_SECONDS:
                logger.info("SKTC intake: %s has no sidecar yet (age %.0fs) — waiting", pdf_path.name, age_s)
                continue
            logger.warning("SKTC intake: %s orphaned — no sidecar found after %ds wait",
                            pdf_path.name, settings.SKTC_INTAKE_SIDECAR_WAIT_SECONDS)
            review.append(IntakeReviewItem(
                identifier=pdf_path.name,
                reason=f"no sidecar metadata found after {settings.SKTC_INTAKE_SIDECAR_WAIT_SECONDS}s "
                       "— cannot verify sender; not auto-ingested",
            ))
            continue

        # Keyed on message_id + filename, not message_id alone: a single email can carry several PDF
        # attachments (see docs/power_automate_sktc_setup.md), and keying on message_id alone would
        # ingest only the FIRST attachment of any multi-PDF email, silently dropping the rest as
        # "already ingested" the moment the first one's message_id landed in the ledger.
        ledger_key = f"{message_id}::{pdf_path.name}"
        if ledger_key in ledger:
            logger.debug("SKTC intake: %s already ingested (in ledger) — skipping", pdf_path.name)
            continue

        if not _sender_allowed(sender):
            logger.warning("SKTC intake: %s sender %r is not in SKTC_SENDER_ALLOWLIST — routing to review",
                            pdf_path.name, sender)
            review.append(IntakeReviewItem(
                identifier=pdf_path.name,
                reason=f"sender {sender!r} is not in SKTC_SENDER_ALLOWLIST — not auto-ingested",
            ))
            continue

        dest = settings.INCOMING_EMAIL_DIR / _safe_dest_name(pdf_path.name)
        shutil.copy2(pdf_path, dest)
        saved.append(str(dest))
        consumed_sidecars.add(sidecar_path)
        ledger.add(ledger_key)
        logger.info("SKTC intake: ingested %s (sender=%r, message_id=%s)", pdf_path.name, sender, message_id)

        _archive(pdf_path, processed_dir / pdf_path.name)

    for sidecar_path in consumed_sidecars:
        _archive(sidecar_path, processed_dir / "_meta" / sidecar_path.name)

    _save_ledger(ledger_path, ledger)

    logger.info("SKTC folder intake complete: %d new PDF(s) ingested, %d flagged for review",
                len(saved), len(review))
    return saved, review


def _find_sidecar(meta_dir: Path, pdf_path: Path, ledger: set[str]) -> tuple[Path | None, str, str]:
    """Find the sidecar JSON whose "attachments" list includes this PDF's filename.

    Returns (sidecar_path_or_None, message_id, sender). Scans all sidecars rather than assuming a
    naming convention between the PDF and its JSON, since Power Automate's exact filename derivation
    for each is independent config — matching by declared attachment name is the one thing the sidecar
    schema guarantees (see docs/power_automate_sktc_setup.md).
    """
    if not meta_dir.is_dir():
        return None, "", ""
    for sidecar_path in meta_dir.glob("*.json"):
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        attachments = data.get("attachments") or []
        if pdf_path.name in attachments:
            return sidecar_path, str(data.get("message_id") or ""), str(data.get("sender") or "")
    return None, "", ""


def _archive(src: Path, dest: Path) -> None:
    """Move a consumed PDF/sidecar into processed/ rather than deleting — cheap audit trail."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except OSError:
        logger.exception("SKTC intake: could not archive %s to %s — leaving in place", src, dest)


if __name__ == "__main__":
    # Standalone: `python -m src.sktc_folder_intake` — polls once, prints results. No LLM, no Synergix.
    settings.configure_logging()
    pdfs, review_items = poll_folder_once()
    print(f"Ingested {len(pdfs)} new PDF(s) into {settings.INCOMING_EMAIL_DIR}:")
    for p in pdfs:
        print(f"  - {p}")
    if review_items:
        print(f"\n{len(review_items)} item(s) need review:")
        for r in review_items:
            print(f"  - {r.identifier}: {r.reason}")
