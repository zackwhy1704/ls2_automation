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
_SYSTEM_PROMPT = """You extract billing fields from a single Town Council Work Order (WO) for a pest
control contractor (LS 2 Services). The WO may be supplied as a PDF document or as the plain text of
an email.

Layout cues from these WOs (use them, but rely on the actual document):
- Header line: "WO Date <DD-Mon-YYYY>  No. WO-PO/<digits>".
- A "G/L <code>" appears near the top, e.g. "731-AN-ANVZLRes-Contractor". There may ALSO be a long
  numeric account "No. 541330-1-721010-0000". The gl_number we want is the ALPHANUMERIC code that
  starts with digits then a hyphen and letters (e.g. "731-AN-ANVZLRes-..."), NOT the long numeric.
- "Remarks:" holds the location + nature of work, e.g.
  "Blk 330A Anchorvale Street - Inspection for beehive activities at Level 15 - No bees found".
- A line like "Job Sheet: <number> A <qty> $<rate> $<gross> $<jobcost>" holds the job sheet number,
  quantity, and the gross Rate (unit_price). There is usually a "Discount %", "Discount Amt",
  "9% GST", and "Grand Total".
- "SR" / "Schedule" reference may appear in the email subject/body, e.g. "(SR: 25955)".

Return ONLY a JSON object — no prose, no markdown code fences, no explanation. Use exactly these keys:

{
  "wo_po_number": string,        // "WO-PO/" + digits, preserve leading zeros, e.g. "WO-PO/000060068"
  "town_council": string,        // the town council name from the header, e.g. "Sengkang Town Council"
  "job_sheet_number": string,    // the "Job Sheet:" value, e.g. "25955"; preserve its leading char
  "service_location": string,    // block/street from Remarks, e.g. "Blk 330A Anchorvale Street"
  "nature_of_work": string,      // the work description from Remarks, e.g. "Inspection for beehive activities..."
  "job_date": string,            // the WO Date as ISO "YYYY-MM-DD"
  "prepared_by": string,         // the "Prepared By" person, e.g. "JENNY ANG"
  "gl_number": string,           // the alphanumeric G/L code (e.g. "731-AN-ANVZLRes-..."); CRITICAL — "" if truly absent
  "quantity": number,            // the line quantity, e.g. 1.0
  "unit_price": number,          // the gross Rate per unit before discount, e.g. 30.00
  "discount_percent": number,    // e.g. 10.0 (0 if none)
  "discount_amount": number,     // e.g. 3.00 (0 if none)
  "net_amount": number,          // gross less discount, before GST, e.g. 27.00
  "gst_percent": number,         // e.g. 9.0 (0 if none)
  "grand_total": number,         // net + GST, e.g. 29.43
  "sr_number": string,           // the SR/Schedule reference if present, else ""
  "confidence": number,          // your overall confidence 0..1
  "low_confidence_fields": [string]  // fields you are unsure about (especially gl_number)
}

If a field is genuinely absent, return an empty string (or 0 for numbers) and add its name to
low_confidence_fields. Never invent values.
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

    def _opt_float(key: str) -> float | None:
        """Optional numeric: missing/empty/0 -> None, so 'absent' is distinct from a real 0.00."""
        v = data.get(key)
        if v in (None, "", 0, 0.0):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    sr = str(data.get("sr_number", "") or "").strip()

    try:
        payload = WOPayload(
            wo_po_number=str(data.get("wo_po_number", "")),
            town_council=str(data.get("town_council", "") or ""),
            job_sheet_number=str(data.get("job_sheet_number", "")),
            service_location=str(data.get("service_location", "")),
            nature_of_work=str(data.get("nature_of_work", "")),
            job_date=str(data.get("job_date", "")),          # pydantic parses ISO date string
            prepared_by=str(data.get("prepared_by", "")),
            gl_number=str(data.get("gl_number", "")),
            quantity=float(data.get("quantity", 0) or 0),
            unit_price=float(data.get("unit_price", 0) or 0),
            discount_percent=_opt_float("discount_percent"),
            discount_amount=_opt_float("discount_amount"),
            net_amount=_opt_float("net_amount"),
            gst_percent=_opt_float("gst_percent"),
            grand_total=_opt_float("grand_total"),
            sr_number=sr or None,
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
