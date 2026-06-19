"""Unit tests for the email ingestor (no network, no LLM).

Covers the .eml path with synthetic messages: PDF attachment, multiple PDFs (multi-WO email),
inline body only, HTML body, and image-only attachments (signatures, no WO). The .msg path is
exercised by the real-sample smoke test in test_msg_samples.py when the samples are present.
"""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from config import settings
from src import email_ingestor


def _write_eml(tmp_path: Path, msg: EmailMessage, name: str = "wo.eml") -> str:
    p = tmp_path / name
    p.write_bytes(msg.as_bytes())
    return str(p)


@pytest.fixture(autouse=True)
def _isolate_pdf_dir(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(settings, "PDF_DIR", out)
    return out


def test_single_pdf_attachment_one_wo(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "WO-PO/123 pest control"
    msg.set_content("See attached work order.")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="000123.pdf")
    wos = email_ingestor.ingest_file(_write_eml(tmp_path, msg))

    assert len(wos) == 1
    wo = wos[0]
    assert wo.has_pdf
    assert Path(wo.pdf_path).read_bytes().startswith(b"%PDF")
    assert wo.primary_source_path == wo.pdf_path
    assert wo.attachment_name == "000123.pdf"


def test_multiple_pdfs_yield_one_wo_each(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "3 Work Orders"
    msg.set_content("Kindly invoice the following WOs.")
    for n in ("000059981", "000059982", "000059983"):
        msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename=f"{n}.pdf")
    wos = email_ingestor.ingest_file(_write_eml(tmp_path, msg))

    assert len(wos) == 3
    assert all(w.has_pdf for w in wos)
    assert {w.attachment_name for w in wos} == {"000059981.pdf", "000059982.pdf", "000059983.pdf"}


def test_image_attachments_are_not_treated_as_wos(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "signature only"
    msg.set_content("body with a signature image")
    msg.add_attachment(b"\x89PNG fake", maintype="image", subtype="png", filename="image001.png")
    wos = email_ingestor.ingest_file(_write_eml(tmp_path, msg))

    # No PDF -> one body-source WO; the image is ignored, not turned into a WO.
    assert len(wos) == 1
    assert not wos[0].has_pdf
    assert wos[0].primary_source_path == wos[0].source_email_path


def test_inline_body_no_attachment_falls_back_to_email(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "Adhoc job"
    msg.set_content("WO-PO/999\nLocation: Block 5")
    wos = email_ingestor.ingest_file(_write_eml(tmp_path, msg))

    assert len(wos) == 1
    assert not wos[0].has_pdf
    assert wos[0].primary_source_path.endswith(".eml")
    assert "Block 5" in wos[0].body_text


def test_saved_email_copy_in_output_dir(tmp_path, _isolate_pdf_dir):
    msg = EmailMessage()
    msg["Subject"] = "copy test"
    msg.set_content("body")
    wos = email_ingestor.ingest_file(_write_eml(tmp_path, msg))
    assert Path(wos[0].source_email_path).parent == _isolate_pdf_dir


def test_unsupported_suffix_raises(tmp_path):
    p = tmp_path / "notanemail.txt"
    p.write_text("hello")
    with pytest.raises(ValueError):
        email_ingestor.ingest_file(str(p))


def test_ingest_dir_picks_supported_skips_others(tmp_path, _isolate_pdf_dir):
    msg = EmailMessage()
    msg["Subject"] = "good"
    msg.set_content("ok")
    _write_eml(tmp_path, msg, "good.eml")
    (tmp_path / "wo.pdf").write_bytes(b"%PDF-1.4 fake")       # bare PDF -> ingested
    (tmp_path / "ignore.txt").write_text("not a WO")          # skipped
    (tmp_path / ".hidden.pdf").write_bytes(b"%PDF-1.4 fake")  # hidden -> skipped

    results = email_ingestor.ingest_dir(str(tmp_path))
    # one .eml + one .pdf = 2 units; .txt and the hidden file are ignored.
    assert len(results) == 2


def test_bare_pdf_is_ingested_as_one_wo(tmp_path, _isolate_pdf_dir):
    p = tmp_path / "000076624.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    wos = email_ingestor.ingest_file(str(p))
    assert len(wos) == 1
    wo = wos[0]
    assert wo.has_pdf and wo.primary_source_path == wo.pdf_path
    assert wo.attachment_name == "000076624.pdf"
    assert Path(wo.pdf_path).read_bytes().startswith(b"%PDF")


def test_ingest_dir_recurses_into_subdirs(tmp_path, _isolate_pdf_dir):
    (tmp_path / "JBTC").mkdir()
    (tmp_path / "SKTC").mkdir()
    (tmp_path / "JBTC" / "76624.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "SKTC" / "000059174.pdf").write_bytes(b"%PDF-1.4 fake")

    results = email_ingestor.ingest_dir(str(tmp_path))
    assert len(results) == 2
    # non-recursive sees nothing at the top level
    assert email_ingestor.ingest_dir(str(tmp_path), recursive=False) == []


def test_empty_file_raises(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        email_ingestor.ingest_file(str(p))
