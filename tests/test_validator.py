"""Unit tests for validation, project-code resolution, and remarks formatting."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import PROJECT_CODE_ECOCARE, PROJECT_CODE_INFIGO, WOPayload
from src.validator import (
    ValidationError,
    build_remarks,
    resolve_project_code,
    validate,
)


def make_payload(**overrides) -> WOPayload:
    base = dict(
        wo_po_number="WO-PO/123456789",
        job_sheet_number="A1023",
        service_location="Block 123 Jalan Besar",
        nature_of_work="Cockroach treatment",
        job_date=date(2026, 1, 15),
        prepared_by="Jane Tan",
        gl_number="GL-5500",
        quantity=1.0,
        unit_price=120.0,
        pdf_path="data/pdfs/WO-PO-123456789.pdf",
    )
    base.update(overrides)
    return WOPayload(**base)


# --- project code resolution ---

def test_project_code_letter_prefix_is_infigo():
    assert resolve_project_code("A1023") == PROJECT_CODE_INFIGO


def test_project_code_digit_prefix_is_ecocare():
    assert resolve_project_code("9920") == PROJECT_CODE_ECOCARE


def test_project_code_empty_raises():
    with pytest.raises(ValidationError):
        resolve_project_code("")


def test_project_code_symbol_prefix_raises():
    with pytest.raises(ValidationError):
        resolve_project_code("#abc")


# --- validation ---

def test_valid_payload_has_no_errors():
    assert validate(make_payload()) == []


def test_bad_wo_po_number_flagged():
    errors = validate(make_payload(wo_po_number="WOPO123"))
    assert any("wo_po_number" in e for e in errors)


def test_missing_gl_number_flagged():
    errors = validate(make_payload(gl_number="  "))
    assert any("gl_number" in e for e in errors)


def test_future_job_date_flagged():
    future = date.today() + timedelta(days=5)
    errors = validate(make_payload(job_date=future))
    assert any("future" in e for e in errors)


def test_zero_unit_price_flagged():
    errors = validate(make_payload(unit_price=0))
    assert any("unit_price" in e for e in errors)


def test_missing_prepared_by_flagged():
    errors = validate(make_payload(prepared_by=""))
    assert any("prepared_by" in e for e in errors)


def test_unresolvable_project_code_flagged():
    errors = validate(make_payload(job_sheet_number="@bad"))
    assert any("prefix" in e for e in errors)


# --- remarks builder ---

def test_remarks_format():
    remarks = build_remarks(make_payload())
    assert "Jalan Besar Town Council – Block 123 Jalan Besar." in remarks
    assert "Remarks: Block 123 Jalan Besar." in remarks
    assert "Job done on 15/01/2026." in remarks
    assert "Cockroach treatment." in remarks
    assert "Job Sheet: A1023." in remarks
    assert "WO-PO/123456789." in remarks


def test_remarks_uses_wo_po_suffix_only():
    remarks = build_remarks(make_payload(wo_po_number="WO-PO/987654321"))
    assert "WO-PO/987654321." in remarks
    # The 'WO-PO/' prefix from the source number must not appear doubled.
    assert "WO-PO/WO-PO" not in remarks
