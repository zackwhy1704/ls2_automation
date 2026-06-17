"""Ingest Work Orders from email files (`.msg` Outlook or `.eml` MIME).

Replaces the TCMS web scraper as the WO source for the email-based flow. From the real samples, each
email carries the Work Order(s) as **PDF attachment(s)** (e.g. `000060068.pdf`); the body is a cover
note, and inline `image00N.png` parts are email-signature images, not WOs.

A single email may contain MULTIPLE WOs (one PDF each), so ingestion yields ONE `IngestedWO` per PDF
attachment. When an email has no PDF at all, a single `IngestedWO` is still produced with the email
body as the source (the saved email file is what Synergix would attach), so the extractor can fall
back to inline body text.

The original email is always copied into data/pdfs/ so the Synergix "attach" step has a file even
when there is no PDF attachment.
"""
from __future__ import annotations

import email
import logging
import re
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

_PDF_CT = "application/pdf"


@dataclass
class IngestedWO:
    """One Work Order's raw materials, before LLM field extraction.

    One per PDF attachment. For a body-only email, pdf_path is None and the source is the saved email.
    """

    source_email_path: str              # the saved original .msg/.eml
    pdf_path: str | None = None         # the single WO PDF for this unit (None for body-only emails)
    body_text: str = ""                 # decoded email body (cover note / fallback WO text)
    subject: str = ""
    attachment_name: str = ""           # original attachment filename, for logging/audit

    @property
    def primary_source_path(self) -> str:
        """The file the extractor reads and Synergix attaches: the PDF if present, else the email."""
        return self.pdf_path or self.source_email_path

    @property
    def has_pdf(self) -> bool:
        return self.pdf_path is not None


def _safe_stem(name: str) -> str:
    """Filesystem-safe stem derived from an arbitrary string (subject / filename)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return stem[:80] or "wo"


def _is_pdf_attachment(filename: str, content_type: str) -> bool:
    return content_type == _PDF_CT or filename.lower().endswith(".pdf")


# ---------------------------------------------------------------------------- .eml (MIME) parsing
def _eml_body_text(msg: Message) -> str:
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, ValueError):
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain_chunks.append(text)
        elif part.get_content_type() == "text/html":
            html_chunks.append(text)
    if plain_chunks:
        return "\n".join(plain_chunks).strip()
    if html_chunks:
        stripped = re.sub(r"<[^>]+>", " ", "\n".join(html_chunks))
        return re.sub(r"[ \t]+", " ", stripped).strip()
    return ""


def _eml_pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        if not _is_pdf_attachment(filename, part.get_content_type()):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((filename or "attachment.pdf", payload))
    return out


def _parse_eml(raw: bytes) -> tuple[str, str, list[tuple[str, bytes]]]:
    msg = email.message_from_bytes(raw)
    subject = str(msg.get("Subject", "")).strip()
    return subject, _eml_body_text(msg), _eml_pdf_attachments(msg)


# ---------------------------------------------------------------------------- .msg (Outlook) parsing
def _parse_msg(path: Path) -> tuple[str, str, list[tuple[str, bytes]]]:
    # Imported lazily so the pure-logic test suite needn't have extract-msg installed.
    import extract_msg

    m = extract_msg.Message(str(path))
    subject = (m.subject or "").strip()
    body = m.body or ""
    pdfs: list[tuple[str, bytes]] = []
    for a in m.attachments:
        name = a.longFilename or a.shortFilename or ""
        ctype = (getattr(a, "mimetype", None) or "").lower()
        if not _is_pdf_attachment(name, ctype):
            continue  # skips inline image00N.png signature images
        data = a.data
        if isinstance(data, (bytes, bytearray)):
            pdfs.append((name or "attachment.pdf", bytes(data)))
    return subject, body.strip(), pdfs


# ---------------------------------------------------------------------------- public API
def ingest_file(path_str: str) -> list[IngestedWO]:
    """Parse one email file (.msg or .eml) into a list of WOs — one per PDF attachment.

    Returns a list because a single email may carry multiple Work Orders. A body-only email yields a
    single body-source WO. Raises FileNotFoundError if the file is missing; unsupported suffixes raise
    ValueError.
    """
    src = Path(path_str)
    raw = src.read_bytes()
    suffix = src.suffix.lower()

    if suffix == ".msg":
        subject, body_text, pdf_attachments = _parse_msg(src)
    elif suffix == ".eml":
        subject, body_text, pdf_attachments = _parse_eml(raw)
    else:
        raise ValueError(f"unsupported email format {suffix!r} (expected .msg or .eml): {src.name}")

    stem = _safe_stem(subject or src.stem)
    # Preserve the original extension on the saved copy so it can be re-opened if needed.
    email_copy = settings.PDF_DIR / f"{stem}{suffix}"
    email_copy.write_bytes(raw)

    wos: list[IngestedWO] = []
    for idx, (att_name, data) in enumerate(pdf_attachments, start=1):
        out_name = _safe_stem(Path(att_name).stem) or f"{stem}_attach{idx}"
        dest = settings.PDF_DIR / f"{out_name}.pdf"
        dest.write_bytes(data)
        logger.info("Saved WO PDF: %s (from attachment %r)", dest, att_name)
        wos.append(
            IngestedWO(
                source_email_path=str(email_copy),
                pdf_path=str(dest),
                body_text=body_text,
                subject=subject,
                attachment_name=att_name,
            )
        )

    if not wos:
        # No PDF — fall back to a single body-source WO.
        logger.info("No PDF attachment in %s; using email body as WO source", src.name)
        wos.append(
            IngestedWO(
                source_email_path=str(email_copy),
                pdf_path=None,
                body_text=body_text,
                subject=subject,
            )
        )

    logger.info("Ingested %s: subject=%r -> %d WO unit(s)", src.name, subject, len(wos))
    return wos


def ingest_dir(dir_path: str) -> list[IngestedWO]:
    """Ingest every .msg/.eml in a directory (non-recursive). Per-file errors are logged, not fatal."""
    results: list[IngestedWO] = []
    paths = sorted(p for p in Path(dir_path).iterdir() if p.suffix.lower() in {".msg", ".eml"})
    for p in paths:
        try:
            results.extend(ingest_file(str(p)))
        except Exception:
            logger.exception("Failed to ingest %s — skipping", p)
    logger.info("Ingested %d WO unit(s) from %d email(s) in %s", len(results), len(paths), dir_path)
    return results


if __name__ == "__main__":
    # Standalone test: `python -m src.email_ingestor <file.msg|.eml | dir>`  (no LLM, no network)
    import sys

    settings.configure_logging()
    if len(sys.argv) < 2:
        print("usage: python -m src.email_ingestor <file.msg|.eml | dir>")
        raise SystemExit(2)
    target = Path(sys.argv[1])
    items = ingest_dir(str(target)) if target.is_dir() else ingest_file(str(target))
    for wo in items:
        print(f"- subject={wo.subject!r} attachment={wo.attachment_name!r}")
        print(f"  primary_source={wo.primary_source_path}")
        print(f"  has_pdf={wo.has_pdf}  body_preview={wo.body_text[:160]!r}")
