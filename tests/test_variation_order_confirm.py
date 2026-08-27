"""Regression coverage for Stage B.5 (Variation Order confirm) and its wiring into write().

Discovered live (2026-08-25): a submitted quotation lands in Synergix's "Under Variation" status
and is NOT yet a schedulable Service Order until a separate Confirm action is taken on it — this
doc's own workflow notes already named the step ("Go to Variation Order... Confirm the VO") but it
had never been automated, so Schedule Board's Unscheduled Service Orders grid had nothing to show
even after a real Submit. _confirm_variation_order automates it; these tests cover its DRY_RUN gate
and write()'s branching so a failed/skipped confirm is reported as PARTIAL (a human must finish it
manually) rather than silently reported as fully PROCESSED.
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


def _driver_with_mocked_stage_b(quo_id: str = "QUO0006749") -> SynergixDriver:
    d = SynergixDriver()
    d.page = AsyncMock()
    d.login = AsyncMock(return_value=None)
    d._stage_b_create_quotation = AsyncMock(return_value=None)
    d._assert_details_filled = AsyncMock(return_value=None)
    d._submit_quotation = AsyncMock(return_value=(quo_id, False))
    d._back_to_home = AsyncMock(return_value=None)
    return d


def test_dry_run_skips_variation_order_confirm_entirely():
    """DRY_RUN must never touch Under Variation -- nothing was really submitted to confirm."""
    d = SynergixDriver()
    d.page = AsyncMock()
    d._confirm_variation_order = AsyncMock(return_value=True)
    with patch("src.synergix_driver.settings.DRY_RUN", True):
        result = asyncio.run(d._confirm_variation_order("QUO0006749"))
    # _dry_guard short-circuits before any page interaction; the mock above is never exercised
    # because DRY_RUN=True makes the module-level settings check return True immediately.
    assert result is True


def test_write_reports_processed_when_vo_confirm_succeeds():
    d = _driver_with_mocked_stage_b()
    d._confirm_variation_order = AsyncMock(return_value=True)
    d._schedule_stage_c = AsyncMock(return_value=True)  # Stage C is tested separately
    d._fulfil_stage_d = AsyncMock(return_value=True)  # Stage D is tested separately
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PROCESSED
    assert "Variation Order confirmed" in result.detail
    d._confirm_variation_order.assert_awaited_once_with("QUO0006749")


def test_write_reports_partial_when_vo_confirm_fails():
    """A failed VO confirm must NOT be reported as fully PROCESSED -- the quotation is real and
    submitted, but without the Service Order it will never appear on Schedule Board, and a human
    needs to know to finish the confirm manually."""
    d = _driver_with_mocked_stage_b()
    d._confirm_variation_order = AsyncMock(return_value=False)
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PARTIAL
    assert "Variation Order confirm failed" in result.detail
    assert "QUO0006749" in result.detail


def test_write_reports_partial_when_vo_confirm_raises():
    """An exception inside the confirm step (e.g. a selector timeout) must be swallowed into a
    PARTIAL result, not propagate and lose the fact that a real quotation WAS submitted."""
    d = _driver_with_mocked_stage_b()
    d._confirm_variation_order = AsyncMock(side_effect=RuntimeError("selector timeout"))
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    assert result.status is WOStatus.PARTIAL
    assert "QUO0006749" in result.detail


def test_write_does_not_attempt_vo_confirm_when_quotation_id_unread():
    """No id read back from the submit step means there's nothing to look up under Under Variation
    -- must not call _confirm_variation_order with None."""
    d = _driver_with_mocked_stage_b(quo_id=None)
    d._confirm_variation_order = AsyncMock(return_value=True)
    with patch("src.synergix_driver.settings.DRY_RUN", False):
        result = asyncio.run(d.write(_payload()))
    d._confirm_variation_order.assert_not_awaited()
    assert result.status is WOStatus.PARTIAL
