"""Typed data model for a Work Order payload and its lifecycle status."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WOStatus(str, Enum):
    SCRAPED = "scraped"              # WO source obtained (scraped from TCMS, or ingested from email)
    INGESTED = "ingested"            # alias used by the email flow; same meaning as SCRAPED
    EXTRACTED = "extracted"
    INVALID = "invalid"              # failed validation
    DUPLICATE = "duplicate"          # already invoiced in Synergix — must NOT be billed again
    NEEDS_REVIEW = "needs_review"    # dedup check was inconclusive/errored — a human must verify before billing
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"          # successfully written to Synergix
    PARTIAL = "partial"              # written but a fragile sub-step (e.g. schedule board) needs manual action
    FAILED = "failed"                # error during Synergix write


# --- Project code mapping (resolved from job_sheet_number prefix) ---
# TODO(human): confirm these two project codes + the prefix rule against real WO samples.
#
# TODO(human) 2026-08-01: these two codes are confirmed to be JBTC-ONLY. Poking Synergix's live
# Project Site picker for SKTC (see project memory synergix-no-copyfrom-path) surfaced that Sengkang
# has its OWN, completely different project codes — 2000073 (Pest control) and 2000130 (Mosquito) —
# that don't fit this alphabetic/numeric job-sheet-prefix scheme at all; they look keyed by service
# type instead. resolve_project_code() below is only verified correct for JBTC. Do NOT assume it
# applies to SKTC WOs without confirming with the client which real SKTC WOs map to which of the two
# codes, and whether the prefix rule means anything for SKTC at all.
PROJECT_CODE_INFIGO = "2000069"     # job_sheet_number starts with a letter
PROJECT_CODE_ECOCARE = "2000050"    # job_sheet_number starts with a digit


def is_jbtc(town_council: str) -> bool:
    """True for a Jalan Besar Town Council WO, false otherwise (incl. Sengkang/SKTC).

    Confirmed against 10 real labelled JBTC samples and 10 real SKTC samples (2026-08-01, via
    scripts/measure_extraction.py): JBTC's printed "Job Cost" (the pre-GST base) is
    Gross + |Discount Amt| — i.e. the WO's own discount COLUMN is added, not subtracted, from gross.
    SKTC's is the intuitive Gross - Discount Amt. Both councils' WOs print "Discount Amt" identically,
    so this council split is the only signal that distinguishes which arithmetic applies; get it
    wrong and JBTC WOs bill low or SKTC WOs bill high. See project memory synergix-money-formula.
    """
    return "jalan besar" in (town_council or "").strip().lower()


class WOPayload(BaseModel):
    wo_po_number: str                # format WO-PO/XXXXXXXXX
    town_council: str = ""           # e.g. "Sengkang Town Council" — from the WO header, drives remarks
    job_sheet_number: str            # drives project code
    service_location: str
    nature_of_work: str
    job_date: date
    prepared_by: str                 # becomes customer contact
    gl_number: str                   # goes into Reference No. — the "731-AN-..." alphanumeric G/L code
    quantity: float
    unit_price: float                # gross Rate per unit (e.g. $30.00 on the WO)

    # Full pricing breakdown from the WO. Captured so the Synergix line mapping can decide which
    # figure to use; unit_price above stays the gross Rate for backward compatibility.
    # TODO(human): confirm which amount Synergix expects on the line (gross vs net-after-discount).
    discount_percent: Optional[float] = None   # e.g. 10.0 for "10.00%"
    discount_amount: Optional[float] = None     # e.g. 3.00
    net_amount: Optional[float] = None          # gross less discount, before GST (e.g. 27.00)
    gst_percent: Optional[float] = None         # e.g. 9.0
    grand_total: Optional[float] = None         # net + GST (e.g. 29.43)

    sr_number: Optional[str] = None             # the "SR"/Schedule reference seen in subject/body, if any

    # The file the extractor read AND the file Synergix attaches in stage D. For the email flow this
    # is the WO PDF attachment when present, otherwise the saved source email. (Named source_path
    # rather than pdf_path because it is not always a PDF.)
    source_path: str
    # Optional[...] (not `float | None`) so the model also evaluates on Python 3.9 dev machines;
    # pydantic eagerly evaluates field annotations, unlike the deferred `from __future__ import annotations`.
    extraction_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 0..1 from Haiku
    low_confidence_fields: list[str] = Field(default_factory=list)  # field names Haiku itself flagged as unsure
