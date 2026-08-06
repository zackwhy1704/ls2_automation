"""Unit tests for the SKTC folder intake adapter (no network, no LLM, no real OneDrive).

Covers: clean ingest, message-id dedup, unallowlisted sender, orphaned PDF (no sidecar), an
unstable/mid-sync "file", a multi-PDF sidecar (one email, two attachments), and an unreachable
intake folder raising loudly instead of reporting "0 WOs".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import settings
from src import sktc_folder_intake as intake


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    sktc_folder = tmp_path / "sktc_intake"
    sktc_folder.mkdir()
    monkeypatch.setattr(settings, "INCOMING_EMAIL_DIR", incoming)
    monkeypatch.setattr(settings, "SKTC_INTAKE_FOLDER", str(sktc_folder))
    monkeypatch.setattr(settings, "SKTC_INTAKE_PROCESSED_SUBFOLDER", "processed")
    monkeypatch.setattr(settings, "SKTC_INTAKE_SIDECAR_WAIT_SECONDS", 30)
    monkeypatch.setattr(settings, "SKTC_SENDER_ALLOWLIST", "jenny.ang@sktc.sg")
    # The stability check sleeps _STABILITY_CHECK_DELAY_S between two size reads — make that instant
    # in tests. A stale/zero-byte file is still caught because the size comparison itself is real.
    monkeypatch.setattr(intake.time, "sleep", lambda *_: None)
    return sktc_folder, incoming


def _write_pdf(folder: Path, name: str, content: bytes = b"%PDF-1.4 fake pdf content") -> Path:
    p = folder / name
    p.write_bytes(content)
    return p


def _write_sidecar(folder: Path, name: str, *, message_id: str, sender: str, attachments: list[str]) -> Path:
    meta = folder / "_meta"
    meta.mkdir(exist_ok=True)
    p = meta / name
    p.write_text(json.dumps({
        "message_id": message_id,
        "sender": sender,
        "subject": "WO-PO/000059174",
        "received_at": "2026-08-06T09:00:00+08:00",
        "attachments": attachments,
    }))
    return p


def test_clean_ingest_moves_pdf_and_archives_both_files(_isolate):
    folder, incoming = _isolate
    _write_pdf(folder, "000059174.pdf")
    _write_sidecar(folder, "msg1.json", message_id="<msg1@sktc.sg>", sender="jenny.ang@sktc.sg",
                    attachments=["000059174.pdf"])

    saved, review = intake.poll_folder_once()

    assert len(saved) == 1
    assert Path(saved[0]).parent == incoming
    assert Path(saved[0]).read_bytes().startswith(b"%PDF")
    assert review == []
    # Source files archived, not left in place or deleted.
    assert not (folder / "000059174.pdf").exists()
    assert (folder / "processed" / "000059174.pdf").exists()
    assert (folder / "processed" / "_meta" / "msg1.json").exists()


def test_duplicate_message_id_not_reingested(_isolate):
    folder, incoming = _isolate
    _write_pdf(folder, "000059174.pdf")
    _write_sidecar(folder, "msg1.json", message_id="<msg1@sktc.sg>", sender="jenny.ang@sktc.sg",
                    attachments=["000059174.pdf"])
    saved1, _ = intake.poll_folder_once()
    assert len(saved1) == 1

    # Simulate the same email landing again (e.g. a re-sync): same PDF + sidecar reappear.
    _write_pdf(folder, "000059174.pdf")
    _write_sidecar(folder, "msg1_again.json", message_id="<msg1@sktc.sg>", sender="jenny.ang@sktc.sg",
                    attachments=["000059174.pdf"])
    saved2, review2 = intake.poll_folder_once()

    assert saved2 == []
    assert review2 == []


def test_unallowlisted_sender_routed_to_review_not_ingested(_isolate):
    folder, incoming = _isolate
    _write_pdf(folder, "000059174.pdf")
    _write_sidecar(folder, "msg1.json", message_id="<msg1@evil.example>", sender="not-sktc@evil.example",
                    attachments=["000059174.pdf"])

    saved, review = intake.poll_folder_once()

    assert saved == []
    assert len(review) == 1
    assert review[0].identifier == "000059174.pdf"
    assert "not-sktc@evil.example" in review[0].reason
    assert list(incoming.iterdir()) == []


def test_orphaned_pdf_with_no_sidecar_after_wait_routed_to_review(_isolate, monkeypatch):
    folder, incoming = _isolate
    monkeypatch.setattr(settings, "SKTC_INTAKE_SIDECAR_WAIT_SECONDS", 0)  # don't actually wait in test
    pdf = _write_pdf(folder, "000059174.pdf")
    # Backdate mtime so the "age >= wait window" check fires immediately.
    import os
    old = pdf.stat().st_mtime - 60
    os.utime(pdf, (old, old))

    saved, review = intake.poll_folder_once()

    assert saved == []
    assert len(review) == 1
    assert review[0].identifier == "000059174.pdf"
    assert "no sidecar" in review[0].reason
    assert list(incoming.iterdir()) == []


def test_pdf_within_sidecar_wait_window_is_skipped_not_orphaned(_isolate, monkeypatch):
    folder, incoming = _isolate
    monkeypatch.setattr(settings, "SKTC_INTAKE_SIDECAR_WAIT_SECONDS", 3600)  # generous window
    _write_pdf(folder, "000059174.pdf")  # freshly written -> age ~0s, well within the window

    saved, review = intake.poll_folder_once()

    assert saved == []
    assert review == []  # not yet orphaned -- just still waiting


def test_zero_byte_placeholder_not_treated_as_ready(_isolate):
    folder, incoming = _isolate
    _write_pdf(folder, "000059174.pdf", content=b"")  # simulates a OneDrive cloud-only placeholder
    _write_sidecar(folder, "msg1.json", message_id="<msg1@sktc.sg>", sender="jenny.ang@sktc.sg",
                    attachments=["000059174.pdf"])

    saved, review = intake.poll_folder_once()

    assert saved == []
    assert review == []  # not orphaned either -- just not stable yet, will retry next poll
    assert (folder / "000059174.pdf").exists()  # left in place, untouched


def test_multi_pdf_sidecar_both_pdfs_ingested_and_linked_to_one_message(_isolate):
    folder, incoming = _isolate
    _write_pdf(folder, "000059174.pdf")
    _write_pdf(folder, "000059175.pdf")
    _write_sidecar(folder, "msg1.json", message_id="<msg1@sktc.sg>", sender="jenny.ang@sktc.sg",
                    attachments=["000059174.pdf", "000059175.pdf"])

    saved, review = intake.poll_folder_once()

    assert len(saved) == 2
    assert {Path(p).name for p in saved} == {"000059174.pdf", "000059175.pdf"}
    assert review == []
    # Both PDFs consumed the SAME sidecar; it's archived once, not duplicated/missing.
    assert (folder / "processed" / "_meta" / "msg1.json").exists()


def test_unreachable_folder_raises_loudly_not_silent_zero(_isolate, monkeypatch):
    monkeypatch.setattr(settings, "SKTC_INTAKE_FOLDER", "/definitely/does/not/exist/anywhere")

    with pytest.raises(RuntimeError, match="unreachable"):
        intake.poll_folder_once()


def test_missing_folder_config_raises(_isolate, monkeypatch):
    monkeypatch.setattr(settings, "SKTC_INTAKE_FOLDER", "")

    with pytest.raises(RuntimeError, match="SKTC_INTAKE_FOLDER"):
        intake.poll_folder_once()


def test_empty_allowlist_fails_closed_with_warning(_isolate, monkeypatch, caplog):
    folder, incoming = _isolate
    monkeypatch.setattr(settings, "SKTC_SENDER_ALLOWLIST", "")
    _write_pdf(folder, "000059174.pdf")
    _write_sidecar(folder, "msg1.json", message_id="<msg1@anywhere.example>", sender="anyone@anywhere.example",
                    attachments=["000059174.pdf"])

    with caplog.at_level("WARNING"):
        saved, review = intake.poll_folder_once()

    assert len(saved) == 0  # fails closed when unconfigured — never silently trusts every sender
    assert len(review) == 1
    assert "SKTC_SENDER_ALLOWLIST" in review[0].reason
    assert any("SKTC_SENDER_ALLOWLIST is empty" in r.message for r in caplog.records)
