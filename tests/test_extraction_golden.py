"""Golden-file regression test for the LLM extraction prompt.

Guards the prompt fixes (GL trailing-hyphen / numeric-account stripping, SR-vs-Schedule-Type,
Job-Sheet "Schd." fallback) against future regressions across BOTH councils and BOTH PDF kinds
(SKTC text PDFs, JBTC scanned/image PDFs).

This test hits the LIVE Anthropic API, so it is SKIPPED by default. Run it deliberately:

    RUN_LLM_TESTS=1 pytest tests/test_extraction_golden.py -v

Regenerate the golden file after an intentional prompt change you've verified by hand:

    python -m tests.regen_golden        # (or rerun the generator snippet)

Fields compared exactly: the business-critical strings (wo_po_number, gl_number, job_sheet_number,
town_council, service_location, job_date, sr_number). Monetary amounts are compared with a small
tolerance. prepared_by / nature_of_work are NOT asserted exactly — they vary on scanned PDFs (OCR
spacing) and are not validation-critical.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_TESTS") != "1",
    reason="live-API extraction test; set RUN_LLM_TESTS=1 to run",
)

_REPO = Path(__file__).resolve().parent.parent
_SAMPLES = _REPO / "data" / "samples"
_GOLDEN = _REPO / "tests" / "golden" / "wo_extractions.json"

# Codes / identifiers: must match byte-for-byte (these are the fields the prompt fixes target).
_EXACT_FIELDS = (
    "wo_po_number", "gl_number", "job_sheet_number", "job_date", "sr_number",
)
# Free-text fields from scanned PDFs: the DATA must match, but casing / "Blk " prefix / whitespace
# vary run-to-run on OCR, so compare normalized (uppercase, collapsed spaces, no leading "BLK").
_NORMALIZED_FIELDS = ("town_council",)
# service_location is the same block but the model may append a unit no. ("(#15-531)") or a block
# range ("334B - 336") run-to-run, so we assert containment of the block id rather than equality.
_CONTAINS_FIELDS = ("service_location",)
_NUMERIC_FIELDS = ("quantity", "unit_price", "net_amount", "grand_total")


def _norm(s) -> str:
    if s is None:
        return ""
    s = " ".join(str(s).upper().split())
    return s[4:].strip() if s.startswith("BLK ") else s


def _block_id(s) -> str:
    """First token of a normalized location, e.g. 'Blk 334B - 336 ...' -> '334B' — the stable part."""
    parts = _norm(s).split()
    return parts[0] if parts else ""


def _load_golden() -> dict[str, dict]:
    if not _GOLDEN.exists():
        pytest.skip(f"golden file missing: {_GOLDEN}")
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _golden_keys() -> list[str]:
    try:
        return sorted(_load_golden())
    except Exception:
        return []


@pytest.mark.parametrize("key", _golden_keys())
def test_extraction_matches_golden(key: str):
    from src import extractor  # imported lazily so non-LLM runs never need anthropic

    golden = _load_golden()[key]
    if golden.get("_verified") is False:
        pytest.skip(f"{key} label is _verified:false — hand-check it, then set _verified:true")
    pdf = _SAMPLES / key
    if not pdf.exists():
        # Sample PDFs are real client data and gitignored; on a clean clone there is nothing to run.
        pytest.skip(f"sample PDF not present locally: {pdf}")

    wo = extractor.extract_from_pdf(str(pdf))
    got = wo.model_dump()

    mismatches = []
    for f in _EXACT_FIELDS:
        exp = golden.get(f)
        actual = got.get(f)
        if hasattr(actual, "isoformat"):
            actual = actual.isoformat()
        if actual != exp:
            mismatches.append(f"{f}: expected {exp!r}, got {actual!r}")

    for f in _NORMALIZED_FIELDS:
        if _norm(got.get(f)) != _norm(golden.get(f)):
            mismatches.append(f"{f}: expected ~{golden.get(f)!r}, got {got.get(f)!r}")

    for f in _CONTAINS_FIELDS:
        # The block id (first token, e.g. '334B') is the billing-stable part; a unit no. or block
        # range appended after it is acceptable run-to-run variation. A different block id is a real
        # regression.
        if _block_id(golden.get(f)) != _block_id(got.get(f)):
            mismatches.append(f"{f}: block id differs — expected ~{golden.get(f)!r}, got {got.get(f)!r}")

    for f in _NUMERIC_FIELDS:
        exp, actual = golden.get(f), got.get(f)
        if exp is None and actual is None:
            continue
        if exp is None or actual is None or abs(float(actual) - float(exp)) > 0.01:
            mismatches.append(f"{f}: expected {exp!r}, got {actual!r}")

    assert not mismatches, f"{key} extraction drifted:\n  " + "\n  ".join(mismatches)
