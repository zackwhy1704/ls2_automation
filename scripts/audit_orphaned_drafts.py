"""Read-only cross-reference: how many WOs on TCMS's Un-Invoiced list have a Synergix quotation
sitting in "Draft" status (i.e. created but never submitted) that would report DUPLICATE on any
future dedup check, permanently masking that WO from ever being retried.

Requested to independently verify a colleague's claim ("107 orphaned DRY_RUN drafts since Aug 4")
that did not match this session's own records (a specific example cited, QUO0006649, was found in
logs/run.log to have already been aborted on 2026-08-17, not "legitimate" as claimed).

Method:
  1. Scrape TCMS's Un-Invoiced WO list (source of un-invoiced WO-PO numbers).
  2. Open Synergix Service Quotation list, "Draft" tab (quotations created but never submitted).
  3. For each Draft-tab quotation, read its Enquiry/Subject cell (contains the WO-PO number) and
     check whether that WO-PO number is in the Un-Invoiced list from step 1.
  4. Report the matching set -- these are exactly the orphaned drafts that would mask a real WO.

Read-only throughout: navigates and reads grids, never clicks Submit/Confirm/Abort/Delete on
anything.

Usage:
    python -m scripts.audit_orphaned_drafts
"""
from __future__ import annotations

import asyncio

from config import settings
from src.synergix_driver import SynergixDriver
from src.tcms_scraper import TCMSScraper


async def _list_draft_quotations(synergix: SynergixDriver) -> list[tuple[str, str]]:
    """Return (quotation_no, enquiry_subject_text) for every row in the Draft tab."""
    await synergix._open_service_quotation_list()
    page = synergix.page
    assert page is not None
    if not await synergix._select_quotation_status_tab("Draft"):
        raise RuntimeError("could not switch to Draft tab")

    all_rows: list[tuple[str, str]] = []
    # Best-effort only: if this selector is present but not interactable (e.g. hidden by layout at
    # this scroll position), fall back to the default page size and paginate via "Next" instead --
    # slower, but does not block the scrape entirely on an unconfirmed selector guess.
    try:
        page_size_select = page.locator("select.ui-paginator-rpp-options").first
        if await page_size_select.count():
            await page_size_select.select_option("100", timeout=5000)
            await page.wait_for_timeout(3000)
    except Exception:
        print("(page-size selector not usable -- paginating at the default size instead)")

    seen_quo_nos: set[str] = set()
    for page_num in range(1, 101):  # safety cap -- at even 10/page this covers 1000 drafts
        rows = await page.evaluate(
            """() => {
                const body = [...document.querySelectorAll('[id$="serviceQuotationTable_data"]')]
                    .find(b => b.offsetParent !== null);
                if (!body) return [];
                return [...body.querySelectorAll('tr')].map(tr => {
                    const cells = [...tr.querySelectorAll('td')];
                    if (cells.length < 5) return null;
                    const quoNo = cells[0]?.innerText?.trim() || '';
                    const enquiry = cells[4]?.innerText?.trim() || '';
                    return quoNo ? [quoNo, enquiry] : null;
                }).filter(Boolean);
            }"""
        )
        new_rows = [tuple(r) for r in rows if r[0] not in seen_quo_nos]
        if not new_rows and page_num > 1:
            print(f"  page {page_num}: no new rows -- stopping (pagination likely looped or ended)")
            break
        all_rows.extend(new_rows)
        seen_quo_nos.update(r[0] for r in new_rows)
        print(f"  page {page_num}: {len(new_rows)} new row(s), {len(all_rows)} total so far")

        next_btn = page.locator("a.ui-paginator-next:not(.ui-state-disabled)").first
        if not await next_btn.count():
            break
        await next_btn.click()
        await page.wait_for_timeout(2500)

    return all_rows


async def main() -> None:
    if "copy." not in settings.SYNERGIX_BASE_URL:
        print(f"Refusing: SYNERGIX_BASE_URL {settings.SYNERGIX_BASE_URL!r} is not the non-production "
              "copy environment. Aborting for safety.")
        return

    print("=== Step 1: scraping TCMS Un-Invoiced WO list ===")
    async with TCMSScraper() as tcms:
        await tcms.login()
        uninvoiced = await tcms.list_uninvoiced()
    print(f"{len(uninvoiced)} un-invoiced WO(s) on TCMS")
    uninvoiced_set = set(uninvoiced)

    print("\n=== Step 2: scraping Synergix Draft-tab quotations ===")
    synergix = SynergixDriver()
    await synergix.start()
    try:
        drafts = await _list_draft_quotations(synergix)
    finally:
        await synergix.close()
    print(f"{len(drafts)} quotation(s) in Draft status")

    print("\n=== Step 3: cross-referencing ===")
    orphaned = []
    for quo_no, enquiry in drafts:
        for wo in uninvoiced_set:
            bare = wo.replace("WO-PO/", "")
            if bare in enquiry or wo in enquiry:
                orphaned.append((wo, quo_no, enquiry))
                break

    print(f"\n{len(orphaned)} orphaned draft(s) found (Draft-status quotation for a still-un-invoiced WO):")
    for wo, quo_no, enquiry in orphaned:
        print(f"  {wo} -> {quo_no}  ({enquiry[:80]})")


if __name__ == "__main__":
    asyncio.run(main())
