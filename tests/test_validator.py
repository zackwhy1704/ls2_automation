"""Unit tests for validation, project-code resolution, and remarks formatting."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import PROJECT_CODE_ECOCARE, PROJECT_CODE_INFIGO, WOPayload
from src.validator import (
    ValidationError,
    build_remarks,
    check_extraction_trust,
    resolve_project_code,
    validate,
)


def make_payload(**overrides) -> WOPayload:
    base = dict(
        wo_po_number="WO-PO/123456789",
        town_council="Sengkang Town Council",
        job_sheet_number="A1023",
        service_location="Block 123 Jalan Besar",
        nature_of_work="Cockroach treatment",
        job_date=date(2026, 1, 15),
        prepared_by="Jane Tan",
        gl_number="GL-5500",
        quantity=1.0,
        unit_price=120.0,
        source_path="data/pdfs/WO-PO-123456789.pdf",
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


# --- extraction trust gate (money consistency + confidence; NOT part of validate()) ---

def test_consistent_money_has_no_concerns():
    p = make_payload(net_amount=27.00, gst_percent=9.0, grand_total=29.43)
    assert not any("money mismatch" in c for c in check_extraction_trust(p))


def test_grand_total_mismatch_flagged():
    # net 27.00 + 9% GST should be 29.43, not 294.30 (e.g. a misread decimal point)
    p = make_payload(net_amount=27.00, gst_percent=9.0, grand_total=294.30)
    assert any("money mismatch" in c for c in check_extraction_trust(p))


def test_grand_total_within_rounding_tolerance_passes():
    p = make_payload(net_amount=27.00, gst_percent=9.0, grand_total=29.44)
    assert not any("money mismatch" in c for c in check_extraction_trust(p))


def test_missing_money_fields_skips_check():
    # net_amount/grand_total absent entirely (e.g. no GST on the WO) -> nothing to cross-check
    p = make_payload(net_amount=None, grand_total=None)
    assert not any("money mismatch" in c for c in check_extraction_trust(p))


def test_zero_gst_percent_checked_correctly():
    p = make_payload(net_amount=100.0, gst_percent=0.0, grand_total=100.0)
    assert not any("money mismatch" in c for c in check_extraction_trust(p))


def test_low_confidence_on_critical_field_flagged():
    p = make_payload(low_confidence_fields=["gl_number"])
    concerns = check_extraction_trust(p)
    assert any("low confidence" in c and "gl_number" in c for c in concerns)


def test_low_confidence_on_noncritical_field_not_flagged():
    p = make_payload(low_confidence_fields=["sr_number"])
    assert not any("low confidence" in c for c in check_extraction_trust(p))


def test_overall_confidence_below_threshold_flagged():
    p = make_payload(extraction_confidence=0.5)
    assert any("extraction_confidence" in c for c in check_extraction_trust(p, min_confidence=0.75))


def test_overall_confidence_above_threshold_passes():
    p = make_payload(extraction_confidence=0.99)
    assert not any("extraction_confidence" in c for c in check_extraction_trust(p, min_confidence=0.75))


def test_no_confidence_reported_skips_threshold_check():
    p = make_payload(extraction_confidence=None)
    assert not any("extraction_confidence" in c for c in check_extraction_trust(p, min_confidence=0.75))


def test_trust_gate_passes_clean_high_confidence_wo():
    p = make_payload(extraction_confidence=0.98, quantity=1.0, unit_price=30.0,
                      net_amount=27.0, gst_percent=9.0, grand_total=29.43)
    assert check_extraction_trust(p, min_confidence=0.75) == []


def test_validate_no_longer_includes_trust_concerns():
    # validate() covers malformed/missing WO data only; trust concerns are a separate axis
    # (NEEDS_REVIEW, not INVALID) — a WO with a money mismatch or low confidence must still pass
    # validate() cleanly, since the trust gate is applied separately in the batch pipeline.
    p = make_payload(net_amount=27.00, gst_percent=9.0, grand_total=294.30,
                      extraction_confidence=0.1, low_confidence_fields=["gl_number"])
    assert validate(p) == []


# --- remarks builder ---

def test_remarks_format():
    remarks = build_remarks(make_payload())
    assert "Sengkang Town Council – Block 123 Jalan Besar." in remarks  # council from the WO, not hardcoded
    assert "Remarks: Block 123 Jalan Besar." in remarks
    assert "Job done on 15/01/2026." in remarks
    assert "Cockroach treatment." in remarks
    assert "Job Sheet: A1023." in remarks
    assert "WO-PO/123456789." in remarks


def test_remarks_falls_back_to_default_council_when_blank():
    remarks = build_remarks(make_payload(town_council=""))
    assert "Town Council – Block 123 Jalan Besar." in remarks


def test_remarks_uses_wo_po_suffix_only():
    remarks = build_remarks(make_payload(wo_po_number="WO-PO/987654321"))
    assert "WO-PO/987654321." in remarks
    # The 'WO-PO/' prefix from the source number must not appear doubled.
    assert "WO-PO/WO-PO" not in remarks
