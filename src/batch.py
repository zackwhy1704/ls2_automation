"""Telegram-free batch pipeline: intake -> extract -> validate -> dedup -> auto-submit -> email report.

The client reviews in Synergix (and the emailed report), not in Telegram. So there is no approval gate:
every WO that passes validation and a clean (NOT_DUPLICATE) dedup is auto-submitted as a Service
Quotation. The double-billing guard stays: a DUPLICATE is skipped and an UNCERTAIN dedup is left as
NEEDS_REVIEW (never auto-submitted). After the batch, a summary email lists every outcome so the team
knows what to spot-check / fix in Synergix.

Auto-submit is gated by DRY_RUN: with DRY_RUN=true the draft is created + filled but not submitted
(safe to run for real while building trust); set DRY_RUN=false to actually submit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from src import db
from src.email_ingestor import ingest_dir, ingest_file
from src.extractor import ExtractionError, extract_from_pdf, extract_from_text
from src.models import WOPayload, WOStatus
from src.synergix_driver import DedupResult, SynergixDriver
from src.validator import build_remarks, resolve_project_code, validate

logger = logging.getLogger(__name__)


@dataclass
class WOOutcome:
    """One WO's result, for the batch report."""

    wo_po_number: str
    status: WOStatus
    detail: str = ""
    quotation_id: str | None = None
    source: str = ""


@dataclass
class BatchResult:
    outcomes: list[WOOutcome] = field(default_factory=list)

    def by_status(self, status: WOStatus) -> list[WOOutcome]:
        return [o for o in self.outcomes if o.status is status]


async def process_payload(payload: WOPayload, synergix: SynergixDriver) -> WOOutcome:
    """Run one extracted WO through validate -> dedup -> auto-submit. Never raises."""
    wo = payload.wo_po_number
    try:
        errors = validate(payload)
        if errors:
            detail = "; ".join(errors)
            logger.info("WO %s INVALID: %s", wo, detail)
            await db.upsert_payload(payload, WOStatus.INVALID, error=detail)
            return WOOutcome(wo, WOStatus.INVALID, detail, source=payload.source_path)

        project_code = resolve_project_code(payload.job_sheet_number)
        remarks = build_remarks(payload)

        # Double-billing guard (fail-safe). Synergix — not the council's list — is the source of truth.
        dedup = await synergix.check_duplicate(payload)
        if dedup is DedupResult.DUPLICATE:
            logger.info("WO %s DUPLICATE — already in Synergix, skipping", wo)
            await db.upsert_payload(payload, WOStatus.DUPLICATE, project_code=project_code, remarks=remarks)
            return WOOutcome(wo, WOStatus.DUPLICATE, "already invoiced in Synergix", source=payload.source_path)
        if dedup is DedupResult.UNCERTAIN:
            msg = "dedup inconclusive — verify manually it is NOT already invoiced before billing"
            logger.warning("WO %s NEEDS_REVIEW: %s", wo, msg)
            await db.upsert_payload(payload, WOStatus.NEEDS_REVIEW, project_code=project_code,
                                    remarks=remarks, error=msg)
            return WOOutcome(wo, WOStatus.NEEDS_REVIEW, msg, source=payload.source_path)

        # Clean: create + (unless DRY_RUN) submit the quotation.
        await db.upsert_payload(payload, WOStatus.APPROVED, project_code=project_code, remarks=remarks)
        result = await synergix.write(payload)
        await db.set_status(wo, result.status, error=result.detail or None)
        quo = None
        if result.detail:
            import re
            m = re.search(r"QUO\d+", result.detail)
            quo = m.group(0) if m else None
        logger.info("WO %s -> %s (%s)", wo, result.status.value, result.detail)
        return WOOutcome(wo, result.status, result.detail, quotation_id=quo, source=payload.source_path)
    except Exception as exc:
        logger.exception("WO %s FAILED", wo)
        await db.set_status(wo, WOStatus.FAILED, error=str(exc))
        return WOOutcome(wo, WOStatus.FAILED, str(exc), source=payload.source_path)


async def run_batch_from_pdfs(pdf_paths: list[str]) -> BatchResult:
    """Process a list of WO PDFs (already downloaded/ingested) end to end."""
    result = BatchResult()
    synergix = SynergixDriver()
    try:
        await synergix.start()
        for path in pdf_paths:
            name = Path(path).name
            try:
                payload = extract_from_pdf(path)
                await db.upsert_scraped(payload.wo_po_number, payload.source_path)
            except ExtractionError as exc:
                logger.warning("Extraction failed for %s: %s", name, exc)
                result.outcomes.append(WOOutcome(name, WOStatus.INVALID, f"extraction failed: {exc}", source=path))
                continue
            result.outcomes.append(await process_payload(payload, synergix))
    finally:
        await synergix.close()
    return result


async def run_batch_from_tcms(limit: int | None = None) -> BatchResult:
    """Scrape TCMS and process each un-invoiced WO in a STREAMING pipeline.

    For each WO id from the un-invoiced list we: (1) dedup on the WO-PO FIRST — a DUPLICATE is skipped
    without the expensive PDF download; (2) otherwise download the PDF, extract, and process. Results
    stream in one WO at a time, so a mid-run failure still leaves everything-so-far done and reported.
    `limit` caps how many WOs are handled (for sampling); None = all.
    """
    from src.tcms_scraper import TCMSScraper

    result = BatchResult()
    synergix = SynergixDriver()
    try:
        await synergix.start()
        async with TCMSScraper() as scraper:
            await scraper.login()
            wo_ids = await scraper.list_uninvoiced()
            if limit is not None:
                wo_ids = wo_ids[:limit]
                logger.info("Batch capped to first %d of the un-invoiced WOs", len(wo_ids))

            for wo_id in wo_ids:
                try:
                    # Cheap dedup on the WO-PO before downloading anything.
                    probe = WOPayload(
                        wo_po_number=wo_id, job_sheet_number="0", service_location="-",
                        nature_of_work="-", job_date=_today(), prepared_by="-", gl_number="-",
                        quantity=1.0, unit_price=1.0, source_path="-",
                    )
                    dedup = await synergix.check_duplicate(probe)
                    if dedup is DedupResult.DUPLICATE:
                        logger.info("WO %s DUPLICATE (pre-download) — skipping", wo_id)
                        result.outcomes.append(
                            WOOutcome(wo_id, WOStatus.DUPLICATE, "already invoiced in Synergix"))
                        continue

                    # Not a confirmed duplicate: download + extract + full process (which re-dedups).
                    path = await scraper.download_pdf(wo_id)
                    try:
                        payload = extract_from_pdf(path)
                        await db.upsert_scraped(payload.wo_po_number, payload.source_path)
                    except ExtractionError as exc:
                        logger.warning("Extraction failed for %s: %s", wo_id, exc)
                        result.outcomes.append(
                            WOOutcome(wo_id, WOStatus.INVALID, f"extraction failed: {exc}", source=path))
                        continue
                    result.outcomes.append(await process_payload(payload, synergix))
                except Exception as exc:
                    logger.exception("Failed handling WO %s — recording FAILED, continuing", wo_id)
                    result.outcomes.append(WOOutcome(wo_id, WOStatus.FAILED, str(exc)))
    finally:
        await synergix.close()
    return result


def _today():
    """Today's date (module-level so tests can monkeypatch if needed)."""
    from datetime import date
    return date.today()


async def run_batch_from_emails(eml_source: str) -> BatchResult:
    """Ingest .msg/.eml/.pdf (file or dir) then process each WO unit end to end."""
    items = ingest_dir(eml_source) if Path(eml_source).is_dir() else ingest_file(eml_source)
    result = BatchResult()
    synergix = SynergixDriver()
    try:
        await synergix.start()
        for wo in items:
            label = wo.attachment_name or wo.subject or wo.primary_source_path
            try:
                payload = extract_from_pdf(wo.pdf_path) if wo.has_pdf else \
                    extract_from_text(wo.body_text, source_path=wo.source_email_path)
                await db.upsert_scraped(payload.wo_po_number, payload.source_path)
            except ExtractionError as exc:
                logger.warning("Extraction failed for %r: %s", label, exc)
                result.outcomes.append(WOOutcome(str(label), WOStatus.INVALID, f"extraction failed: {exc}"))
                continue
            result.outcomes.append(await process_payload(payload, synergix))
    finally:
        await synergix.close()
    return result
