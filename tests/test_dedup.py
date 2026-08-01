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


# --- finding 7: WO-PO must survive the 50-char subject truncation (pure logic, always runs) ---

def test_wo_po_survives_subject_truncation():
    """dedup searches the Enquiry/Subject column for the WO-PO, so the WO-PO MUST remain intact in
    the (<=50 char) subject even for a long council name. It's front-anchored, so [:50] only trims the
    council tail — verified live 2026-08-01 (see project memory synergix-dedup-verified). This test
    locks that in so a future reformat can't silently break dedup."""
    p = _payload()
    p.town_council = "Some Extremely Long Town Council Name That Exceeds Fifty Characters Easily"
    subject = SynergixDriver()._subject(p)
    assert len(subject) <= 50
    assert p.wo_po_number in subject, "WO-PO was truncated out of the subject — dedup would miss it"


# --- finding 6: live round-trip against the REAL Synergix grid (opt-in) ---
# Exercises actual nav + column filter + grid read — the control that prevents double-billing.
# Already verified once manually (see project memory synergix-dedup-verified). These make that
# verification repeatable: run deliberately, with a Synergix login configured in .env and two known
# WO-POs supplied:
#     RUN_SYNERGIX_TESTS=1 \
#     SYNERGIX_KNOWN_DUPLICATE_WO_PO=WO-PO/000060068 \
#     SYNERGIX_KNOWN_ABSENT_WO_PO=WO-PO/000000000 \
#     pytest tests/test_dedup.py -k roundtrip -v

import os

_LIVE = pytest.mark.skipif(
    os.getenv("RUN_SYNERGIX_TESTS") != "1",
    reason="live Synergix round-trip; set RUN_SYNERGIX_TESTS=1 (+ known WO-POs in env) to run",
)


def _live_check(wo_po: str) -> DedupResult:
    async def _run() -> DedupResult:
        drv = SynergixDriver()
        await drv.start()
        try:
            p = _payload()
            p.wo_po_number = wo_po
            return await drv.check_duplicate(p)
        finally:
            await drv.close()
    return asyncio.run(_run())


@_LIVE
def test_roundtrip_known_duplicate_reads_as_duplicate():
    wo = os.getenv("SYNERGIX_KNOWN_DUPLICATE_WO_PO")
    if not wo:
        pytest.skip("set SYNERGIX_KNOWN_DUPLICATE_WO_PO to a WO-PO already invoiced in Synergix")
    assert _live_check(wo) is DedupResult.DUPLICATE, (
        f"{wo} is known-invoiced but the checker did not return DUPLICATE — double-billing risk"
    )


@_LIVE
def test_roundtrip_known_absent_reads_as_not_duplicate():
    wo = os.getenv("SYNERGIX_KNOWN_ABSENT_WO_PO", "WO-PO/000000000")
    result = _live_check(wo)
    # NOT_DUPLICATE is the pass. UNCERTAIN is a soft-fail worth surfacing (grid/marker changed), but
    # it is fail-safe (won't auto-bill), so we assert it's specifically NOT a false DUPLICATE and
    # flag UNCERTAIN loudly rather than passing silently.
    assert result is not DedupResult.DUPLICATE, f"absent WO {wo} wrongly read as DUPLICATE"
    if result is DedupResult.UNCERTAIN:
        pytest.fail(f"absent WO {wo} read as UNCERTAIN — 'no records' marker likely changed; "
                    "dedup can't positively confirm NOT_DUPLICATE, so everything will route to review")
