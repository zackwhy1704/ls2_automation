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
    DUPLICATE = "duplicate"          # already in Synergix
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"          # successfully written to Synergix
    PARTIAL = "partial"              # written but a fragile sub-step (e.g. schedule board) needs manual action
    FAILED = "failed"                # error during Synergix write


# --- Project code mapping (resolved from job_sheet_number prefix) ---
# TODO(human): confirm these two project codes + the prefix rule against real WO samples.
PROJECT_CODE_INFIGO = "2000069"     # job_sheet_number starts with a letter
PROJECT_CODE_ECOCARE = "2000050"    # job_sheet_number starts with a digit


class WOPayload(BaseModel):
    wo_po_number: str                # format WO-PO/XXXXXXXXX
    job_sheet_number: str            # drives project code
    service_location: str
    nature_of_work: str
    job_date: date
    prepared_by: str                 # becomes customer contact
    gl_number: str                   # goes into Reference No.
    quantity: float
    unit_price: float
    # The file the extractor read AND the file Synergix attaches in stage D. For the email flow this
    # is the WO PDF attachment when present, otherwise the saved .eml. (Named source_path rather than
    # pdf_path because it is not always a PDF.)
    source_path: str
    # Optional[...] (not `float | None`) so the model also evaluates on Python 3.9 dev machines;
    # pydantic eagerly evaluates field annotations, unlike the deferred `from __future__ import annotations`.
    extraction_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 0..1 from Haiku
