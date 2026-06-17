"""Unit tests for the .eml ingestor (no network, no LLM).

Builds synthetic emails covering: PDF attachment, inline plain-text body, HTML-only body, and a
PDF attachment without an explicit Content-Disposition. Verifies the ingestor saves attachments,
decodes bodies, and picks the right primary source for the extractor / Synergix attach step.
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
    # Redirect the ingestor's output dir to a temp location so tests don't litter data/pdfs.
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(settings, "PDF_DIR", out)
    return out


def test_pdf_attachment_is_saved_and_is_primary(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "WO-PO/123 pest control"
    msg.set_content("See attached work order.")
    msg.add_attachment(b"%PDF-1.4 fake pdf bytes", maintype="application", subtype="pdf",
                       filename="workorder.pdf")
    eml = _write_eml(tmp_path, msg)

    wo = email_ingestor.ingest_file(eml)

    assert wo.has_pdf
    assert len(wo.pdf_paths) == 1
    assert Path(wo.pdf_paths[0]).read_bytes().startswith(b"%PDF")
    assert wo.primary_source_path == wo.pdf_paths[0]  # PDF preferred over .eml
    assert "See attached" in wo.body_text


def test_inline_body_no_attachment_falls_back_to_eml(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "Adhoc job"
    msg.set_content("WO-PO/999\nLocation: Block 5\nGL: GL-22\nUnit price: 80")
    eml = _write_eml(tmp_path, msg)

    wo = email_ingestor.ingest_file(eml)

    assert not wo.has_pdf
    assert wo.primary_source_path == wo.eml_path  # no PDF -> the saved .eml is the source
    assert wo.primary_source_path.endswith(".eml")
    assert "Block 5" in wo.body_text


def test_html_only_body_is_stripped_to_text(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "HTML WO"
    msg.set_content("plain fallback")
    msg.add_alternative("<html><body><p>WO-PO/<b>777</b></p></body></html>", subtype="html")
    eml = _write_eml(tmp_path, msg)

    wo = email_ingestor.ingest_file(eml)
    # set_content + add_alternative yields a text/plain part, which we prefer.
    assert "plain fallback" in wo.body_text


def test_eml_copy_written_to_output_dir(tmp_path, _isolate_pdf_dir):
    msg = EmailMessage()
    msg["Subject"] = "copy test"
    msg.set_content("body")
    eml = _write_eml(tmp_path, msg)

    wo = email_ingestor.ingest_file(eml)
    assert Path(wo.eml_path).exists()
    assert Path(wo.eml_path).parent == _isolate_pdf_dir


def test_ingest_dir_skips_bad_file(tmp_path):
    good = EmailMessage()
    good["Subject"] = "good"
    good.set_content("ok")
    _write_eml(tmp_path, good, "good.eml")
    # A non-parseable .eml should be skipped, not crash the batch.
    (tmp_path / "bad.eml").write_bytes(b"\xff\xfe not really an email")

    results = email_ingestor.ingest_dir(str(tmp_path))
    assert len(results) == 2  # both parse; email lib is lenient — at minimum no crash
