"""Validation rules, project-code resolution, and the remarks builder.

Pure functions, no external dependencies — fully unit-testable on their own.
"""
from __future__ import annotations

import re
from datetime import date

from config import settings
from src.models import (
    PROJECT_CODE_ECOCARE,
    PROJECT_CODE_INFIGO,
    WOPayload,
)

_WO_PO_RE = re.compile(r"^WO-PO/\d+$")

# Fields whose extraction being uncertain is billing-critical enough to block auto-submit outright,
# rather than just being logged. gl_number is the one the extraction prompt itself calls CRITICAL.
_CRITICAL_LOW_CONFIDENCE_FIELDS = {"gl_number", "wo_po_number", "unit_price", "quantity"}

# Absolute cents of slack allowed in the grand_total = net * (1 + GST%) cross-check, to absorb
# the model's own rounding (e.g. GST computed on a slightly different intermediate rounding).
_MONEY_TOLERANCE = 0.02


class ValidationError(Exception):
    """Raised by resolve_project_code when the job sheet prefix is unrecognised."""


def resolve_project_code(job_sheet_number: str) -> str:
    """Resolve the Synergix project code from the job sheet number prefix.

    - starts with a letter -> Infigo (2000069)
    - starts with a digit  -> Ecocare (2000050)
    - else                 -> raise (caller marks the WO INVALID)

    # TODO(human): confirm these two project codes + the prefix rule against real WO samples.
    """
    js = (job_sheet_number or "").strip()
    if not js:
        raise ValidationError("job_sheet_number is empty")
    first = js[0]
    if first.isalpha():
        return PROJECT_CODE_INFIGO
    if first.isdigit():
        return PROJECT_CODE_ECOCARE
    raise ValidationError(f"job_sheet_number has unrecognised prefix: {first!r}")


def validate(payload: WOPayload) -> list[str]:
    """Return a list of human-readable validation errors. Empty list == valid.

    Implements every rule from the workflow doc.
    """
    errors: list[str] = []

    if not _WO_PO_RE.match(payload.wo_po_number or ""):
        errors.append(f"wo_po_number {payload.wo_po_number!r} does not match WO-PO/<digits>")

    # gl_number is critical — its absence blocks the WO.
    if not (payload.gl_number and payload.gl_number.strip()):
        errors.append("gl_number is missing (critical — required for Reference No.)")

    if payload.job_date > date.today():
        errors.append(f"job_date {payload.job_date.isoformat()} is in the future")

    if not (payload.unit_price > 0):
        errors.append(f"unit_price must be > 0 (got {payload.unit_price})")

    if not (payload.prepared_by and payload.prepared_by.strip()):
        errors.append("prepared_by is missing")

    # Project code must resolve.
    try:
        resolve_project_code(payload.job_sheet_number)
    except ValidationError as exc:
        errors.append(str(exc))

    return errors


def check_extraction_trust(payload: WOPayload, *, min_confidence: float | None = None) -> list[str]:
    """Return reasons this extraction should NOT be auto-billed. Empty list == safe to auto-submit.

    Distinct from validate(): validate() catches bad/missing data on the WO itself (-> INVALID,
    dropped — the source document is malformed). This catches "the data looks well-formed but we're
    not confident the extraction read it correctly" (-> NEEDS_REVIEW, the same fail-safe bucket as an
    uncertain dedup — a human looks at it, it is not treated as a broken WO). A not-a-duplicate result
    was never on its own enough to justify auto-billing; this is the gate that sits between a clean
    dedup and the actual write.

    Three checks:
      1. money consistency — net_amount is already deterministically recomputed by the extractor from
         quantity*unit_price minus discount, so it can't disagree with those inputs. grand_total and
         gst_percent come straight from the model, though — if a line figure was misread (e.g. $30.00
         as $300.00), grand_total will no longer match net_amount + GST. That mismatch is the signal.
      2. a CRITICAL field (gl_number, wo_po_number, unit_price, quantity) the model itself flagged as
         low-confidence, even if overall confidence looks fine.
      3. overall extraction_confidence below the configured threshold.
    """
    threshold = settings.EXTRACTION_CONFIDENCE_THRESHOLD if min_confidence is None else min_confidence
    concerns: list[str] = []

    flagged_critical = _CRITICAL_LOW_CONFIDENCE_FIELDS.intersection(payload.low_confidence_fields)
    if flagged_critical:
        concerns.append(
            f"model flagged low confidence on critical field(s): {', '.join(sorted(flagged_critical))}"
        )

    if payload.extraction_confidence is not None and payload.extraction_confidence < threshold:
        concerns.append(
            f"extraction_confidence {payload.extraction_confidence:.2f} is below the "
            f"required threshold {threshold:.2f}"
        )

    net = payload.net_amount
    grand = payload.grand_total
    gst_pct = payload.gst_percent or 0.0
    if net is not None and grand is not None:
        expected_grand = round(net * (1 + gst_pct / 100), 2)
        if abs(expected_grand - grand) > _MONEY_TOLERANCE:
            concerns.append(
                f"money mismatch: grand_total {grand:.2f} does not match net_amount {net:.2f} "
                f"+ {gst_pct:g}% GST (expected {expected_grand:.2f}) — a line figure was likely misread"
            )

    return concerns


def _wo_po_suffix(wo_po_number: str) -> str:
    """The digits after 'WO-PO/'. Returns the whole string if no slash present."""
    return wo_po_number.split("/", 1)[1] if "/" in wo_po_number else wo_po_number


# Configurable so the client can tweak wording without touching logic. The town council is taken
# from the WO (real samples are Sengkang TC, not Jalan Besar) rather than hardcoded.
# TODO(human): confirm exact remarks template + wording with client (and the fallback council name).
REMARKS_TEMPLATE = (
    "{town_council} – {service_location}. Remarks: {service_location}. "
    "Job done on {job_date}. {nature_of_work}. Job Sheet: {job_sheet_number}. "
    "WO-PO/{wo_po_suffix}."
)

# Used only if the WO header didn't yield a town council name.
DEFAULT_TOWN_COUNCIL = "Town Council"


def build_remarks(payload: WOPayload, *, template: str = REMARKS_TEMPLATE) -> str:
    """Build the Synergix remarks string. Pure function — testable in isolation."""
    return template.format(
        town_council=payload.town_council.strip() or DEFAULT_TOWN_COUNCIL,
        service_location=payload.service_location,
        job_date=payload.job_date.strftime("%d/%m/%Y"),
        nature_of_work=payload.nature_of_work,
        job_sheet_number=payload.job_sheet_number,
        wo_po_suffix=_wo_po_suffix(payload.wo_po_number),
    )
