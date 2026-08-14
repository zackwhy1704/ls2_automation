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


class LineItem(BaseModel):
    """One row of a WO's "Description of Work" table. Most WOs have exactly one; some (e.g. a
    combined inspection + transport charge) have several, each under its own "Job Sheet:" block but
    sharing the same job_sheet_number — see WOPayload.line_items.
    """

    description: str = ""            # the line's work description, for reference only
    quantity: float
    unit_price: float                # gross Rate per unit for THIS line (e.g. $30.00)
    discount_percent: Optional[float] = None   # e.g. 10.0 for "10.00%"
    discount_amount: Optional[float] = None     # e.g. 3.00
    net_amount: Optional[float] = None          # this line's Job Cost (gross adjusted for discount)


class WOPayload(BaseModel):
    wo_po_number: str                # format WO-PO/XXXXXXXXX
    town_council: str = ""           # e.g. "Sengkang Town Council" — from the WO header, drives remarks
    job_sheet_number: str            # drives project code
    service_location: str
    nature_of_work: str
    job_date: date
    prepared_by: str                 # becomes customer contact
    gl_number: str                   # goes into Reference No. — the "731-AN-..." alphanumeric G/L code
    quantity: float                  # first line item's quantity — kept for validate()/remarks/legacy display
    unit_price: float                # first line item's gross Rate — same reason

    # Every line item on the WO (usually just one). Synergix needs one Details-grid row per entry;
    # net_amount below is the SUM across all of them, which is what must reconcile with grand_total —
    # comparing grand_total against only the first line's net_amount is exactly what caused every
    # multi-line WO to trip the money-consistency trust gate as a false "misread" (confirmed against
    # 21/21 real flagged WOs, 2026-08-07: every one reconciled once ALL lines were summed).
    line_items: list[LineItem] = Field(default_factory=list)

    # Full pricing breakdown from the WO. discount_percent/discount_amount are the first line's, kept
    # for backward compatibility; net_amount is the SUM of every line_items entry (pre-GST, before
    # discount is what "gross" means — net_amount is gross adjusted for discount).
    # TODO(human): confirm which amount Synergix expects on the line (gross vs net-after-discount).
    discount_percent: Optional[float] = None   # e.g. 10.0 for "10.00%"
    discount_amount: Optional[float] = None     # e.g. 3.00
    net_amount: Optional[float] = None          # SUM of all line items' net Job Cost, before GST
    gst_percent: Optional[float] = None         # e.g. 9.0
    grand_total: Optional[float] = None         # net + GST (e.g. 29.43)

    sr_number: Optional[str] = None             # the "SR"/Schedule reference seen in subject/body, if any

    # The TCMS WO's "Property officer" — the Town Council staff member who raised the WO. Not on the
    # printed PDF (extract_from_pdf can't get it); scraped separately from the TCMS WO detail page
    # (TCMSScraper.download_pdf) and set on the payload afterward for the JBTC/TCMS flow only — the
    # SKTC/email flow has no TCMS page to scrape, so this stays unset there. Used for Synergix's
    # Customer Contact field, which otherwise falls back to a generic default.
    property_officer: Optional[str] = None

    # The file the extractor read AND the file Synergix attaches in stage D. For the email flow this
    # is the WO PDF attachment when present, otherwise the saved source email. (Named source_path
    # rather than pdf_path because it is not always a PDF.)
    source_path: str
    # Optional[...] (not `float | None`) so the model also evaluates on Python 3.9 dev machines;
    # pydantic eagerly evaluates field annotations, unlike the deferred `from __future__ import annotations`.
    extraction_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 0..1 from Haiku
    low_confidence_fields: list[str] = Field(default_factory=list)  # field names Haiku itself flagged as unsure

    @property
    def effective_line_items(self) -> list[LineItem]:
        """line_items if the extractor populated it, else a single line synthesized from the
        top-level quantity/unit_price/discount fields — so a payload built directly (e.g. in tests,
        or anything predating the multi-line-item extractor change) still yields exactly one Synergix
        Details row, matching the old behavior.
        """
        if self.line_items:
            return self.line_items
        return [LineItem(
            quantity=self.quantity,
            unit_price=self.unit_price,
            discount_percent=self.discount_percent,
            discount_amount=self.discount_amount,
            net_amount=self.net_amount,
        )]
