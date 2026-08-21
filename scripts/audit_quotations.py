"""Independently audit the quotations this automation produced, record by record, against the WOs
they were built from.

WHY THIS EXISTS
    Every verification in this project has so far been hand-written per situation, and aggregate
    signals have repeatedly concealed real defects:
      - a batch reporting "93% success" had silently deleted a correct quotation belonging to
        another WO;
      - dedup reported NOT_DUPLICATE for three WOs that already had submitted quotations, which
        would have double-billed them;
      - a dedup fix passed 5 of 6 checks and was still wrong (bad control);
      - "3 live submissions, all verified" was actually 5, two of them never checked.
    Each was caught by digging into one record, not by reading a summary. This script makes that
    digging systematic and repeatable, so it can run after every scheduled batch.

WHAT IT CHECKS, per WO
    - exactly ONE quotation exists for the WO         (>1 => double-billing risk)
    - Subject references the WO-PO
    - Details row COUNT matches the WO's line items
    - per row: Qty and billed unit price (net/qty, not the gross rate)
    - page total reconciles to the WO's authorised net or grand total
    - Payment Method, External Remarks, Project Site, Customer Contact  (reported)
    - the quotation's status (Draft vs Pending Confirmation etc.)

MATCH / MISMATCH / UNREADABLE
    Fields are graded three ways, not two. A submitted quotation opened from the "All" tab renders
    read-only, and some labelled fields come back empty there even though the record is fine â€”
    reporting that as a MISMATCH would manufacture false failures, which is the exact mistake that
    made a working dedup fix look broken. Only a MISMATCH fails the audit; UNREADABLE is surfaced
    as a coverage gap so nobody mistakes it for a pass.

USAGE
    python -m scripts.audit_quotations                  # audit WOs with quotations, newest first
    python -m scripts.audit_quotations --limit 20
    python -m scripts.audit_quotations --wo WO-PO/000082151 --wo WO-PO/000082153
    python -m scripts.audit_quotations --all-statuses    # include invalid/failed WOs too

    Exit code 0 = no mismatches found. 1 = at least one MISMATCH (or a duplicate quotation).
    Read-only: opens records and reads values. Creates, edits and submits nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from config import settings
from src.models import WOPayload
from src.synergix_driver import EXTERNAL_REMARK_CODE, PAYMENT_METHOD, SynergixDriver
from src.validator import resolve_project_code

MATCH, MISMATCH, UNREADABLE, INFO = "MATCH", "MISMATCH", "UNREADABLE", "INFO"

# Statuses whose WOs should have a quotation in Synergix. Anything else never got that far.
_QUOTED_STATUSES = ("processed", "partial", "approved")


def _flat(text: str, limit: int = 90) -> str:
    """One-line, length-capped detail text. Several of these values (External Remarks, the list row)
    are multi-line, which would otherwise wreck a report meant to be skim-read after a nightly run."""
    return " ".join((text or "").split())[:limit]


def say(*parts: object) -> None:
    """print(), but flushed. A full sweep is ~40s per WO and hours long overall; with Python's
    default block buffering on a redirected stdout, a run piped to a log file (which is exactly how
    run_batch.ps1 invokes things) shows NOTHING until it exits — and shows nothing at all if it is
    killed or the host reboots partway. Progress on a long audit has to be visible as it happens."""
    print(*parts, flush=True)


@dataclass
class Finding:
    field: str
    verdict: str
    detail: str


@dataclass
class WOAudit:
    wo: str
    quotation_ids: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.findings.append(Finding(name, verdict, detail))

    @property
    def mismatches(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == MISMATCH]

    @property
    def unreadable(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == UNREADABLE]


def load_payloads(wos: list[str] | None, limit: int | None, all_statuses: bool) -> list[WOPayload]:
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    if wos:
        rows = [
            conn.execute(
                "SELECT payload_json FROM work_orders WHERE wo_po_number=?", (w,)
            ).fetchone()
            for w in wos
        ]
        rows = [r for r in rows if r and r["payload_json"]]
    else:
        sql = "SELECT payload_json FROM work_orders WHERE payload_json IS NOT NULL"
        params: tuple = ()
        if not all_statuses:
            placeholders = ",".join("?" * len(_QUOTED_STATUSES))
            sql += f" AND status IN ({placeholders})"
            params = _QUOTED_STATUSES
        sql += " ORDER BY updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        try:
            out.append(WOPayload.model_validate(json.loads(r["payload_json"])))
        except Exception:
            continue
    return out[:limit] if limit else out


async def _read_dropdown_label(page, label: str) -> str | None:
    """Selected text of a ui-selectonemenu by its field label. _read_labeled_value cannot read these
    â€” it looks for an <input>, but a closed PrimeFaces dropdown shows its value in a <label>, so it
    silently returns '' (which would read as 'Payment Method is blank' on a perfectly fine record)."""
    return await page.evaluate(
        """(label) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const host = [...document.querySelectorAll('td,div,span,label')]
                .find(e => e.children.length === 0 && norm(e.textContent) === label);
            if (!host) return null;
            const tr = host.closest('tr');
            const menu = tr ? tr.querySelector('.ui-selectonemenu-label') : null;
            return menu ? menu.textContent.trim() : null;
        }""",
        label,
    )


async def quotations_for(d, page, wo: str) -> list[dict] | None:
    """Every quotation referencing this WO, from the All tab (submitted ones are not in Draft)."""
    await d._open_service_quotation_list()
    if not await d._select_quotation_status_tab("All"):
        return None
    header = page.locator("th:visible", has_text="Enquiry/Subject").first
    fi = header.locator("input.ui-column-filter").first
    if not await fi.count():
        return None
    await fi.click()
    await fi.fill("")
    await fi.press("Enter")
    await page.wait_for_timeout(2000)
    await fi.fill(wo)
    await fi.press("Enter")
    await page.wait_for_timeout(6000)
    return await page.evaluate(
        """() => {
            const out = [];
            [...document.querySelectorAll('[id$="serviceQuotationTable_data"]')]
                .filter(b => b.offsetParent !== null)
                .forEach(b => b.querySelectorAll('tr').forEach(tr => {
                    const t = tr.innerText || '';
                    const m = t.match(/QUO\\d+/);
                    if (m) out.push({id: m[0], row: t.replace(/\\t/g, ' | ').slice(0, 200)});
                }));
            return out;
        }"""
    )


async def open_quotation(d, page, quo: str) -> bool:
    await d._open_service_quotation_list()
    if not await d._select_quotation_status_tab("All"):
        return False
    header = page.locator("th:visible", has_text="Quotation No.").first
    fi = header.locator("input.ui-column-filter").first
    if not await fi.count():
        return False
    await fi.click()
    await fi.fill("")
    await fi.press("Enter")
    await page.wait_for_timeout(2000)
    await fi.fill(quo)
    await fi.press("Enter")
    await page.wait_for_timeout(5000)
    link = page.get_by_role("link", name=quo, exact=True)
    if not await link.count():
        return False
    await link.first.click(timeout=15000)
    await page.wait_for_timeout(5000)
    return True


async def audit_one(d, page, payload: WOPayload) -> WOAudit:
    wo = payload.wo_po_number
    audit = WOAudit(wo=wo)
    line_items = payload.effective_line_items

    rows = await quotations_for(d, page, wo)
    if rows is None:
        audit.add("lookup", UNREADABLE, "could not reach the All tab")
        return audit

    ids = []
    for r in rows:
        if r["id"] not in ids:
            ids.append(r["id"])
    audit.quotation_ids = ids

    if not ids:
        audit.add("quotation exists", MISMATCH, "no quotation found for this WO")
        return audit
    if len(ids) > 1:
        audit.add("single quotation", MISMATCH,
                  f"{len(ids)} quotations reference this WO ({', '.join(ids)}) â€” double-billing risk")
    else:
        audit.add("single quotation", MATCH, ids[0])

    # Status, straight off the list row (the record view doesn't always show it).
    for r in rows:
        if r["id"] == ids[0]:
            audit.add("list row", INFO, _flat(r["row"]))
            break

    if not await open_quotation(d, page, ids[0]):
        audit.add("open record", UNREADABLE, f"could not open {ids[0]}")
        return audit

    # --- Subject ---
    subject = (await d._read_labeled_value("Enquiry/Subject") or "").strip()
    if not subject:
        audit.add("subject", UNREADABLE, "field not readable in this view")
    elif wo in subject:
        audit.add("subject", MATCH, _flat(subject))
    else:
        audit.add("subject", MISMATCH, f"{subject!r} does not reference {wo}")

    # --- Details rows: count, qty, unit price ---
    grid_rows = []
    for i in range(len(line_items) + 2):  # look past the expected count to catch extras
        r = await d._read_grid_row(i, ("^qty", "unit price"))
        if not r or not any((r or {}).values()):
            break
        grid_rows.append(r)

    if len(grid_rows) != len(line_items):
        audit.add("row count", MISMATCH,
                  f"WO has {len(line_items)} line item(s), quotation has {len(grid_rows)}")
    else:
        audit.add("row count", MATCH, str(len(line_items)))

    for i, li in enumerate(line_items):
        if i >= len(grid_rows):
            audit.add(f"row {i}", MISMATCH, "missing from the quotation")
            continue
        got = grid_rows[i]
        for name, want, key in (
            ("qty", li.quantity, "^qty"),
            ("unit price", li.billed_unit_price, "unit price"),
        ):
            raw = got.get(key)
            try:
                actual = float(raw)
            except (TypeError, ValueError):
                audit.add(f"row {i} {name}", UNREADABLE, f"unparseable: {raw!r}")
                continue
            if abs(actual - float(want)) <= 0.005:
                audit.add(f"row {i} {name}", MATCH, f"{actual:.2f}")
            else:
                audit.add(f"row {i} {name}", MISMATCH, f"expected {float(want):.2f}, got {actual:.2f}")

    # --- Total: the only value reflecting what the SERVER committed ---
    total = await d._read_total_after_tax()
    expected = d._expected_totals(payload)
    if total is None:
        audit.add("total", UNREADABLE, "no total on the page")
    elif not expected:
        audit.add("total", INFO, f"{total:.2f} (WO has no net/grand total to compare)")
    elif any(abs(total - e) <= 0.05 for e in expected):
        audit.add("total", MATCH, f"{total:.2f}")
    else:
        audit.add("total", MISMATCH,
                  f"{total:.2f} matches neither net {payload.net_amount} nor grand {payload.grand_total}")

    # --- Supporting fields. Reported, and only a MISMATCH when positively wrong. ---
    pay = await _read_dropdown_label(page, "Payment Method")
    if pay is None or not pay.strip():
        audit.add("payment method", UNREADABLE, "not readable in this view")
    elif PAYMENT_METHOD.lower() in pay.lower():
        audit.add("payment method", MATCH, pay)
    else:
        audit.add("payment method", MISMATCH, f"expected {PAYMENT_METHOD}, got {pay!r}")

    remarks = (await d._read_labeled_value("External Remarks") or "").strip()
    if not remarks:
        audit.add("external remarks", UNREADABLE, "blank or not readable")
    elif EXTERNAL_REMARK_CODE.split()[0].lower() in remarks.lower():
        audit.add("external remarks", MATCH, _flat(remarks, 60))
    else:
        audit.add("external remarks", MISMATCH, f"expected {EXTERNAL_REMARK_CODE}, got {_flat(remarks, 60)!r}")

    site = (await d._read_labeled_value("Project Site") or "").strip()
    want_code = resolve_project_code(payload.job_sheet_number)
    if not site:
        audit.add("project site", UNREADABLE, "not readable in this view")
    elif want_code and want_code in site:
        audit.add("project site", MATCH, site)
    else:
        audit.add("project site", MISMATCH if want_code else INFO,
                  f"expected code {want_code}, got {site!r}")

    # Customer Contact is a business decision (many TC officers are not registered Synergix
    # contacts), so it is reported for review and never failed.
    contact = (await d._read_labeled_value("Customer Contact") or "").strip()
    audit.add("customer contact", INFO,
              f"{contact!r} (WO officer: {payload.property_officer or '-'})")

    return audit


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wo", action="append", help="audit specific WO-PO(s); repeatable")
    ap.add_argument("--limit", type=int, help="cap how many WOs are audited")
    ap.add_argument("--all-statuses", action="store_true",
                    help="include WOs that never reached the quotation stage")
    ap.add_argument("--quiet", action="store_true", help="only print problems and the summary")
    args = ap.parse_args()

    settings.configure_logging()
    payloads = load_payloads(args.wo, args.limit, args.all_statuses)
    if not payloads:
        say("no WOs to audit")
        return 0
    say(f"auditing {len(payloads)} WO(s) against Synergix\n")

    audits: list[WOAudit] = []
    d = SynergixDriver()
    await d.start()
    try:
        for n, payload in enumerate(payloads, 1):
            # Session guardrails, mirroring batch.run_batch_from_tcms. Synergix degrades under
            # sustained automation: a first attempt at this sweep died at WO 24 on a 30s
            # Page.goto timeout, which is the same decay the batch pipeline already defends
            # against with a periodic re-login and a full browser recycle.
            every = settings.SYNERGIX_RELOGIN_EVERY
            if every and n > 1 and (n - 1) % every == 0:
                try:
                    await d.relogin()
                except Exception as exc:
                    say(f"      (re-login before {payload.wo_po_number} failed: {exc})")
            fresh = settings.SYNERGIX_FRESH_BROWSER_EVERY
            if fresh and n > 1 and (n - 1) % fresh == 0:
                say(f"      (recycling the Synergix browser after {n - 1} WOs)")
                try:
                    await d.close()
                    d = SynergixDriver()
                    await d.start()
                except Exception as exc:
                    say(f"      (browser recycle failed: {exc})")

            # Per-WO isolation: an audit is a diagnostic, so one unreachable record must never
            # cost the other 291 results. Without this the first navigation timeout ended the
            # whole run and discarded everything after it.
            try:
                a = await audit_one(d, d.page, payload)
            except Exception as exc:
                a = WOAudit(wo=payload.wo_po_number)
                a.add("audit", UNREADABLE, f"errored: {type(exc).__name__}: {exc}".split("\n")[0][:160])
            audits.append(a)
            bad, gaps = a.mismatches, a.unreadable
            flag = "FAIL" if bad else ("gaps" if gaps else "ok")
            say(f"[{n}/{len(payloads)}] {a.wo}  {flag}  {a.quotation_ids}")
            for f in a.findings:
                if args.quiet and f.verdict in (MATCH, INFO):
                    continue
                say(f"      {f.verdict:<10} {f.field}: {f.detail}")
    finally:
        try:
            await d.close()
        except Exception:
            pass

    failed = [a for a in audits if a.mismatches]
    gapped = [a for a in audits if not a.mismatches and a.unreadable]
    say("\n" + "=" * 78)
    say(f"audited {len(audits)} WO(s): {len(audits) - len(failed) - len(gapped)} clean, "
          f"{len(gapped)} with unreadable fields, {len(failed)} with MISMATCHES")
    for a in failed:
        say(f"\n  FAIL {a.wo} ({', '.join(a.quotation_ids) or 'no quotation'})")
        for f in a.mismatches:
            say(f"      {f.field}: {f.detail}")
    if gapped and not failed:
        say("\n  (unreadable fields are a coverage gap, not a defect â€” submitted quotations render "
              "read-only in the All tab and some labelled fields cannot be read there)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


