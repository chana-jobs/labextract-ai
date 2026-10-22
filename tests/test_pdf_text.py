"""Rule-based extraction from the text layer of SYNTHETIC digital PDFs (built in the test; no real forms)."""
from pathlib import Path

import pymupdf
import pytest

from backend.excel_export import build_export
from extraction.pdf_text import ENGINE_VERSION, METHOD, extract_document
from shared.contracts import ApprovalStatus, ExtractionStatus, Form, IssueReason

FID = "cccccccccccccccccccccccccccccccc"
HEBREW_FONT = Path("C:/Windows/Fonts/arial.ttf")


def _pdf(pages: list[list[str]], font: Path | None = None) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        kwargs = {}
        if font is not None:
            page.insert_font(fontname="F0", fontfile=str(font))
            kwargs = {"fontname": "F0"}
        for n, text in enumerate(lines):
            page.insert_text((72, 80 + 24 * n), text, fontsize=11, **kwargs)
    data = doc.tobytes()
    doc.close()
    return data


FORM = [
    "Synthetic laboratory commitment form",
    "First name: Dana",
    "Last name: Levi",
    "Israeli ID: 000000018",
    "Sample received date: 23/08/2026",
    "Sender: Synthetic Clinic",
    "Central number: 000987 000988",
    "Commitment number: 000730001",
    "Commitment number: 000730002",
]


def _one(lines, **kwargs) -> Form:
    [form] = extract_document(FID, _pdf([lines], **kwargs), ".pdf")
    return Form.model_validate(form.model_dump())                  # contract-valid


def test_reads_all_seven_fields_with_evidence():
    form = _one(FORM)
    exam = form.examinations[0]
    assert (exam.first_name.normalized, exam.last_name.normalized, exam.sender.normalized) == \
           ("Dana", "Levi", "Synthetic Clinic")
    assert exam.israeli_id.normalized == "000000018"                 # leading zeros kept
    assert exam.sample_received_date.raw == "23/08/2026" and exam.sample_received_date.normalized == "2026-08-23"
    assert [c.normalized for c in exam.central_numbers] == ["000987", "000988"]
    assert [c.number.normalized for c in exam.commitments] == ["000730001", "000730002"]
    for fv in (exam.first_name, exam.israeli_id, exam.commitments[0].number):
        assert fv.extraction_status == ExtractionStatus.COMPLETE
        assert fv.approval_status == ApprovalStatus.PENDING           # extraction never approves
        [ev] = fv.evidence
        assert ev.page == 0 and ev.method == METHOD and ev.engine_version == ENGINE_VERSION
        assert ev.bbox and len(ev.bbox) == 4                          # exact line box from the text layer
    assert form.processing_status.value == "DONE" and form.issues == []


def test_commitments_have_stable_ids_and_existence_evidence():
    first, second = _one(FORM).examinations[0].commitments
    assert first.commitment_id == f"{FID}-p1-c1" and second.commitment_id == f"{FID}-p1-c2"
    assert first.existence_evidence_ref and first.origin_anchor == first.existence_evidence_ref
    assert _one(FORM).examinations[0].commitments == [first, second]   # deterministic


def test_value_on_the_next_line():
    exam = _one(["Synthetic form header line", "Sender:", "Synthetic Clinic North"]).examinations[0]
    assert exam.sender.normalized == "Synthetic Clinic North"
    assert exam.sender.dimensions.attribution.value == "MEDIUM"


def test_conflicting_readings_are_not_resolved():
    exam = _one(FORM + ["Israeli ID: 000000026"]).examinations[0]
    assert exam.israeli_id.extraction_status == ExtractionStatus.CONFLICT
    assert exam.israeli_id.normalized is None
    assert sorted(c.normalized for c in exam.israeli_id.candidates) == ["000000018", "000000026"]


def test_unreadable_is_not_absent():
    exam = _one(["Synthetic form header line", "Israeli ID: 00000018", "Sender:"]).examinations[0]
    assert exam.israeli_id.extraction_status == ExtractionStatus.UNCERTAIN    # 8 digits: never padded
    assert exam.israeli_id.normalized is None and exam.israeli_id.candidates[0].raw == "00000018"
    assert exam.sender.extraction_status == ExtractionStatus.UNCERTAIN       # label printed, no value
    assert exam.first_name.extraction_status == ExtractionStatus.MISSING     # no label at all


def test_ambiguous_date_is_only_a_candidate():
    exam = _one(["Synthetic form header line", "Sample received date: 11/08/2026"]).examinations[0]
    date = exam.sample_received_date
    assert date.extraction_status == ExtractionStatus.UNCERTAIN and date.normalized is None
    assert "DATE_AMBIGUOUS" in date.flags and date.candidates[0].normalized == "2026-08-11"


def test_commitment_label_without_number_keeps_an_empty_row():
    exam = _one(["Synthetic form header line", "Commitment number: unreadable", "Commitment number: 000730001"]
                ).examinations[0]
    empty, readable = exam.commitments
    assert empty.number.normalized is None and empty.number.extraction_status == ExtractionStatus.UNCERTAIN
    assert readable.number.normalized == "000730001"


def test_no_commitment_label_creates_no_row_and_an_issue():
    form = _one(["Synthetic form header line", "First name: Dana"])
    assert form.examinations[0].commitments == []
    assert [i.reason for i in form.issues] == [IssueReason.COMMITMENT_COUNT_UNCERTAIN]


def test_repeated_commitment_number_is_flagged_not_merged_silently():
    form = _one(["Synthetic form header line", "Commitment number: 000730001", "Commitment number: 000730001"])
    assert len(form.examinations[0].commitments) == 1
    assert [i.reason for i in form.issues] == [IssueReason.COMMITMENT_COUNT_UNCERTAIN]


@pytest.mark.parametrize("content,extension", [(_pdf([[]]), ".pdf"), (b"synthetic image bytes", ".png")])
def test_no_text_layer_extracts_nothing(content, extension):
    [form] = extract_document(FID, content, extension)
    exam = form.examinations[0]
    for fv in (exam.first_name, exam.israeli_id, exam.sender):
        assert fv.extraction_status == ExtractionStatus.UNCERTAIN and fv.normalized is None
        assert "no_text_layer" in fv.flags
    assert exam.commitments == [] and form.issues[0].reason == IssueReason.COMMITMENT_COUNT_UNCERTAIN


def test_one_form_per_page():
    forms = extract_document(FID, _pdf([FORM, FORM[:4]]), ".pdf")
    assert [f.form_id for f in forms] == [f"{FID}-p1", f"{FID}-p2"]
    assert forms[1].examinations[0].commitments == []


def test_export_of_extracted_values_is_empty_and_orange_until_approved():
    _, summary = build_export([_one(FORM)])
    assert summary.rows == 2 and summary.orange_cells == 14 and summary.is_partial


HEBREW_FORM = [
    "טופס התחייבות סינתטי",
    "שם פרטי: דנה",
    "שם משפחה: לוי",
    "ת.ז. 000000018",
    "תאריך קבלת הדגימה: 23/08/2026",
    "גורם שולח: מרפאה סינתטית",
    "מספר מרכזי: 000987",
    "מספר התחייבות: 000730001",
]


def _hebrew_rtl_pdf(lines: list[str]) -> bytes:
    """Typeset right-to-left, as a word processor or browser would."""
    doc = pymupdf.open()
    page = doc.new_page()
    css = "@font-face {font-family: heb; src: url(arial.ttf);} body {font-family: heb; font-size: 11pt;}"
    html = "".join(f'<p dir="rtl">{text}</p>' for text in lines)
    page.insert_htmlbox(pymupdf.Rect(72, 72, 520, 500), html, css=css,
                        archive=pymupdf.Archive(str(HEBREW_FONT.parent)))
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.skipif(not HEBREW_FONT.is_file(), reason="Hebrew-capable font not available")
def test_hebrew_text_in_visual_order_is_refused_not_misread():
    # glyphs placed left to right in logical order: the text layer comes back reversed
    [form] = extract_document(FID, _pdf([HEBREW_FORM], font=HEBREW_FONT), ".pdf")
    exam = form.examinations[0]
    assert exam.first_name.normalized is None and "text_order_unsupported" in exam.first_name.flags
    assert exam.commitments == [] and form.issues[0].reason == IssueReason.COMMITMENT_COUNT_UNCERTAIN


@pytest.mark.skipif(not HEBREW_FONT.is_file(), reason="Hebrew-capable font not available")
def test_hebrew_labels():
    [form] = extract_document(FID, _hebrew_rtl_pdf(HEBREW_FORM), ".pdf")
    exam = Form.model_validate(form.model_dump()).examinations[0]
    assert exam.first_name.normalized == "דנה" and exam.last_name.normalized == "לוי"
    assert exam.israeli_id.normalized == "000000018"
    assert exam.sample_received_date.normalized == "2026-08-23"
    assert exam.sender.normalized == "מרפאה סינתטית"
    assert [c.number.normalized for c in exam.commitments] == ["000730001"]
