"""Claude Haiku WO field extraction: a WO source (PDF or text) -> structured WOPayload + confidence.

Handles both forms a Work Order can arrive in:
  - a PDF (a PDF attachment from an email, or a scraped TCMS PDF) -> sent as a document block
  - inline email body text                                        -> sent as a text block

Parses defensively (strips stray markdown fences, tolerates a confidence/flags wrapper) and
validates into a WOPayload. On any parse/validation failure the caller marks the WO INVALID and the
raw model response is logged.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from config import settings
from src.models import WOPayload

logger = logging.getLogger(__name__)

# We ask the model for a flat JSON object with these keys (a superset of WOPayload's source fields,
# plus confidence + low_confidence_fields). source_path is injected by us, not the model.
_SYSTEM_PROMPT = """You extract billing fields from a single Work Order (WO) for a pest control company.
The WO may be supplied as a PDF document or as the plain text of an email.

Return ONLY a JSON object — no prose, no markdown code fences, no explanation. Use exactly these keys:

{
  "wo_po_number": string,        // format "WO-PO/" followed by digits, e.g. "WO-PO/123456789"
  "job_sheet_number": string,    // the job sheet reference; preserve its exact leading character
  "service_location": string,
  "nature_of_work": string,      // a short description of the pest control work performed
  "job_date": string,            // ISO date "YYYY-MM-DD"
  "prepared_by": string,         // person who prepared the WO (becomes the customer contact)
  "gl_number": string,           // the GL number; this is CRITICAL — if you cannot find it, return ""
  "quantity": number,
  "unit_price": number,
  "confidence": number,          // your overall confidence 0..1
  "low_confidence_fields": [string]  // names of any fields you are unsure about (especially gl_number)
}

If a field is genuinely absent from the PDF, return an empty string (or 0 for numbers) and add its name
to low_confidence_fields. Never invent values.
"""


class ExtractionError(Exception):
    """Raised when the model response cannot be parsed into the expected JSON shape."""


def _strip_fences(text: str) -> str:
    """Remove a leading/trailing ```...``` fence if the model added one despite instructions."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, flags=re.DOTALL)
    return fence.group(1).strip() if fence else t


def _parse_response(raw: str) -> dict:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _build_client():
    # Imported lazily so the pure-logic test suite never needs the anthropic package installed.
    import anthropic

    if not settings.ANTHROPIC_API_KEY:
        raise ExtractionError("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _pdf_content_block(pdf_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
    }


def _call_model(content_blocks: list[dict], label: str) -> str:
    """Send content blocks to Haiku and return the concatenated text response."""
    client = _build_client()
    message = client.messages.create(
        model=settings.EXTRACTION_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
    )
    raw = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    logger.debug("Raw extraction response for %s: %s", label, raw)
    return raw


def extract_from_pdf(pdf_path: str) -> WOPayload:
    """Extract a WOPayload from a WO PDF file."""
    path = Path(pdf_path)
    content = [
        _pdf_content_block(path.read_bytes()),
        {"type": "text", "text": "Extract the WO billing fields as the specified JSON."},
    ]
    return _finalize(_call_model(content, path.name), source_path=str(path))


def extract_from_text(body_text: str, *, source_path: str) -> WOPayload:
    """Extract a WOPayload from inline email-body text.

    `source_path` is recorded on the payload (e.g. the saved .eml) for the Synergix attach step.
    """
    if not body_text.strip():
        raise ExtractionError("email body text is empty — nothing to extract")
    content = [
        {
            "type": "text",
            "text": (
                "The following is the text of a Work Order email. Extract the WO billing fields "
                "as the specified JSON.\n\n--- EMAIL BODY ---\n" + body_text
            ),
        }
    ]
    return _finalize(_call_model(content, Path(source_path).name), source_path=source_path)


def extract(source_path: str) -> WOPayload:
    """Dispatch by file type: .pdf -> document extraction; anything else -> read as text.

    Convenience entry point for a single source file. For email ingestion prefer the more explicit
    extract_from_pdf / extract_from_text driven by the IngestedWO (PDF attachment vs. inline body).
    """
    path = Path(source_path)
    if path.suffix.lower() == ".pdf":
        return extract_from_pdf(source_path)
    # .eml / .txt / .html etc. — read as text and extract from the body.
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_from_text(text, source_path=source_path)


def _finalize(raw: str, *, source_path: str) -> WOPayload:
    """Parse a model response and build a validated WOPayload, recording source_path."""
    data = _parse_response(raw)

    confidence = data.get("confidence")
    low_conf = data.get("low_confidence_fields") or []
    if low_conf:
        logger.warning("%s: model flagged low-confidence fields: %s", Path(source_path).name, low_conf)

    try:
        payload = WOPayload(
            wo_po_number=str(data.get("wo_po_number", "")),
            job_sheet_number=str(data.get("job_sheet_number", "")),
            service_location=str(data.get("service_location", "")),
            nature_of_work=str(data.get("nature_of_work", "")),
            job_date=str(data.get("job_date", "")),          # pydantic parses ISO date string
            prepared_by=str(data.get("prepared_by", "")),
            gl_number=str(data.get("gl_number", "")),
            quantity=float(data.get("quantity", 0) or 0),
            unit_price=float(data.get("unit_price", 0) or 0),
            source_path=source_path,
            extraction_confidence=float(confidence) if confidence is not None else None,
        )
    except Exception as exc:  # pydantic ValidationError, ValueError, etc.
        raise ExtractionError(f"extracted fields failed model validation: {exc}\nraw: {raw}") from exc

    return payload


if __name__ == "__main__":
    # Standalone test: `python -m src.extractor <sample.pdf | sample.eml | sample.txt>`
    import sys

    settings.configure_logging()
    if len(sys.argv) < 2:
        print("usage: python -m src.extractor <sample.pdf | sample.eml | sample.txt>")
        raise SystemExit(2)
    sample = sys.argv[1]
    try:
        result = extract(sample)
        print(json.dumps(result.model_dump(), default=str, indent=2))
    except ExtractionError as e:
        print(f"EXTRACTION FAILED: {e}")
        raise SystemExit(1)
