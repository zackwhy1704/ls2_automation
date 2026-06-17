"""Ingest Work Orders from `.eml` email files.

Replaces the TCMS web scraper as the WO source for the email-based flow. Parses an .eml with the
stdlib `email` package and produces, per email, an `IngestedWO`:

  - any PDF attachment(s) saved to data/pdfs/  (preferred WO source)
  - the plain-text body (fallback / supplement when the WO is inline, not attached)

We do NOT assume the WO is a PDF attachment vs. inline body — both are captured so the extractor can
use whichever is present. The original .eml is also copied to data/pdfs/ so the Synergix "attach"
step always has a file to upload even when there is no PDF attachment.
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
    """The raw materials extracted from one email, before LLM field extraction."""

    eml_path: str                       # source .eml, copied into data/pdfs/
    pdf_paths: list[str] = field(default_factory=list)  # saved PDF attachment(s), if any
    body_text: str = ""                 # plain-text body (decoded), if any
    subject: str = ""

    @property
    def primary_source_path(self) -> str:
        """The file the extractor should read, and the file Synergix attaches.

        Prefer the first PDF attachment; fall back to the saved .eml when there is no PDF.
        """
        return self.pdf_paths[0] if self.pdf_paths else self.eml_path

    @property
    def has_pdf(self) -> bool:
        return bool(self.pdf_paths)


def _safe_stem(name: str) -> str:
    """Filesystem-safe stem derived from an arbitrary string (subject / filename)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return stem[:80] or "wo"


def _decode_body_text(msg: Message) -> str:
    """Return a best-effort plain-text rendering of the email body.

    Prefers text/plain parts; falls back to stripped text/html if that's all there is.
    """
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
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
        if ctype == "text/plain":
            plain_chunks.append(text)
        elif ctype == "text/html":
            html_chunks.append(text)

    if plain_chunks:
        return "\n".join(plain_chunks).strip()
    if html_chunks:
        # Crude HTML -> text: drop tags. Good enough to feed an LLM; not for display.
        stripped = re.sub(r"<[^>]+>", " ", "\n".join(html_chunks))
        return re.sub(r"[ \t]+", " ", stripped).strip()
    return ""


def _save_pdf_attachments(msg: Message, stem: str) -> list[str]:
    saved: list[str] = []
    idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename() or ""
        disp = (part.get("Content-Disposition") or "").lower()
        is_pdf = ctype == _PDF_CT or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        # Treat as an attachment even if Content-Disposition is missing, as long as it's a PDF.
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        idx += 1
        out_name = _safe_stem(Path(filename).stem) if filename else f"{stem}_attach{idx}"
        dest = settings.PDF_DIR / f"{out_name}.pdf"
        dest.write_bytes(payload)
        saved.append(str(dest))
        logger.info("Saved PDF attachment: %s (disposition=%r)", dest, disp or "none")
    return saved


def ingest_file(eml_path: str) -> IngestedWO:
    """Parse one .eml and return its WO materials. Raises FileNotFoundError if missing."""
    src = Path(eml_path)
    raw = src.read_bytes()
    msg = email.message_from_bytes(raw)

    subject = str(msg.get("Subject", "")).strip()
    stem = _safe_stem(subject or src.stem)

    # Copy the original .eml into the data dir so it's the fallback Synergix attachment.
    eml_copy = settings.PDF_DIR / f"{stem}.eml"
    eml_copy.write_bytes(raw)

    pdf_paths = _save_pdf_attachments(msg, stem)
    body_text = _decode_body_text(msg)

    logger.info(
        "Ingested %s: subject=%r pdf_attachments=%d body_chars=%d",
        src.name, subject, len(pdf_paths), len(body_text),
    )
    return IngestedWO(
        eml_path=str(eml_copy),
        pdf_paths=pdf_paths,
        body_text=body_text,
        subject=subject,
    )


def ingest_dir(dir_path: str) -> list[IngestedWO]:
    """Ingest every .eml in a directory (non-recursive). Per-file errors are logged, not fatal."""
    results: list[IngestedWO] = []
    for p in sorted(Path(dir_path).glob("*.eml")):
        try:
            results.append(ingest_file(str(p)))
        except Exception:
            logger.exception("Failed to ingest %s — skipping", p)
    logger.info("Ingested %d email(s) from %s", len(results), dir_path)
    return results


if __name__ == "__main__":
    # Standalone test: `python -m src.email_ingestor path/to/wo.eml`  (or a directory of .eml)
    import sys

    settings.configure_logging()
    if len(sys.argv) < 2:
        print("usage: python -m src.email_ingestor <file.eml | dir-of-emls>")
        raise SystemExit(2)
    target = Path(sys.argv[1])
    items = ingest_dir(str(target)) if target.is_dir() else [ingest_file(str(target))]
    for wo in items:
        print(f"- subject={wo.subject!r}")
        print(f"  primary_source={wo.primary_source_path}")
        print(f"  pdf_attachments={wo.pdf_paths}")
        print(f"  body_preview={wo.body_text[:200]!r}")
