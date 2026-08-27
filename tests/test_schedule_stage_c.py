"""Regression coverage for Stage C (Schedule Board) and its wiring into write().

Discovered and automated live (2026-08-25) after finding the real click sequence by watching the
user do it by hand -- see docs/synergix_workflow.md ("Stage C completed end-to-end") for the full
narrative: selecting the order row first, the Employee-vs-Work-Team filter distinction, the
invisible newEventButton overlay, the time-fields-reset-on-refresh quirk, and the two separate
submit actions (Event Details popup checkmark, then a second Order Details "Submit" bar).

These tests cover write()'s branching around _schedule_stage_c: a failed/skipped schedule is
reported as PARTIAL (a human must finish Stage C manually), not silently PROCESSED, since a real
submitted quotation + confirmed Variation Order must never be reported as fully done just because
the WO-level exception handler didn't fire.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

from src.models import LineItem, WOPayload, WOStatus
from src.synergix_driver import SynergixDriver


def _payload() -> WOPayload:
    return WOPayload(
        wo_po_number="WO-PO/000076625",
        town_council="JALAN BESAR TOWN COUNCIL",
        job_sheet_number="A25-01104",
        service_location="215B Bidadari Park Drive",
        nature_of_work="Misting",
        job_date=date(2026, 8, 21),
        prepared_by="Test Officer",
        gl_number="431-XX-XXXX",
        quantity=1.0,
        unit_price=44.0,
        line_items=[LineItem(quantity=1.0, unit_price=44.0, net_amount=44.0)],
        net_amount=44.0,
        grand_total=47.96,
        source_path="test.pdf",
    )


def _driver_ready_for_stage_c(quo_id: str = "QUO0006749") -> SynergixDriver:
    d = SynergixDriver()
    d.page = AsyncMock()
    d.login = AsyncMock(return_value=None)
    d._stage_b_create_quotation = AsyncMock(return_value=None)
    d._assert_details_filled = AsyncMock(return_value=None)
    d._submit_quotation = AsyncMock(return_value=(quo_id, False))
    d._confirm_variation_order = AsyncMock(return_value=True)
    d._back_to_home = AsyncMock(return_value=None)
    return d


def test_dry_run_skips_schedule_stage_c_entirely():
    d = SynergixDriver()
    d.page = AsyncMock()
    with patch("src.synergix_driver.settings.DRY_RUN", True):
        result = asyncio.run(d._schedule_stage_c(_payload()))
    assert result is True


def test_write_reports_processed_when_stage_c_succeeds():
    d = _driver_ready_for_stage_c()
    d._schedule_stage_c = AsyncMock(return_value=True)
    d._fulfil_stage_d = AsyncMock(return_value=True)  # Stage D is tested separately
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PROCESSED
    assert "Fulfil (Stage D) submitted" in result.detail
    d._schedule_stage_c.assert_awaited_once()


def test_write_reports_partial_when_stage_c_fails():
    """A failed Stage C schedule must NOT be reported as fully PROCESSED -- the quotation and
    Variation Order are both real, but without a schedule Stage D has nothing to fulfil."""
    d = _driver_ready_for_stage_c()
    d._schedule_stage_c = AsyncMock(return_value=False)
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PARTIAL
    assert "Stage C" in result.detail
    assert "QUO0006749" in result.detail


def test_write_reports_partial_when_stage_c_raises():
    """An exception inside Stage C (e.g. a selector timeout) must be swallowed into a PARTIAL
    result, not propagate and lose the fact that Stage B + B.5 both succeeded."""
    d = _driver_ready_for_stage_c()
    d._schedule_stage_c = AsyncMock(side_effect=RuntimeError("selector timeout"))
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PARTIAL
    assert "QUO0006749" in result.detail
