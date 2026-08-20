"""Regression coverage for the Details-grid flicker-tolerance fix (commit 255e5ef).

Root cause: the grid re-renders itself independently of any write in flight and can transiently
blank an already-correct, untouched cell for several seconds before it self-recovers. The old code
read a single blank poll as a genuine failure and overwrote the cell — a write landing mid-re-render
is a plausible way to actually corrupt a value that would have recovered fine on its own. The fix
requires a value to read back correctly on TWO CONSECUTIVE polls before trusting it, and re-checks a
mismatch once more before acting on it in _verify_and_refill_rows / _force_totals_commit.

These tests exercise that logic in isolation with a scripted fake Locator/page — no real Playwright,
no real timeouts (wait_for_timeout is a no-op here so the suite stays fast).
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.models import LineItem, WOPayload
from src.synergix_driver import SynergixDriver


def _payload(**overrides) -> WOPayload:
    base = dict(
        wo_po_number="WO-PO/000080420",
        job_sheet_number="26315",
        service_location="Blk 1",
        nature_of_work="pest",
        job_date=date(2026, 8, 19),
        prepared_by="A",
        gl_number="431-KY-KYR8P2-382020-0-7211",
        quantity=2.0,
        unit_price=44.0,
        net_amount=88.0,
        grand_total=95.92,
        source_path="x.pdf",
    )
    base.update(overrides)
    return WOPayload(**base)


def _make_driver() -> SynergixDriver:
    """A SynergixDriver with a fake page (no-op wait_for_timeout, so tests run fast) and
    _click_when_clear stubbed out — these tests are about the read/retry logic, not click mechanics,
    which is already exercised by _click_when_clear being called at all."""
    driver = SynergixDriver()
    driver.page = AsyncMock()
    driver.page.wait_for_timeout = AsyncMock(return_value=None)
    driver._click_when_clear = AsyncMock(return_value=None)
    return driver


def _scripted_cell(reads: list[str], value_written_to: list[str]) -> AsyncMock:
    """A fake Locator whose input_value() returns `reads` in order (one per call, repeating the
    last value once exhausted so a test can't accidentally run off the end) and whose fill()
    records what it was called with."""
    cell = AsyncMock()
    remaining = list(reads)

    async def _input_value():
        if remaining:
            return remaining.pop(0)
        return reads[-1] if reads else ""

    async def _fill(value):
        value_written_to.append(value)

    cell.input_value = AsyncMock(side_effect=_input_value)
    cell.fill = AsyncMock(side_effect=_fill)
    return cell


# --- P1: _fill_grid_field ----------------------------------------------------------------------


def test_flicker_then_recovery_is_not_treated_as_failure():
    """[value, "", "", value, value]: a real value, blanks out for a bit, recovers, stable twice
    in a row. Must NOT trigger a second overwrite once it's read back correctly twice."""
    driver = _make_driver()
    written = []
    cell = _scripted_cell(["", "", "value", "value"], written)
    driver._grid_cell_locator = AsyncMock(return_value=cell)

    result = asyncio.run(driver._fill_grid_field(0, "Qty", "value", r"^qty"))

    assert result is True
    # fill() is called once per attempt, at the START of that attempt — the flicker recovering on
    # its own within the poll window must not trigger a SECOND fill() call.
    assert written == ["value"], f"expected exactly one fill() call, got {written}"


def test_genuine_failure_still_caught():
    """A value that never recovers (stays wrong/blank for the whole window) must still eventually
    report failure — the fix must not trade false-positives for false-negatives."""
    driver = _make_driver()
    written = []
    cell = _scripted_cell(["" for _ in range(100)], written)
    driver._grid_cell_locator = AsyncMock(return_value=cell)

    result = asyncio.run(driver._fill_grid_field(0, "Qty", "value", r"^qty", attempts=1))

    assert result is False
    assert written == ["value"]  # it did try


def test_single_matching_read_sandwiched_in_mismatches_is_not_trusted():
    """["", value, ""]: exactly ONE matching read, then reverts again. The old single-read logic
    would have wrongly accepted this on the matching read; the fix requires TWO CONSECUTIVE
    matches, so this single blip must not be trusted, and the attempt must keep polling (and
    ultimately fail, since it never stabilizes)."""
    driver = _make_driver()
    written = []
    # After the single matching read, keep it mismatched for the rest of the poll window so we can
    # observe whether the function incorrectly returned True right after the lone match.
    reads = ["", "value", ""] + ["" for _ in range(40)]
    cell = _scripted_cell(reads, written)
    driver._grid_cell_locator = AsyncMock(return_value=cell)

    result = asyncio.run(driver._fill_grid_field(0, "Qty", "value", r"^qty", attempts=1))

    assert result is False, "a single matching read sandwiched in mismatches must not be trusted"


def test_two_consecutive_matches_are_trusted_immediately():
    """Sanity check on the other side: two genuine consecutive matches right away should return
    True promptly, not force the full window to elapse."""
    driver = _make_driver()
    written = []
    cell = _scripted_cell(["value", "value"], written)
    driver._grid_cell_locator = AsyncMock(return_value=cell)

    result = asyncio.run(driver._fill_grid_field(0, "Qty", "value", r"^qty"))

    assert result is True


# --- P1: _verify_and_refill_rows -----------------------------------------------------------------


def test_verify_and_refill_does_not_refill_a_self_correcting_flicker():
    """A single mismatched read that self-corrects on the recheck must not trigger
    _fill_grid_field at all — no wasted/risky re-fill of a cell that's actually fine."""
    driver = _make_driver()
    line_items = [LineItem(quantity=2.0, unit_price=44.0, net_amount=88.0)]
    remarks = "test remarks"

    # First read of the row shows Qty wrong; the recheck (single-field _read_grid_row call) shows
    # it's actually correct. Item Code/Unit Price/Remarks all match on the very first read so only
    # Qty's mismatch-then-recheck path gets exercised.
    read_calls = {"n": 0}

    async def _read_grid_row(row_index, header_regexes):
        read_calls["n"] += 1
        if header_regexes == ("item code", "^qty", "unit price", "remarks"):
            # The initial full-row read: Qty wrong, everything else already correct.
            return {"item code": "SE-400212A", "^qty": "0.00", "unit price": "44.00", "remarks": remarks}
        if header_regexes == (r"^qty",):
            # The recheck: Qty is actually fine now.
            return {r"^qty": "2.00"}
        raise AssertionError(f"unexpected _read_grid_row call: {header_regexes}")

    driver._read_grid_row = AsyncMock(side_effect=_read_grid_row)
    driver._fill_grid_field = AsyncMock(return_value=True)
    driver._select_item_code = AsyncMock(return_value=True)

    asyncio.run(driver._verify_and_refill_rows(line_items, remarks))

    driver._fill_grid_field.assert_not_called()


def test_verify_and_refill_still_refills_a_confirmed_reversion():
    """A mismatch that's STILL wrong on the recheck must trigger a real re-fill — the recheck
    must not make genuine reversions invisible."""
    driver = _make_driver()
    line_items = [LineItem(quantity=2.0, unit_price=44.0, net_amount=88.0)]
    remarks = "test remarks"

    async def _read_grid_row(row_index, header_regexes):
        if header_regexes == ("item code", "^qty", "unit price", "remarks"):
            return {"item code": "SE-400212A", "^qty": "0.00", "unit price": "44.00", "remarks": remarks}
        if header_regexes == (r"^qty",):
            return {r"^qty": "0.00"}  # still wrong on recheck — a real reversion
        raise AssertionError(f"unexpected _read_grid_row call: {header_regexes}")

    driver._read_grid_row = AsyncMock(side_effect=_read_grid_row)
    driver._fill_grid_field = AsyncMock(return_value=True)
    driver._select_item_code = AsyncMock(return_value=True)

    asyncio.run(driver._verify_and_refill_rows(line_items, remarks))

    # The mock never "fixes" the underlying read, so a confirmed reversion correctly triggers a
    # re-fill on every one of the 3 verify rounds (each round's own read+recheck still sees it
    # wrong) — the point being tested is that it DOES re-fill at all, not that it stops after one.
    driver._fill_grid_field.assert_called_with(0, "Qty", "2.00", r"^qty")
    assert driver._fill_grid_field.call_count == 3


# --- P1: _force_totals_commit --------------------------------------------------------------------


def test_force_totals_commit_does_not_nudge_a_self_correcting_flicker():
    """A total that reads unsettled once, then settles on the recheck, must not trigger a Qty
    nudge — same recheck-before-acting discipline as the other two call sites."""
    driver = _make_driver()
    payload = _payload()
    line_items = [LineItem(quantity=2.0, unit_price=44.0, net_amount=88.0)]

    # _total_is_settled is called repeatedly inside the polling loop (24x @ ~0ms in tests since
    # wait_for_timeout is a no-op), then once more for the recheck. Script it: unsettled for the
    # whole polling loop, THEN settled on the recheck call.
    call_count = {"n": 0}

    async def _total_is_settled(expected):
        call_count["n"] += 1
        if call_count["n"] <= 24:
            return False, 0.0
        return True, 88.0  # the recheck call: settled

    driver._total_is_settled = AsyncMock(side_effect=_total_is_settled)
    driver._fill_grid_field = AsyncMock(return_value=True)

    asyncio.run(driver._force_totals_commit(payload, line_items))

    driver._fill_grid_field.assert_not_called()


def test_force_totals_commit_nudges_qty_when_the_row_already_reads_correct():
    """Total stalled short while every cell in the row already reads its target: the server is
    simply behind, so the Qty nudge (0.00 then the real value) is the right lever."""
    driver = _make_driver()
    payload = _payload()
    line_items = [LineItem(quantity=2.0, unit_price=44.0, net_amount=88.0)]

    async def _total_is_settled(expected):
        return False, 0.0  # never settles, including the recheck and every subsequent round

    driver._total_is_settled = AsyncMock(side_effect=_total_is_settled)
    # The DOM is already correct — nothing to repair, so the nudge is the only option left.
    driver._read_grid_row = AsyncMock(return_value={"^qty": "2.00", "unit price": "44.00"})
    driver._fill_grid_field = AsyncMock(return_value=True)

    asyncio.run(driver._force_totals_commit(payload, line_items))

    calls = driver._fill_grid_field.call_args_list
    assert any(c.args == (0, "Qty", "0.00", r"^qty") for c in calls)
    assert any(c.args == (0, "Qty", "2.00", r"^qty") for c in calls)


def test_force_totals_commit_budget_scales_with_row_count():
    """A 4-row WO must get more repair rounds than a 2-row one.

    Regression guard for the 2026-08-20 finding: roughly one row commits per round, so on the 4-row
    WO-PO/000079836 the total climbed 33.00 -> 77.00 -> 110.00 and was still converging when a
    hardcoded 3-round budget cut it off short of 275.00. Budget must be a function of row count.
    """
    rounds_seen = {}

    for n_rows in (2, 4):
        driver = _make_driver()
        payload = _payload()
        line_items = [LineItem(quantity=1.0, unit_price=33.0, net_amount=33.0)] * n_rows

        async def _total_is_settled(expected):
            return False, 33.0  # never settles, so every available round is consumed

        # DOM always reads correct -> the nudge branch, one pair of fills per row per round.
        driver._total_is_settled = AsyncMock(side_effect=_total_is_settled)
        driver._read_grid_row = AsyncMock(return_value={"^qty": "1.00", "unit price": "33.00"})
        driver._fill_grid_field = AsyncMock(return_value=True)

        asyncio.run(driver._force_totals_commit(payload, line_items))

        # 2 fills (0.00 then the target) per row, per round.
        rounds_seen[n_rows] = len(driver._fill_grid_field.call_args_list) / (2 * n_rows)

    assert rounds_seen[4] > rounds_seen[2], (
        f"4-row WOs must get more rounds than 2-row ones, got {rounds_seen}")
    assert rounds_seen[4] >= 4, (
        f"a 4-row WO needs at least one round per row, got {rounds_seen[4]}")


def test_force_totals_commit_repairs_the_field_that_is_actually_short():
    """Total stalled short because row 1's Unit Price is 0.00: the repair must re-fill THAT field.

    Regression guard for the 2026-08-20 finding — the old version only ever nudged Qty, so on
    WO-PO/000080454 (row 1's price missing) it burned all three rounds writing Qty and never touched
    the value that was actually wrong. A Qty-only nudge must NOT be what happens here.
    """
    driver = _make_driver()
    payload = _payload(net_amount=99.0, grand_total=107.91)
    line_items = [
        LineItem(quantity=1.0, unit_price=44.0, net_amount=44.0),
        LineItem(quantity=1.0, unit_price=55.0, net_amount=55.0),
    ]

    async def _total_is_settled(expected):
        return False, 44.0  # row 0 committed, row 1 missing — never settles

    async def _read_grid_row(row_index, header_regexes):
        if row_index == 0:
            return {"^qty": "1.00", "unit price": "44.00"}       # row 0 is fine
        return {"^qty": "1.00", "unit price": "0.00"}             # row 1's price is the problem

    driver._total_is_settled = AsyncMock(side_effect=_total_is_settled)
    driver._read_grid_row = AsyncMock(side_effect=_read_grid_row)
    driver._fill_grid_field = AsyncMock(return_value=True)

    asyncio.run(driver._force_totals_commit(payload, line_items))

    calls = driver._fill_grid_field.call_args_list
    # The genuinely-short field is re-filled...
    assert any(c.args == (1, "Unit Price", "55.00", "unit price") for c in calls)
    # ...and row 0, which already reads correct, is only ever nudged on Qty — never has its correct
    # price rewritten, which is what could knock it out via the cross-row interference.
    assert not any(c.args[0] == 0 and c.args[1] == "Unit Price" for c in calls)


# --- P2: _assert_details_filled ------------------------------------------------------------------


def test_assert_details_filled_ignores_a_self_correcting_flicker():
    """A row that reads blank/zero once for a field, then recovers on the recheck, must NOT end up
    in `problems` and must NOT raise — this is the fail-safe gate born from the 151-empty-quotation
    incident, so a false positive here wrongly routes a genuinely fine WO to FAILED/review."""
    driver = _make_driver()
    payload = _payload()  # quantity=2.0, unit_price=44.0, net_amount=88.0, grand_total=95.92

    # First (full-row) read: Qty reads "0.00" (looks bad), everything else is fine.
    initial_row = {"item code": "SE-400212A", "^qty": "0.00", "unit price": "44.00", "remarks": "r"}
    # The recheck (single-field read) for Qty: it's actually fine now — a flicker, not a real
    # problem.
    recheck_row = {"^qty": "2.00"}

    call_log = []

    async def _read_grid_row(row_index, header_regexes):
        call_log.append(header_regexes)
        if header_regexes == ("item code", "^qty", "unit price", "remarks"):
            return initial_row
        if header_regexes == ("^qty",):
            return recheck_row
        raise AssertionError(f"unexpected _read_grid_row call: {header_regexes}")

    driver._read_grid_row = AsyncMock(side_effect=_read_grid_row)
    # Total After Tax reads back positive and matching net_amount immediately, so the function has
    # nothing else to complain about once the Qty flicker is correctly dismissed.
    driver.page.evaluate = AsyncMock(return_value="88.00")

    # Must not raise.
    asyncio.run(driver._assert_details_filled(payload))

    # The recheck path was actually exercised (not skipped).
    assert ("^qty",) in call_log


def test_assert_details_filled_still_raises_on_a_confirmed_problem():
    """A field that's still bad on the recheck must still raise — the recheck must not silence a
    genuine failure."""
    driver = _make_driver()
    payload = _payload()

    initial_row = {"item code": "SE-400212A", "^qty": "0.00", "unit price": "44.00", "remarks": "r"}
    recheck_row = {"^qty": "0.00"}  # still bad on recheck — a real problem

    async def _read_grid_row(row_index, header_regexes):
        if header_regexes == ("item code", "^qty", "unit price", "remarks"):
            return initial_row
        if header_regexes == ("^qty",):
            return recheck_row
        raise AssertionError(f"unexpected _read_grid_row call: {header_regexes}")

    driver._read_grid_row = AsyncMock(side_effect=_read_grid_row)
    driver.page.evaluate = AsyncMock(return_value="88.00")

    with pytest.raises(RuntimeError, match="Qty is '0.00'"):
        asyncio.run(driver._assert_details_filled(payload))
