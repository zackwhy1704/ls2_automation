"""Measure extraction accuracy against the labelled golden set — the go/no-go gate for live billing.

Unlike tests/test_extraction_golden.py (per-file pass/fail regression guard), this produces a
SCOREBOARD across the whole sample set: per-field accuracy, with the billing-critical fields called
out, plus how often the extraction trust gate (src.validator.check_extraction_trust) would fire on
real extractions. That's what tells you whether extraction is trustworthy enough to let the batch
auto-submit — and whether the trust gate will act as a light net or a floodgate.

Run it locally (needs the real sample PDFs in data/samples/ and ANTHROPIC_API_KEY):

    python -m scripts.measure_extraction
    python -m scripts.measure_extraction --council JBTC        # filter to one council's samples
    python -m scripts.measure_extraction --fail-under 0.98     # non-zero exit if critical acc < bar

Exits non-zero when billing-critical accuracy is below --fail-under (default 1.0), so it can gate a
"promote to live" checklist step in CI or a pre-deploy script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import settings
from src.validator import check_extraction_trust

_REPO = Path(__file__).resolve().parent.parent
_SAMPLES = _REPO / "data" / "samples"
_GOLDEN = _REPO / "tests" / "golden" / "wo_extractions.json"

# The three fields a wrong value on which means a wrong invoice. These gate go-live.
_CRITICAL = ("wo_po_number", "gl_number", "unit_price")
# Other exact-match code/identifier fields.
_EXACT = ("job_sheet_number", "job_date", "sr_number")
_MONEY = ("unit_price", "quantity", "net_amount", "grand_total",
          "discount_amount", "discount_percent", "gst_percent")
_NORMALIZED = ("town_council",)
_CONTAINS = ("service_location",)


def _norm(s) -> str:
    if s is None:
        return ""
    s = " ".join(str(s).upper().split())
    return s[4:].strip() if s.startswith("BLK ") else s


def _block_id(s) -> str:
    parts = _norm(s).split()
    return parts[0] if parts else ""


def _num_eq(a, b, tol: float = 0.01) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _field_correct(field: str, expected, actual) -> bool:
    if hasattr(actual, "isoformat"):
        actual = actual.isoformat()
    if field in _MONEY:
        return _num_eq(expected, actual)
    if field in _NORMALIZED:
        return _norm(expected) == _norm(actual)
    if field in _CONTAINS:
        return _block_id(expected) == _block_id(actual)
    return (expected or None) == (actual or None)  # exact; treat "" and None alike


def _golden_money_inconsistent(exp: dict) -> bool:
    """Sanity-check the LABEL itself for money consistency, using the same tolerance/logic as the
    real trust gate, so a mislabeled sample gets caught rather than silently penalizing the model."""
    net = exp.get("net_amount")
    grand = exp.get("grand_total")
    gst_pct = exp.get("gst_percent") or 0.0
    if net is None or grand is None:
        return False
    expected_grand = round(float(net) * (1 + float(gst_pct) / 100), 2)
    return abs(expected_grand - float(grand)) > 0.02


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--council", help="only samples whose key starts with this (e.g. JBTC, SKTC)")
    ap.add_argument("--fail-under", type=float, default=1.0,
                    help="exit non-zero if critical-field accuracy is below this (0..1, default 1.0)")
    args = ap.parse_args()

    settings.configure_logging()
    if not _GOLDEN.exists():
        print(f"golden file missing: {_GOLDEN}", file=sys.stderr)
        return 2
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))

    from src import extractor  # lazy: needs anthropic + API key

    all_fields = _CRITICAL + _EXACT + _NORMALIZED + _CONTAINS + tuple(
        f for f in _MONEY if f not in _CRITICAL)
    hits = {f: 0 for f in all_fields}
    seen = {f: 0 for f in all_fields}
    ran = skipped = 0
    per_sample_fail: list[str] = []
    trust_gate_flagged = 0
    golden_money_bad = 0

    for key in sorted(golden):
        if args.council and not key.startswith(args.council):
            continue
        if golden[key].get("_verified") is False:
            skipped += 1
            continue
        pdf = _SAMPLES / key
        if not pdf.exists():
            skipped += 1
            continue
        try:
            wo_obj = extractor.extract_from_pdf(str(pdf))
            got = wo_obj.model_dump()
        except Exception as exc:  # a hard extraction failure is itself a critical miss
            per_sample_fail.append(f"{key}: EXTRACTION ERROR — {exc}")
            ran += 1
            for f in _CRITICAL:
                seen[f] += 1  # counted as a miss (no hit increment)
            continue
        ran += 1

        exp = golden[key]
        bad_here: list[str] = []
        for f in all_fields:
            if f not in exp:
                continue
            seen[f] += 1
            if _field_correct(f, exp.get(f), got.get(f)):
                hits[f] += 1
            elif f in _CRITICAL:
                bad_here.append(f"{f}: exp {exp.get(f)!r} got {got.get(f)!r}")
        if bad_here:
            per_sample_fail.append(f"{key}: " + "; ".join(bad_here))

        # How often does the trust gate (money consistency + confidence) fire on real extractions?
        if check_extraction_trust(wo_obj):
            trust_gate_flagged += 1

        # Sanity on the LABELS themselves: are the golden money figures internally consistent?
        if _golden_money_inconsistent(exp):
            golden_money_bad += 1

    # ---- scoreboard ----
    print("\n" + "=" * 64)
    print("EXTRACTION ACCURACY" + (f" [{args.council}]" if args.council else ""))
    print("=" * 64)
    print(f"samples run: {ran}   skipped (PDF absent / unverified): {skipped}\n")
    if ran == 0:
        print("No sample PDFs present — put the real WO PDFs in data/samples/ and set ANTHROPIC_API_KEY.")
        return 2

    def _pct(f: str) -> float:
        return (hits[f] / seen[f]) if seen[f] else 1.0

    print("CRITICAL (a wrong value here = a wrong invoice):")
    crit_min = 1.0
    for f in _CRITICAL:
        p = _pct(f)
        crit_min = min(crit_min, p)
        print(f"  {f:<18} {hits[f]:>3}/{seen[f]:<3}  {p*100:5.1f}%")
    print("\nOther exact fields:")
    for f in _EXACT + _NORMALIZED + _CONTAINS:
        print(f"  {f:<18} {hits[f]:>3}/{seen[f]:<3}  {_pct(f)*100:5.1f}%")
    print("\nMoney fields:")
    for f in _MONEY:
        if f in _CRITICAL:
            continue
        print(f"  {f:<18} {hits[f]:>3}/{seen[f]:<3}  {_pct(f)*100:5.1f}%")

    print(f"\nTrust gate would route {trust_gate_flagged}/{ran} extractions to NEEDS_REVIEW.")
    if golden_money_bad:
        print(f"WARNING: {golden_money_bad} GOLDEN label(s) are themselves money-inconsistent — check the labels.")

    if per_sample_fail:
        print("\nCritical-field misses:")
        for line in per_sample_fail:
            print("  - " + line)

    print("\n" + "-" * 64)
    verdict = "PASS" if crit_min >= args.fail_under else "FAIL"
    print(f"Critical-field floor: {crit_min*100:.1f}%   (bar {args.fail_under*100:.1f}%)   -> {verdict}")
    print("-" * 64)
    return 0 if crit_min >= args.fail_under else 1


if __name__ == "__main__":
    raise SystemExit(main())
