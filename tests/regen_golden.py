"""Scaffold golden labels for NEW sample WOs — without blessing the model's own guesses.

A golden file is only useful if it's ground truth. If we regenerated it wholesale from the current
model output, the regression test would just assert "the model still says what the model said" — it
would rubber-stamp drift instead of catching it. So this tool is deliberately conservative:

  * for each PDF in data/samples/ NOT already in the golden file, it runs extraction and adds an
    entry marked "_verified": false — a STARTING POINT a human must check against the real WO;
  * it NEVER touches an entry that already exists (verified or not) unless you pass --force-key KEY
    for one specific key you've re-inspected.

Workflow to add new labelled samples:
    1. drop the new WO PDFs into data/samples/<COUNCIL>/<name>.pdf
    2. python -m tests.regen_golden            # scaffolds entries, "_verified": false
    3. open tests/golden/wo_extractions.json, CHECK each new entry against the PDF by eye,
       fix any wrong field, then set "_verified": true
    4. python -m scripts.measure_extraction    # confirm accuracy on the enlarged set

Needs the real PDFs in data/samples/ and ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import settings

_REPO = Path(__file__).resolve().parent.parent
_SAMPLES = _REPO / "data" / "samples"
_GOLDEN = _REPO / "tests" / "golden" / "wo_extractions.json"

# Fields we store as labels (mirror of the extractor output that the golden test compares).
_LABEL_FIELDS = (
    "wo_po_number", "town_council", "job_sheet_number", "service_location", "nature_of_work",
    "job_date", "prepared_by", "gl_number", "quantity", "unit_price", "discount_percent",
    "discount_amount", "net_amount", "gst_percent", "grand_total", "sr_number",
)


def _iter_sample_keys() -> list[str]:
    if not _SAMPLES.exists():
        return []
    return sorted(
        str(p.relative_to(_SAMPLES)).replace("\\", "/")
        for p in _SAMPLES.rglob("*.pdf")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force-key", action="append", default=[],
                    help="re-extract and overwrite this ONE key (repeatable) — use after you've "
                         "re-inspected that WO. Sets its entry back to _verified:false.")
    args = ap.parse_args()

    settings.configure_logging()
    golden: dict[str, dict] = json.loads(_GOLDEN.read_text("utf-8")) if _GOLDEN.exists() else {}
    keys = _iter_sample_keys()
    if not keys:
        print(f"No sample PDFs under {_SAMPLES}. Nothing to scaffold.", file=sys.stderr)
        return 2

    from src import extractor  # lazy: needs anthropic + API key

    added, forced, kept = 0, 0, 0
    for key in keys:
        exists = key in golden
        if exists and key not in args.force_key:
            kept += 1
            continue
        try:
            got = extractor.extract_from_pdf(str(_SAMPLES / key)).model_dump()
        except Exception as exc:
            print(f"  ! {key}: extraction failed — {exc}")
            continue
        entry = {f: (got.get(f).isoformat() if hasattr(got.get(f), "isoformat") else got.get(f))
                 for f in _LABEL_FIELDS}
        entry["_verified"] = False
        golden[key] = entry
        if exists:
            forced += 1
            print(f"  ~ {key}: re-scaffolded (was force-listed) — RE-VERIFY")
        else:
            added += 1
            print(f"  + {key}: scaffolded — VERIFY against the PDF, then set _verified:true")

    _GOLDEN.write_text(json.dumps(golden, indent=2, ensure_ascii=False, sort_keys=True), "utf-8")
    unverified = sum(1 for v in golden.values() if v.get("_verified") is False)
    print(f"\nwrote {_GOLDEN}: +{added} new, {forced} re-scaffolded, {kept} untouched.")
    if unverified:
        print(f"WARNING: {unverified} entr(ies) are _verified:false — hand-check them before trusting "
              "the golden test or the accuracy scoreboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
