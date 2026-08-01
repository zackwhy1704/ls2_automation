"""Tests for the council-specific net_amount/discount arithmetic.

Confirmed 2026-08-01 by running scripts/measure_extraction.py against all 20 real labelled samples:
JBTC's printed pre-GST base ("Job Cost") is Gross + |Discount Amt|; SKTC's is Gross - |Discount Amt|.
Getting the sign backwards silently bills JBTC WOs too low (or trips the trust gate on every JBTC
WO with a discount) — see project memory synergix-money-formula for the full investigation.

_finalize is pure JSON-in/WOPayload-out (the anthropic import is lazy, inside _build_client, not at
module level), so these run without an API key or network access.
"""
from __future__ import annotations

import json

from src.extractor import _finalize
from src.models import is_jbtc


def _raw(**overrides) -> str:
    base = dict(
        wo_po_number="WO-PO/000012345",
        town_council="Jalan Besar Town Council",
        job_sheet_number="A1023",
        service_location="Blk 1",
        nature_of_work="pest control",
        job_date="2026-01-15",
        prepared_by="Jane Tan",
        gl_number="731-AN-ANVZCRes-542353-9-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        discount_percent=10.0,
        discount_amount=3.0,
        net_amount=999.0,      # deliberately wrong — must be overridden, never trusted from the model
        gst_percent=9.0,
        grand_total=35.97,
        sr_number="",
        confidence=0.95,
        low_confidence_fields=[],
    )
    base.update(overrides)
    return json.dumps(base)


def test_is_jbtc_matches_jalan_besar_case_insensitively():
    assert is_jbtc("Jalan Besar Town Council")
    assert is_jbtc("JALAN BESAR TOWN COUNCIL")
    assert is_jbtc("  jalan besar town council  ")


def test_is_jbtc_false_for_sengkang_and_blank():
    assert not is_jbtc("Sengkang Town Council")
    assert not is_jbtc("")
    assert not is_jbtc(None)  # type: ignore[arg-type]


def test_jbtc_net_amount_adds_discount():
    # Real sample pattern: gross 30.00, discount_amount 3.00 -> Job Cost 33.00 (added, not subtracted)
    payload = _finalize(_raw(town_council="Jalan Besar Town Council"), source_path="x.pdf")
    assert payload.net_amount == 33.0


def test_sktc_net_amount_subtracts_discount():
    payload = _finalize(
        _raw(town_council="Sengkang Town Council", grand_total=29.43), source_path="x.pdf",
    )
    assert payload.net_amount == 27.0


def test_jbtc_net_amount_via_discount_percent_only():
    payload = _finalize(
        _raw(town_council="Jalan Besar Town Council", discount_amount=0, discount_percent=10.0),
        source_path="x.pdf",
    )
    # gross 30 * (1 + 10%) = 33.0, matching the discount_amount-based path
    assert payload.net_amount == 33.0


def test_sktc_net_amount_via_discount_percent_only():
    payload = _finalize(
        _raw(town_council="Sengkang Town Council", discount_amount=0, discount_percent=10.0),
        source_path="x.pdf",
    )
    assert payload.net_amount == 27.0


def test_no_discount_same_for_both_councils():
    for council in ("Jalan Besar Town Council", "Sengkang Town Council"):
        payload = _finalize(
            _raw(town_council=council, discount_amount=0, discount_percent=0), source_path="x.pdf",
        )
        assert payload.net_amount == 30.0
