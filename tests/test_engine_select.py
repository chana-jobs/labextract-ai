"""Engine selection behind the drafts flow, on SYNTHETIC PDFs only. The OCR engine is faked, except in the last test,
which runs the real handoff package when LABEXTRACT_OCR_DIR points to it (skipped otherwise)."""
import os
from pathlib import Path

import pymupdf
import pytest

from extraction import engine_select, pdf_text
from shared.contracts import ApprovalStatus, ExistenceStatus, ExtractionStatus, Form, IssueReason, ProcessingStatus

FID = "dddddddddddddddddddddddddddddddd"
TEXT = ["First name: Dana", "Last name: Levi", "Israeli ID: 000000018", "Sender: Synthetic Clinic",
        "Commitment number: 000730001"]


def _text_page(doc):
    page = doc.new_page()
    for n, line in enumerate(TEXT):
        page.insert_text((72, 80 + 24 * n), line, fontsize=11)


def _scan_page(doc):
    source = pymupdf.open()
    _text_page(source)
    image = source[0].get_pixmap(dpi=100).tobytes("png")
    page = doc.new_page()
    page.insert_image(page.rect, stream=image)       # an image only: no text layer


def _pdf(*kinds) -> bytes:
    doc = pymupdf.open()
    for kind in kinds:
        {"text": _text_page, "scan": _scan_page}[kind](doc)
    return doc.tobytes()


class FakeEngine:
    def __init__(self):
        self.calls = []

    def extract_file(self, path, pages):
        self.calls.append((Path(path).suffix, pages))
        return {"engine_version": "fake", "pages": [{"page": p} for p in pages]}


def _fake_page_to_form(file_id, page, engine_version):
    doc = pymupdf.open()
    _text_page(doc)
    return pdf_text.extract_page(file_id, page["page"] - 1, doc[0])


@pytest.fixture
def fake_ocr(monkeypatch, tmp_path):
    engine = FakeEngine()
    (tmp_path / "token.json").write_text("{}")            # stands for cached page text
    monkeypatch.setattr(engine_select, "_ocr", lambda: (engine, _fake_page_to_form, tmp_path))
    return engine, tmp_path


def test_default_is_pdf_text_and_never_calls_ocr(monkeypatch, fake_ocr):
    monkeypatch.delenv("LABEXTRACT_ENGINE", raising=False)
    forms = engine_select.extract_document(FID, _pdf("text", "scan"), ".pdf")
    assert fake_ocr[0].calls == []
    assert forms[1].examinations[0].commitments == []     # nothing read on the page at all: no row


def test_form_without_commitment_number_keeps_its_other_fields(monkeypatch, fake_ocr):
    monkeypatch.delenv("LABEXTRACT_ENGINE", raising=False)
    doc = pymupdf.open()
    page = doc.new_page()
    for n, line in enumerate(TEXT[:-1]):                   # every line except the commitment number
        page.insert_text((72, 80 + 24 * n), line, fontsize=11)
    [form] = engine_select.extract_document(FID, doc.tobytes(), ".pdf")
    exam = form.examinations[0]
    assert [c.commitment_id for c in exam.commitments] == [f"{FID}-p1-c1"]
    assert exam.commitments[0].existence_status == ExistenceStatus.UNCERTAIN
    assert exam.commitments[0].number.extraction_status == ExtractionStatus.MISSING
    assert exam.israeli_id.normalized == "000000018" and exam.first_name.normalized == "Dana"
    assert any(i.reason == IssueReason.COMMITMENT_COUNT_UNCERTAIN for i in form.issues)


def test_failed_form_gets_no_commitment_row():
    failed = Form(form_id="f", file_id=FID, page_regions=[{"page": 0}], processing_status=ProcessingStatus.FAILED,
                  examinations=[])
    assert engine_select._ensure_commitment_row(failed) is failed


def test_hybrid_sends_only_scanned_pages_to_ocr(monkeypatch, fake_ocr):
    monkeypatch.setenv("LABEXTRACT_ENGINE", "hybrid")
    engine, cache = fake_ocr
    forms = engine_select.extract_document(FID, _pdf("text", "scan", "text"), ".pdf")
    assert engine.calls == [(".pdf", [2])]                 # 1-based page numbers, scanned page only
    assert [f.form_id for f in forms] == [f"{FID}-p1", f"{FID}-p2", f"{FID}-p3"]
    assert forms[1].examinations[0].commitments[0].number.normalized == "000730001"
    assert list(cache.iterdir()) == []                     # page text never kept after the document


def test_ocr_mode_reads_every_page(monkeypatch, fake_ocr):
    monkeypatch.setenv("LABEXTRACT_ENGINE", "ocr")
    forms = engine_select.extract_document(FID, _pdf("text", "scan"), ".pdf")
    assert fake_ocr[0].calls == [(".pdf", [1, 2])]
    assert len(forms) == 2


def test_hybrid_without_ocr_engine_fails_the_scanned_page_only(monkeypatch):
    monkeypatch.setenv("LABEXTRACT_ENGINE", "hybrid")

    def unavailable():
        raise engine_select.OcrUnavailable("not installed")
    monkeypatch.setattr(engine_select, "_ocr", unavailable)
    text, scan = engine_select.extract_document(FID, _pdf("text", "scan"), ".pdf")
    assert text.processing_status == ProcessingStatus.DONE
    assert text.examinations[0].commitments
    assert scan.processing_status == ProcessingStatus.FAILED
    assert [i.reason for i in scan.issues] == [IssueReason.TECHNICAL_FAILURE]


def test_unknown_engine_is_refused(monkeypatch):
    monkeypatch.setenv("LABEXTRACT_ENGINE", "magic")
    with pytest.raises(RuntimeError):
        engine_select.extract_document(FID, _pdf("text"), ".pdf")


SAMPLE = "samples/input/synth_00_L1_row_scan.pdf"


@pytest.mark.skipif(not (os.environ.get("LABEXTRACT_OCR_DIR")
                         and Path(os.environ["LABEXTRACT_OCR_DIR"], SAMPLE).is_file()),
                    reason="real OCR engine not configured (LABEXTRACT_OCR_DIR)")
def test_real_engine_reads_commitments_from_a_synthetic_scan(monkeypatch):
    monkeypatch.setenv("LABEXTRACT_ENGINE", "hybrid")
    content = Path(os.environ["LABEXTRACT_OCR_DIR"], SAMPLE).read_bytes()
    forms = engine_select.extract_document(FID, content, ".pdf")
    for form in forms:
        Form.model_validate(form.model_dump(mode="json"))
    commitments = [c for f in forms for c in f.examinations[0].commitments]
    assert commitments, "a scanned synthetic form must yield commitment rows"
    assert any(c.number.extraction_status == ExtractionStatus.COMPLETE for c in commitments)
    assert all(c.number.approval_status == ApprovalStatus.PENDING for c in commitments)
