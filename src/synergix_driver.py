"""Synergix ERP driver: duplicate check + create/fulfil. DRY_RUN aware.

In DRY_RUN (the default) every step runs EXCEPT the final submit/confirm clicks, which are logged
instead of executed. Live submission only happens when DRY_RUN=false is set in .env.

Stages from the workflow doc:
  B — Create quotation: new quotation -> "Copy From" template -> fill ~8 fields -> submit
  C — Schedule board update: MOST FRAGILE. Best-effort; failure marks PARTIAL, not FAILED.
  D — Attach PDF + fulfil service order.

After any WO (success or fail), navigate back to a known home state before the next.
Browser access is serialised by the caller via an asyncio.Lock (writes run one at a time).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from playwright.async_api import Page, async_playwright

from config import selectors as S
from config import settings
from src.models import WOPayload, WOStatus
from src.validator import build_remarks, resolve_project_code

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    status: WOStatus            # PROCESSED, PARTIAL, or FAILED
    detail: str = ""


class DedupResult(str, Enum):
    """Three-state duplicate check. UNCERTAIN is fail-safe: never auto-bill, flag for a human.

    Needed because the JBTC "Un-Invoiced WO" list is maintained by hand, so a WO can still appear
    there after it has actually been invoiced in Synergix. Synergix — not JBTC — is the source of
    truth for whether a WO is already billed. A boolean can't distinguish "confirmed not billed" from
    "couldn't tell" (search error/timeout/ambiguous result); conflating them risks double-billing.
    """

    NOT_DUPLICATE = "not_duplicate"   # confirmed: no existing invoice found in Synergix
    DUPLICATE = "duplicate"           # confirmed: an existing invoice matches -> do NOT bill again
    UNCERTAIN = "uncertain"           # search errored/ambiguous -> human must verify before billing

    @property
    def safe_to_bill(self) -> bool:
        return self is DedupResult.NOT_DUPLICATE


def _dry_guard(action: str) -> bool:
    """Return True if the (final, mutating) action should be SKIPPED due to DRY_RUN.

    Logs the decision either way so the run log clearly shows what was/wasn't submitted.
    """
    if settings.DRY_RUN:
        logger.info("[DRY_RUN] SKIPPING final action: %s", action)
        return True
    logger.info("[LIVE] executing final action: %s", action)
    return False


def synergix_configured() -> bool:
    """True once the client's Synergix env is available (base URL set).

    Until the sandbox is procured this is False, and the driver runs in STUB mode: no browser is
    launched, the duplicate check returns False, and write() reports PROCESSED with a stub note so
    the full ingest -> extract -> validate -> approve loop can be exercised end to end. Filling in
    SYNERGIX_* in .env switches every step to the real browser automation with no code change.
    """
    return bool(settings.SYNERGIX_BASE_URL.strip())


class SynergixDriver:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._logged_in = False

    @property
    def stubbed(self) -> bool:
        """True when Synergix is not configured yet — driver runs without a browser."""
        return not synergix_configured()

    async def start(self) -> None:
        if self.stubbed:
            logger.warning(
                "Synergix not configured (SYNERGIX_BASE_URL empty) — running in STUB mode: "
                "no browser, dedup skipped, writes are simulated. Fill SYNERGIX_* in .env to go live."
            )
            return
        self._pw = await async_playwright().start()
        # Synergix (taskhub.ls2.sg) sits behind Cloudflare bot protection: headless Chromium gets
        # blocked. Run a persistent, non-headless context with a realistic UA and the automation flag
        # disabled so we look like a normal browser. Persisting the profile also reuses the login
        # cookie across runs (Synergix sessions are short-lived).
        settings.SYNERGIX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._context = await self._pw.chromium.launch_persistent_context(
            str(settings.SYNERGIX_SESSION_DIR),
            headless=settings.SYNERGIX_HEADLESS,
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.page.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT_MS)
        logger.info("Synergix browser launched (headless=%s, persistent)", settings.SYNERGIX_HEADLESS)

    async def close(self) -> None:
        if self.stubbed:
            return
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        if self._logged_in:
            return
        if not settings.SYNERGIX_BASE_URL:
            raise RuntimeError("SYNERGIX_BASE_URL is not set in .env")
        assert self.page is not None
        # JSF/PrimeFaces app keeps connections open, so wait on DOM content, not networkidle.
        await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)

        login_field = self.page.locator(S.require("SYNERGIX_USERNAME_INPUT", S.SYNERGIX_USERNAME_INPUT))
        if await login_field.count():
            # Login form is present -> authenticate.
            await login_field.fill(settings.SYNERGIX_USERNAME)
            await self.page.fill(
                S.require("SYNERGIX_PASSWORD_INPUT", S.SYNERGIX_PASSWORD_INPUT), settings.SYNERGIX_PASSWORD
            )
            await self.page.click(S.require("SYNERGIX_LOGIN_BUTTON", S.SYNERGIX_LOGIN_BUTTON))
        else:
            # Persisted session already logged in — we landed straight on the app.
            logger.info("Synergix session reused (no login form present)")

        # Confirm we're on the app: the header home button is always present once authenticated.
        await self.page.wait_for_selector("#headerToolbarFormLeft\\:homeButton", timeout=30000)
        self._logged_in = True
        logger.info("Synergix login successful")

    # ------------------------------------------------------------------ duplicate check
    def _dedup_search_value(self, payload: WOPayload) -> str:
        """The WO field to search Synergix on, per SYNERGIX_DEDUP_KEY config."""
        if settings.SYNERGIX_DEDUP_KEY == "job_sheet":
            return payload.job_sheet_number
        return payload.wo_po_number

    async def _open_service_quotation_list(self) -> None:
        """Navigate (logged in) to General Service -> Service Quotation - LS2 list view.

        Re-navigates from the base URL each time so the grid is a fresh, unfiltered instance — calling
        this twice in one session without the reset can leave a stale/filtered datatable.
        """
        await self.login()
        assert self.page is not None
        await self.page.goto(settings.SYNERGIX_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(4000)
        await self.page.get_by_text("General Service", exact=False).first.click()
        await self.page.wait_for_timeout(3000)
        await self.page.get_by_text("Service Quotation - LS2", exact=False).first.click()
        # The JSF datatable renders lazily; wait for the Enquiry/Subject column to exist.
        await self.page.wait_for_selector("th:has-text('Enquiry/Subject')", timeout=30000)
        await self.page.wait_for_timeout(4000)

    async def check_duplicate(self, payload: WOPayload) -> DedupResult:
        """Is this WO already invoiced in Synergix? Returns a three-state, fail-safe result.

        The JBTC "Un-Invoiced WO" list is hand-maintained and can be stale, so Synergix is the source
        of truth. Any error, timeout, or ambiguous page yields UNCERTAIN (not NOT_DUPLICATE), so a WO
        is NEVER silently billed when we can't confirm it is unbilled — avoiding double invoicing.

        A confirmed NOT_DUPLICATE requires positive evidence of "no records" (the no-result marker),
        not merely the absence of result rows — otherwise a layout change would read as "safe to bill".
        """
        search_value = self._dedup_search_value(payload)

        if self.stubbed:
            if settings.DEDUP_STUB_ASSUME_SAFE:
                logger.warning("[STUB] DEDUP_STUB_ASSUME_SAFE=true — assuming %s NOT invoiced "
                               "(DEV ONLY; never use with real billing)", search_value)
                return DedupResult.NOT_DUPLICATE
            # No Synergix yet: we CANNOT verify invoiced status, so this is genuinely uncertain.
            logger.warning("[STUB] cannot verify invoiced status for %s (Synergix not configured) "
                           "-> UNCERTAIN (needs human review)", search_value)
            return DedupResult.UNCERTAIN

        try:
            await self._open_service_quotation_list()
            assert self.page is not None

            # Filter the Enquiry/Subject column by the WO-PO. This JSF grid's ids are auto-generated,
            # so target the column by its header text, then the stable filter class within it. The
            # PrimeFaces column filter applies on Enter.
            header = self.page.locator("th", has_text="Enquiry/Subject").first
            filter_input = header.locator("input.ui-column-filter").first
            await filter_input.click()
            await filter_input.fill("")
            await filter_input.press("Enter")            # clear any prior filter first
            await self.page.wait_for_timeout(2000)
            await filter_input.fill(search_value)
            await filter_input.press("Enter")
            await self.page.wait_for_timeout(6000)        # PrimeFaces ajax re-filter + settle

            # Read the filtered grid body: does it contain the WO-PO, or the "No records found" row?
            grid = await self.page.evaluate(
                "(wo) => { const t = document.querySelector('[id$=\"serviceQuotationTable_data\"]');"
                " const txt = t ? t.innerText : '';"
                " return { present: !!t, empty: /no records found|no data/i.test(txt),"
                " match: txt.includes(wo) }; }",
                search_value,
            )

            if not grid["present"]:
                logger.warning("Dedup %s: UNCERTAIN (quotation grid not found)", search_value)
                return DedupResult.UNCERTAIN
            if grid["match"]:
                logger.info("Dedup %s: DUPLICATE (existing quotation found)", search_value)
                return DedupResult.DUPLICATE
            if grid["empty"]:
                logger.info("Dedup %s: NOT_DUPLICATE (Synergix reports 'No records found')", search_value)
                return DedupResult.NOT_DUPLICATE
            # Filtered but neither a WO match nor the explicit empty marker — can't be sure. Fail safe.
            logger.warning("Dedup %s: UNCERTAIN (no WO match and no 'no records' marker)", search_value)
            return DedupResult.UNCERTAIN
        except Exception:
            logger.exception("Dedup check for %s errored — returning UNCERTAIN (fail-safe)", search_value)
            return DedupResult.UNCERTAIN

    # ------------------------------------------------------------------ write path
    async def write(self, payload: WOPayload) -> WriteResult:
        """Run stages B, C, D for one WO. Returns a WriteResult; never raises for a WO-level error."""
        if self.stubbed:
            project_code = resolve_project_code(payload.job_sheet_number)
            remarks = build_remarks(payload)
            logger.info(
                "[STUB] would write %s to Synergix: project_code=%s\n  remarks=%s",
                payload.wo_po_number, project_code, remarks,
            )
            return WriteResult(
                WOStatus.PROCESSED,
                "Synergix stubbed — no write performed (SYNERGIX_* not configured). "
                f"project_code={project_code}",
            )
        try:
            await self.login()
            await self._stage_b_create_quotation(payload)
            quo_id = await self._submit_quotation(payload)
            # Schedule Board (C) and Fulfil (D) remain manual — done by the team in Synergix.
            if settings.DRY_RUN:
                return WriteResult(
                    WOStatus.PARTIAL,
                    f"DRY_RUN: quotation draft {quo_id or '(id unread)'} created + filled, NOT submitted.",
                )
            return WriteResult(
                WOStatus.PROCESSED,
                f"Quotation {quo_id or '(id unread)'} created + submitted. "
                "Schedule board + fulfil still manual.",
            )
        except S.MissingSelectorError as exc:
            logger.error("MISSING SELECTOR: %s — fill it in config/selectors.py", exc)
            return WriteResult(WOStatus.FAILED, f"missing selector: {exc}")
        except Exception as exc:
            logger.exception("Synergix write failed for %s", payload.wo_po_number)
            await self._screenshot(payload.wo_po_number)
            return WriteResult(WOStatus.FAILED, str(exc))
        finally:
            await self._back_to_home()

    def _subject(self, payload: WOPayload) -> str:
        """Enquiry/Subject string, capped at Synergix's 50-char limit.

        Format `WO-PO/<num> - <Town Council>` (service-type suffix dropped per client, to fit 50).
        """
        council = payload.town_council.strip().title() or "Town Council"
        subject = f"{payload.wo_po_number} - {council}"
        return subject[:50]

    async def _fill_labeled_input(self, label: str, value: str) -> None:
        """Fill the input/textarea belonging to a form field identified by its on-screen label.

        Synergix's JSF ids are auto-generated, so we anchor on the (stable) label text and take the
        input in the same table row. Raises if the field can't be found — the caller marks FAILED.
        """
        assert self.page is not None
        handle = await self.page.evaluate_handle(
            """(label) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const host = [...document.querySelectorAll('td,div,span,label')]
                  .find(e => e.children.length === 0 && norm(e.textContent) === label);
                if (!host) return null;
                const tr = host.closest('tr');
                return (tr && tr.querySelector('input:not([type=hidden]):not([readonly]), textarea')) || null;
            }""",
            label,
        )
        element = handle.as_element()
        if element is None:
            raise RuntimeError(f"could not locate the input for field {label!r}")
        await element.click()
        await element.fill(value)

    async def _stage_b_create_quotation(self, payload: WOPayload) -> None:
        """Create a draft Service Quotation by Copy From, fill the WO-specific fields, DON'T submit.

        Everything not set here (customer, contact, item code, project segment) is inherited from the
        copied template. Leaves the draft filled and un-submitted for a human to review + Submit.
        """
        assert self.page is not None
        page = self.page
        logger.info("Stage B: create quotation draft for %s", payload.wo_po_number)

        await self._open_service_quotation_list()

        # New draft, then Copy From the most recent quotation for this town council.
        await page.locator("button:has(span.fa-plus)").first.click()
        await page.wait_for_timeout(8000)
        await page.get_by_role("button", name="Copy From").first.click()
        await page.wait_for_timeout(7000)

        cust_filter = page.locator("th:has-text('Customer') input.ui-column-filter").first
        await cust_filter.click()
        await cust_filter.fill(payload.town_council.strip())
        await cust_filter.press("Enter")
        await page.wait_for_timeout(6000)

        # The Copy From modal lists matching quotations newest-first; copy the top one.
        top_link = page.locator(".ui-dialog:visible [id$='_data'] tr a, [id$='_data'] tr a").first
        if not await top_link.count():
            raise RuntimeError(f"no template quotation found for customer {payload.town_council!r}")
        await top_link.click()
        await page.wait_for_timeout(2500)
        # Confirm the "override current data" dialog.
        await page.get_by_role("button", name="Yes").first.click()
        await page.wait_for_timeout(12000)

        # Overwrite the WO-specific fields (label-anchored; ids are unstable).
        await self._fill_labeled_input("Enquiry/Subject", self._subject(payload))
        await self._fill_labeled_input("Reference No.", payload.gl_number)
        logger.info("Stage B: filled Subject + Reference No. for %s", payload.wo_po_number)

        # Line item: unit price + remarks live in the Details grid. Set them if present.
        await self._fill_line_item(payload)
        logger.info("Stage B: draft filled for %s", payload.wo_po_number)

    async def _submit_quotation(self, payload: WOPayload) -> str | None:
        """Submit the filled draft (DRY_RUN-gated). Returns the quotation ID if it can be read.

        In DRY_RUN the Submit click is skipped and logged. Otherwise it clicks Submit and confirms any
        follow-up dialog. The quotation ID (QUO...) is read from the form title bar either way.
        """
        assert self.page is not None
        page = self.page
        quo_id = await self._current_quotation_id()

        if _dry_guard(f"submit quotation for {payload.wo_po_number} (draft {quo_id})"):
            return quo_id  # DRY_RUN: left as a draft

        await page.locator("button:has(span.fa-vote-yea)").first.click()
        await page.wait_for_timeout(3000)
        # A confirm dialog may appear (Yes/OK) — click it if present.
        for label in ("Yes", "OK", "Confirm"):
            btn = page.get_by_role("button", name=label)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click()
                break
        await page.wait_for_timeout(8000)
        logger.info("Submitted quotation %s for %s", quo_id, payload.wo_po_number)
        return quo_id

    async def _current_quotation_id(self) -> str | None:
        """Read the QUO id from the form title bar (e.g. 'Service Quotation - LS2 [QUO0006225]')."""
        assert self.page is not None
        try:
            text = await self.page.locator("text=/QUO[0-9]+/").first.inner_text()
            import re
            m = re.search(r"QUO\d+", text)
            return m.group(0) if m else None
        except Exception:
            return None

    async def _fill_line_item(self, payload: WOPayload) -> None:
        """Set the line Unit Price and Remarks in the Details grid (best-effort, logged if absent)."""
        assert self.page is not None
        remarks = build_remarks(payload)
        try:
            # The Details grid has a "Remarks" column cell (editable) and a Unit Price input.
            filled = await self.page.evaluate(
                """([price, remarks]) => {
                    const setVal = (el, v) => {
                      if (!el) return false;
                      el.focus(); el.value = v;
                      el.dispatchEvent(new Event('input', {bubbles:true}));
                      el.dispatchEvent(new Event('change', {bubbles:true}));
                      return true;
                    };
                    // Unit Price: an input in the details row whose column header is 'Unit Price'
                    let priceOk = false, remarkOk = false;
                    const grids = [...document.querySelectorAll('.ui-datatable')];
                    for (const g of grids) {
                      const heads = [...g.querySelectorAll('th')].map(t => (t.innerText||'').trim());
                      const pIdx = heads.findIndex(h => /unit price/i.test(h));
                      const rIdx = heads.findIndex(h => /remarks/i.test(h));
                      const row = g.querySelector('[id$="_data"] tr');
                      if (!row) continue;
                      const cells = [...row.querySelectorAll('td')];
                      if (pIdx >= 0 && cells[pIdx]) priceOk = setVal(cells[pIdx].querySelector('input'), price) || priceOk;
                      if (rIdx >= 0 && cells[rIdx]) remarkOk = setVal(cells[rIdx].querySelector('input,textarea'), remarks) || remarkOk;
                    }
                    return {priceOk, remarkOk};
                }""",
                [f"{payload.unit_price:.2f}", remarks],
            )
            logger.info("Stage B line item for %s: %s", payload.wo_po_number, filled)
        except Exception as exc:
            logger.warning("Stage B: could not set line item for %s: %s — leaving template values",
                           payload.wo_po_number, exc)

    # ------------------------------------------------------------------ recovery helpers
    async def _back_to_home(self) -> None:
        if not self.page:
            return
        try:
            await self.page.goto(settings.SYNERGIX_BASE_URL)
            await self.page.wait_for_load_state("networkidle")
        except Exception:
            logger.exception("Failed to return Synergix to home state")

    async def _screenshot(self, wo_po_number: str) -> None:
        if not self.page:
            return
        try:
            safe = wo_po_number.replace("/", "-")
            path = settings.LOGS_DIR / f"synergix_error_{safe}.png"
            await self.page.screenshot(path=str(path))
            logger.info("Saved error screenshot: %s", path)
        except Exception:
            logger.exception("Could not capture error screenshot")
