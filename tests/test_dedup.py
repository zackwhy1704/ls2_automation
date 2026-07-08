"""Tests for the double-invoice guard: the three-state Synergix duplicate check.

The JBTC "Un-Invoiced WO" list is hand-maintained and can be stale, so Synergix is the source of
truth for whether a WO is already billed. These tests lock in the fail-safe contract: when we cannot
confirm a WO is un-invoiced, the result is UNCERTAIN (not safe to bill) — never a silent proceed.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from config import settings
from src.models import WOPayload
from src.synergix_driver import DedupResult, SynergixDriver


def _payload() -> WOPayload:
    return WOPayload(
        wo_po_number="WO-PO/000065531",
        job_sheet_number="23590",
        service_location="Blk 1",
        nature_of_work="pest",
        job_date=date(2026, 1, 1),
        prepared_by="A",
        gl_number="431-KK-KKR9P1-050531-0-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        source_path="x.pdf",
    )


def test_safe_to_bill_only_for_not_duplicate():
    assert DedupResult.NOT_DUPLICATE.safe_to_bill is True
    assert DedupResult.DUPLICATE.safe_to_bill is False
    assert DedupResult.UNCERTAIN.safe_to_bill is False


def test_stub_defaults_to_uncertain(monkeypatch):
    # No Synergix configured + fail-safe default -> cannot verify -> UNCERTAIN.
    monkeypatch.setattr(settings, "SYNERGIX_BASE_URL", "")
    monkeypatch.setattr(settings, "DEDUP_STUB_ASSUME_SAFE", False)
    result = asyncio.run(SynergixDriver().check_duplicate(_payload()))
    assert result is DedupResult.UNCERTAIN
    assert not result.safe_to_bill


def test_stub_assume_safe_flag_allows_local_demo(monkeypatch):
    # DEV escape hatch: explicitly opt in to treat stub as NOT_DUPLICATE for local approval demos.
    monkeypatch.setattr(settings, "SYNERGIX_BASE_URL", "")
    monkeypatch.setattr(settings, "DEDUP_STUB_ASSUME_SAFE", True)
    result = asyncio.run(SynergixDriver().check_duplicate(_payload()))
    assert result is DedupResult.NOT_DUPLICATE
    assert result.safe_to_bill


@pytest.mark.parametrize("key,expected", [("wo_po", "WO-PO/000065531"), ("job_sheet", "23590")])
def test_dedup_search_value_follows_config(monkeypatch, key, expected):
    monkeypatch.setattr(settings, "SYNERGIX_DEDUP_KEY", key)
    assert SynergixDriver()._dedup_search_value(_payload()) == expected
