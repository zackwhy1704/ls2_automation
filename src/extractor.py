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
- A "G/L No." appears near the top. It has two parts: an alphanumeric cost code that ENDS IN A HYPHEN
  (e.g. "731-AN-ANVZCRes-") immediately followed by a numeric account (e.g. "542353-9-721010-0000").
  On some WOs these print on two separate lines; join them into ONE value, keeping the code's trailing
  hyphen as the join, e.g. -> "731-AN-ANVZCRes-542353-9-721010-0000". We want the ENTIRE joined string.
- "Remarks:" holds the location + nature of work, e.g.
  "Blk 330A Anchorvale Street - Inspection for beehive activities at Level 15 - No bees found".
- A line like "Job Sheet: <ref> A <qty> $<rate> $<gross> $<jobcost>" holds the job sheet reference,
  quantity, and the gross Rate (unit_price). There is usually a "Discount %", "Discount Amt",
  "9% GST", and "Grand Total".
- "SR" / "Schedule" reference may appear in the email subject/body, e.g. "(SR: 25955)".

Three fields are easy to confuse — read these rules carefully:

- GL CODE (gl_number): the COMPLETE "G/L No." string — the alphanumeric cost code AND the numeric
  account that follows it, captured as one value exactly as printed. It begins with digits, a hyphen,
  then a segment containing LETTERS, then a numeric account tail. Return the whole thing:
    * "731-AN-ANVZCRes-542353-9-721010-0000"  (NOT just "731-AN-ANVZCRes-")
    * "431-WH-WHR1P1-320121-0-721010-0000"    (NOT just "431-WH-WHR1P1-")
    * "731-CP-SCPMAST-541216-9-721010-0000"
  Do NOT stop at the letter segment and do NOT drop the numeric account tail — Synergix needs the full
  string. This field is CRITICAL. If no such G/L string is present, return "" and add "gl_number" to
  low_confidence_fields.

- JOB SHEET (job_sheet_number): the WO's job/schedule sheet reference. Find it in this order:
    1. A value explicitly labelled "Job Sheet:", e.g. "Job Sheet: 25958" -> "25958". It may be
       alphanumeric (e.g. "A027"); copy it exactly, preserving letters, hyphens and leading zeros.
    2. If there is NO "Job Sheet:" label, some WOs instead use a line-item table with columns
       "Sect." and "Schd." (Schedule). In that case use the "Schd." column value of the line item
       (e.g. Sect.="B", Schd.="002" -> job_sheet_number "002"). Preserve leading zeros.
  This is its OWN field. It is NOT the "Schedule Type" code (e.g. "SN-001556") and NOT the
  "Contractor Code" (e.g. "SN-000004") — never use an "SN-..." value here. Never move it into
  sr_number. Only return "" if you can find neither a "Job Sheet:" label nor a "Schd." column.

- SR NUMBER (sr_number): ONLY a value explicitly labelled "SR" or "SR:" — this is the Service Request
  reference and it almost always appears in the email subject/body, e.g. "(SR: 25955)". It is normally
  a plain number like "25955".
  It is NONE of the following — do NOT put any of these in sr_number:
    * a "Job Sheet:" value (e.g. "25958", "SN-001556");
    * a "Schedule Type" column value (e.g. "SN-001556", "SN-000004") — "Schedule Type" is a service
      classification on the WO, NOT the SR;
    * the WO-PO number.
  If there is no value explicitly labelled "SR", return "" for sr_number. Match on the literal label
  "SR", not merely on the word "Schedule".

Return ONLY a JSON object — no prose, no markdown code fences, no explanation. Use exactly these keys:

{
  "wo_po_number": string,        // "WO-PO/" + digits, preserve leading zeros, e.g. "WO-PO/000060068"
  "town_council": string,        // the town council name from the header, e.g. "Sengkang Town Council"
  "job_sheet_number": string,    // "Job Sheet:" value EXACTLY; may be alphanumeric e.g. "SN-001556" or "25955"
  "service_location": string,    // block/street from Remarks, e.g. "Blk 330A Anchorvale Street"
  "nature_of_work": string,      // the work description from Remarks, e.g. "Inspection for beehive activities..."
  "job_date": string,            // the WO Date as ISO "YYYY-MM-DD"
  "prepared_by": string,         // the "Prepared By" person, e.g. "JENNY ANG"
  "gl_number": string,           // FULL G/L No. string incl. numeric account, e.g. "731-AN-ANVZCRes-542353-9-721010-0000"; CRITICAL
  "quantity": number,            // the line quantity, e.g. 1.0
  "unit_price": number,          // the gross Rate per unit before discount, e.g. 30.00
  "discount_percent": number,    // e.g. 10.0 (0 if none)
  "discount_amount": number,     // e.g. 3.00 (0 if none)
  "net_amount": number,          // = (quantity*unit_price) MINUS discount_amount, before GST. NOT the gross. e.g. 30.00-3.00=27.00
  "gst_percent": number,         // e.g. 9.0 (0 if none)
  "grand_total": number,         // net + GST, e.g. 29.43
  "sr_number": string,           // ONLY a value labelled "SR"/"Schedule" (e.g. "25955"); else "". NOT the job sheet
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

    # net_amount is the one figure the model gets arithmetically wrong run-to-run (it sometimes
    # returns the gross). Compute it deterministically from the fields it DOES read reliably:
    # net = quantity*unit_price - discount. Fall back to discount_percent, then the model's value.
    quantity = float(data.get("quantity", 0) or 0)
    unit_price = float(data.get("unit_price", 0) or 0)
    disc_amt = _opt_float("discount_amount")
    disc_pct = _opt_float("discount_percent")
    gross = quantity * unit_price
    net_amount: float | None
    if gross > 0:
        if disc_amt is not None:
            net_amount = round(gross - disc_amt, 2)
        elif disc_pct is not None:
            net_amount = round(gross * (1 - disc_pct / 100), 2)
        else:
            net_amount = round(gross, 2)
    else:
        net_amount = _opt_float("net_amount")  # no usable line figures — trust the model

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
            quantity=quantity,
            unit_price=unit_price,
            discount_percent=disc_pct,
            discount_amount=disc_amt,
            net_amount=net_amount,
            gst_percent=_opt_float("gst_percent"),
            grand_total=_opt_float("grand_total"),
            sr_number=sr or None,
            source_path=source_path,
            extraction_confidence=float(confidence) if confidence is not None else None,
            low_confidence_fields=[str(f) for f in low_conf],
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
