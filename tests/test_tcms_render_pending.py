"""WORowNeverRenderedError -> WOStatus.TCMS_RENDER_PENDING mapping in self_process_one.

Regression coverage for a real, live-confirmed finding: some WOs that list_uninvoiced() genuinely
sees can never be made selectable — TCMS's own FixedDataTable grid gives that row a permanently
zero-height cell layout, reproduced identically across 3 independent fresh sessions (see
WORowNeverRenderedError's docstring). This must be reported distinctly from FAILED (a code/pipeline
error) since it's expected to clear on its own on a later run, not something to debug.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock

from src.batch import BatchResult, self_process_one
from src.models import WOStatus
from src.synergix_driver import DedupResult
from src.tcms_scraper import WORowNeverRenderedError


def test_row_never_rendered_maps_to_tcms_render_pending(monkeypatch):
    wo_id = "WO-PO/000076820"

    scraper = AsyncMock()
    scraper.download_pdf.side_effect = WORowNeverRenderedError(wo_id)

    synergix = AsyncMock()
    synergix.check_duplicate.return_value = DedupResult.NOT_DUPLICATE

    result = BatchResult()
    outcome = asyncio.run(self_process_one(wo_id, scraper, synergix, result))

    assert outcome.status == WOStatus.TCMS_RENDER_PENDING
    assert wo_id in outcome.detail
    assert "rendering defect" in outcome.detail or "render" in outcome.detail.lower()


def test_tcms_render_pending_excluded_from_success_rate_denominator():
    from src.batch import WOOutcome
    from src.report import _quotation_summary

    result = BatchResult(outcomes=[
        WOOutcome("WO-PO/000000001", WOStatus.TCMS_RENDER_PENDING, "row never rendered"),
        WOOutcome("WO-PO/000000002", WOStatus.PROCESSED, "submitted", quotation_id="QUO123"),
    ])

    generated, attempted, rate = _quotation_summary(result)

    assert generated == 1
    assert attempted == 1  # the render-pending WO must NOT count as an attempt
    assert rate == 100.0
