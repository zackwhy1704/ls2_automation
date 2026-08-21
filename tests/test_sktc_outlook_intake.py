"""Tests for the Outlook COM intake adapter's decision logic.

The COM calls themselves need a live Outlook and are not unit-testable, but the two rules that
decide whether a real WO gets billed or silently lost are pure functions, and both were derived from
real mail in this mailbox:

  - looks_like_wo_pdf: SKTC names WO attachments after the WO number ("000061116.pdf"). The same
    senders also attach job sheet photo reports, invoices and (observed) a WhatsApp group chat
    screenshot. Too loose and junk reaches the extractor; too strict and a real WO is dropped.
  - _sender_allowed: must FAIL CLOSED on an empty allowlist, or a forgotten .env line before go-live
    would ingest WO PDFs from anyone.
"""
from __future__ import annotations

import pytest

from src.sktc_outlook_intake import _sender_allowed, looks_like_wo_pdf


@pytest.mark.parametrize("name", [
    "000061116.pdf",          # real: WO-PO/000061116
    "000062136.pdf",          # real
    "000062024.pdf",          # real, arrived alongside a second WO PDF in one email
    "000060075.pdf",          # real
    "WO-PO000060666.pdf",     # plausible alternate spelling, harmless to accept
    "wo-po_000061116.PDF",    # case and separator tolerance
])
def test_real_wo_attachment_names_are_accepted(name):
    assert looks_like_wo_pdf(name), f"{name} is a real WO attachment and must be ingested"


@pytest.mark.parametrize("name", [
    # All observed in real mail from @sktc.sg senders:
    "25973-  PIGEON TREATMENT 24 JULY  2026 @ 227C COMPASSVALE DRIVE.pdf",  # job sheet photo report
    "LS2 Whatsapp Group Chat SS - 6 July 2026 to 17 July 2026.pdf",         # chat screenshot
    "Jenny Ang_WO-PO000060666_SIN0006063_Jul 26.pdf",                       # an INVOICE, not a WO
    "signature.pdf",
    "000061116.docx",         # right name, wrong type
    "12345.pdf",              # too short to be a WO number
    "",
])
def test_non_wo_attachments_are_rejected(name):
    assert not looks_like_wo_pdf(name), f"{name} is not a WO and must not be auto-ingested"


def test_invoice_named_pdf_is_rejected_despite_containing_a_wo_number():
    """The invoice filename embeds a WO number, so a naive 'contains digits' rule would accept it and
    bill an invoice as a work order. The rule matches the whole stem for exactly this reason."""
    assert not looks_like_wo_pdf("Jenny Ang_WO-PO000060666_SIN0006063_Jul 26.pdf")


def test_sender_allowlist_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr("src.sktc_outlook_intake.settings.SKTC_SENDER_ALLOWLIST", "", raising=False)
    assert _sender_allowed("stephanie.lim@sktc.sg") is False


def test_sender_allowlist_matches_domain_substring(monkeypatch):
    monkeypatch.setattr("src.sktc_outlook_intake.settings.SKTC_SENDER_ALLOWLIST", "@sktc.sg",
                        raising=False)
    # The three real SKTC senders seen in this mailbox.
    assert _sender_allowed("stephanie.lim@sktc.sg")
    assert _sender_allowed("jermaine.lim@sktc.sg")
    assert _sender_allowed("JENNY.ANG@SKTC.SG")
    # Internal LS2 forwards attach invoices and photo reports, not WOs — they must not qualify.
    assert not _sender_allowed("itnah@ls2.sg")
    assert not _sender_allowed("kumar-project@ls2.sg")
