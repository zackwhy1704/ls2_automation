"""Regression coverage for _abort_blank_draft's "is it actually blank?" guard.

Found live in the 2026-08-20 overnight sweep: WO-PO/000081588 failed before Customer was set, and
the cleanup aborted QUO0006710 — a fully-filled, gate-verified draft belonging to
WO-PO/000080420, created 47 minutes earlier. The '+' click had not put a new id on the form, so
_current_quotation_id() returned the previous record's id and the cleanup deleted the wrong thing
(while WO-PO/000081588's own blank shell, QUO0006711, was left orphaned).

Two guards now stand between a stale id and a deleted record:
  1. _stage_b_create_quotation refuses to treat an id matching the pre-click one as this WO's draft.
  2. _abort_blank_draft verifies the target is genuinely blank before aborting — the guard that
     holds no matter how the id was obtained. These tests cover (2).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from src.synergix_driver import SynergixDriver


def _driver(subject: str, total: float | None) -> SynergixDriver:
    d = SynergixDriver()
    d.page = AsyncMock()
    d.page.wait_for_timeout = AsyncMock(return_value=None)
    d._read_labeled_value = AsyncMock(return_value=subject)
    d._read_total_after_tax = AsyncMock(return_value=total)
    d.abort_quotation = AsyncMock(return_value=True)
    return d


def test_refuses_to_abort_a_draft_that_has_a_subject():
    """The exact live failure: the on-screen record belongs to another WO, so it has a Subject."""
    d = _driver("WO-PO/000080420 - Jalan Besar Town Council", 88.0)
    asyncio.run(d._abort_blank_draft("QUO0006710", "WO-PO/000081588"))
    d.abort_quotation.assert_not_called()


def test_refuses_to_abort_a_draft_with_a_positive_total():
    """Belt and braces: even with no Subject read, real money on the record means it is not blank."""
    d = _driver("", 209.0)
    asyncio.run(d._abort_blank_draft("QUO0006709", "WO-PO/000081588"))
    d.abort_quotation.assert_not_called()


def test_aborts_a_genuinely_blank_shell():
    """The case the cleanup exists for — no subject, no total — must still be cleaned up, otherwise
    the fix would trade one leak (wrong deletions) for another (orphans masking WOs from dedup)."""
    d = _driver("", 0.0)
    asyncio.run(d._abort_blank_draft("QUO0006711", "WO-PO/000081588"))
    d.abort_quotation.assert_called_once_with("QUO0006711")


def test_no_id_means_nothing_is_aborted():
    d = _driver("", 0.0)
    asyncio.run(d._abort_blank_draft(None, "WO-PO/000081588"))
    d.abort_quotation.assert_not_called()
