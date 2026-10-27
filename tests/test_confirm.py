"""Second, independent source for values the OCR read alone (extraction/confirm.py, labx_overlay)."""
import pytest

from backend.readiness import apply_auto_approval
from extraction import confirm, labx_overlay
from shared.contracts import (
    ApprovalStatus, Candidate, Commitment, Dimensions, Examination, ExtractionStatus, FieldName, FieldValue, Form,
    ProcessingStatus, Readability, Attribution, Verification,
)


def _ocr(name, value, flags=("same_engine_reread",), others=("90942",)):
    """A value as the OCR proposes it: COMPLETE, not verified, with a note and a weaker competing candidate."""
    return FieldValue(field_name=name, raw=value, normalized=value, extraction_status=ExtractionStatus.COMPLETE,
                      dimensions=Dimensions(readability=Readability.OK, attribution=Attribution.STRONG,
                                            verification=Verification.NONE),
                      flags=list(flags), candidates=[Candidate(raw=value, normalized=value)] +
                      [Candidate(raw=o, normalized=o) for o in others])


def _form():
    blank = lambda n: FieldValue(field_name=n, extraction_status=ExtractionStatus.MISSING)  # noqa: E731
    exam = Examination(examination_id="f-e1", form_id="f", first_name=blank(FieldName.FIRST_NAME),
                       last_name=blank(FieldName.LAST_NAME), israeli_id=blank(FieldName.ISRAELI_ID),
                       sample_received_date=blank(FieldName.SAMPLE_RECEIVED_DATE),
                       sender=_ocr(FieldName.SENDER, 'מרכז רפואי ע"ש רמב"ם', flags=(), others=()),
                       central_numbers=[_ocr(FieldName.CENTRAL_NUMBERS, "939376")],
                       commitments=[Commitment(commitment_id="f-c1", examination_id="f-e1",
                                               number=_ocr(FieldName.COMMITMENT_NUMBER, "730578626"))])
    return Form(form_id="f", file_id="file", page_regions=[{"page": 0}], processing_status=ProcessingStatus.DONE,
                examinations=[exam])


@pytest.fixture
def auto_on(monkeypatch):
    monkeypatch.setenv("LABX_AUTO_APPROVE", "1")
    monkeypatch.setenv("LABX_MODE", "crops_first")


def test_ocr_value_alone_is_not_approved():
    exam = apply_auto_approval(_form()).examinations[0]
    assert exam.central_numbers[0].approval_status == ApprovalStatus.PENDING


def test_barcode_with_the_same_digits_makes_it_approvable(auto_on):
    form = confirm.with_barcodes(_form(), [{"text": "939376"}, {"text": "0730578626"}], 0)
    exam = apply_auto_approval(form).examinations[0]
    assert exam.central_numbers[0].approval_status == ApprovalStatus.AUTO_APPROVED
    assert exam.commitments[0].number.approval_status == ApprovalStatus.AUTO_APPROVED
    assert exam.central_numbers[0].dimensions.verification == Verification.INDEPENDENT


def test_barcode_with_other_digits_changes_nothing(auto_on):
    form = _form()
    assert confirm.with_barcodes(form, [{"text": "939377"}], 0) is form


def test_switch_off_confirms_nothing(monkeypatch):
    monkeypatch.setenv("LABX_AUTO_APPROVE", "0")
    monkeypatch.setenv("LABX_MODE", "crops_first")
    form = _form()
    assert confirm.with_barcodes(form, [{"text": "939376"}], 0) is form


def test_confirmation_waits_for_the_crops_first_switch(monkeypatch):
    """Auto-approve on (as on the site today) but crops-first off: barcodes do not approve anything yet."""
    monkeypatch.setenv("LABX_AUTO_APPROVE", "1")
    monkeypatch.setenv("LABX_MODE", "full_page")
    form = _form()
    assert confirm.with_barcodes(form, [{"text": "939376"}], 0) is form


def _apply(form, targets_readings, auto=True):
    exam = form.examinations[0]
    for target, readings in targets_readings:
        labx_overlay._confirm_ocr_value(form, exam, target, readings, 0, auto)
    return apply_auto_approval(Form.model_validate(form.model_dump())).examinations[0]


TWO_CLEAR = lambda text: [("a", text, "clear"), ("b", text, "clear")]  # noqa: E731


def test_two_models_reading_the_same_number_make_it_approvable():
    exam = _apply(_form(), [({"target": "central", "value": "939376"}, TWO_CLEAR("939376")),
                            ({"target": "commitment", "value": "730578626", "item_id": "c1"}, TWO_CLEAR("730578626"))])
    assert exam.central_numbers[0].approval_status == ApprovalStatus.AUTO_APPROVED
    assert exam.commitments[0].number.approval_status == ApprovalStatus.AUTO_APPROVED


def test_one_model_is_not_enough():
    exam = _apply(_form(), [({"target": "central", "value": "939376"}, [("a", "939376", "clear")])])
    assert exam.central_numbers[0].approval_status == ApprovalStatus.PENDING


def test_a_different_reading_flags_the_ocr_value_and_never_approves_it():
    exam = _apply(_form(), [({"target": "central", "value": "939376"}, [("a", "939376", "clear"),
                                                                         ("b", "939378", "clear")])])
    fv = exam.central_numbers[0]
    assert fv.approval_status == ApprovalStatus.PENDING and "second_reader_disagrees" in fv.flags
    assert fv.normalized == "939376"                         # the OCR's decision itself is kept


def test_sender_written_shorter_is_the_same_institution(monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "ocr_engine"))
    sys.modules.pop("labextract_ocr.institutions", None)
    exam = _apply(_form(), [({"target": "sender", "value": 'מרכז רפואי ע"ש רמב"ם'}, TWO_CLEAR('רמב"ם'))])
    assert exam.sender.approval_status == ApprovalStatus.AUTO_APPROVED


def test_a_different_institution_is_not_confirmed(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "ocr_engine"))
    exam = _apply(_form(), [({"target": "sender", "value": 'מרכז רפואי ע"ש רמב"ם'}, TWO_CLEAR("כללית"))])
    assert exam.sender.approval_status == ApprovalStatus.PENDING and "second_reader_disagrees" in exam.sender.flags


def test_auto_approve_off_marks_nothing():
    exam = _apply(_form(), [({"target": "central", "value": "939376"}, TWO_CLEAR("939376"))], auto=False)
    assert exam.central_numbers[0].approval_status == ApprovalStatus.PENDING


def test_the_sender_is_never_sent_for_confirmation():
    """A crop can confirm a name's text but not that it is the sender (part 2 p13: the fund 'מכבי')."""
    from extraction import engine_select
    page = {"fields": {"sender": {"status": "proposed", "value": "מכבי",
                                  "candidates": [{"value": "מכבי", "bbox": [1, 1, 50, 20]}]},
                       "central_numbers": {"status": "proposed", "values": ["939376"],
                                           "candidates": [{"value": "939376", "bbox": [1, 30, 60, 50]}]}}}
    assert [m["target"] for _, m in engine_select.confirm_value_boxes(page)] == ["central"]


# ---------------------------------------------------------------- commitment from an unlabeled Code 39 barcode
from extraction import engine_select  # noqa: E402

PAGE = {"fields": {"commitment_numbers": {"status": "missing", "items": [], "candidates": []},
                   "central_numbers": {"status": "proposed", "values": ["948257"], "candidates": []}},
        "barcodes": [{"text": "282574736601", "format": "Code 128"}, {"text": "82857", "format": "Code 39"},
                     {"text": "948257", "format": "Code 128"}]}


def test_only_a_code39_barcode_is_taken_as_the_commitment():
    """part 2 p16: Code 128 = the central sticker and a hospital number, Code 39 = the commitment."""
    assert engine_select.barcode_commitments(PAGE, []) == ["82857"]


def test_a_central_number_is_never_taken_even_in_code39():
    page = dict(PAGE, barcodes=[{"text": "948257", "format": "Code 39"}])
    assert engine_select.barcode_commitments(page, []) == []


def test_the_printed_letter_is_kept():
    """part 2 p15: the barcode encodes 132797, the commitment is printed X132797."""
    page = dict(PAGE, barcodes=[{"text": "132797", "format": "Code 39"}])
    toks = [{"text": "X132797", "bbox": [0, 0, 10, 10]}]
    assert engine_select.barcode_commitments(page, toks) == ["X132797"]


def test_no_barcode_commitment_when_the_ocr_found_one():
    page = dict(PAGE, fields={**PAGE["fields"], "commitment_numbers": {"status": "proposed", "values": ["1"]}})
    assert engine_select.barcode_commitments(page, []) == []


def test_barcode_commitment_is_filled_flagged_and_never_approved(auto_on):
    form = _form()
    form.examinations[0].commitments = []
    form = confirm.with_barcode_commitments(form, ["82857"], 0)
    form = confirm.with_barcodes(form, [{"text": "82857"}], 0)          # its own barcode must not confirm it
    [c] = apply_auto_approval(form).examinations[0].commitments
    assert c.number.normalized == "82857" and c.number.approval_status == ApprovalStatus.PENDING
    assert confirm.BARCODE_ONLY in c.number.flags


def test_a_weak_central_candidate_does_not_block_the_commitment():
    """The OCR lists every number of the page as a weak central candidate, the commitment too (part 2 p16)."""
    page = dict(PAGE, fields={**PAGE["fields"], "central_numbers": {
        "status": "proposed", "values": ["948257"],
        "candidates": [{"value": "948257", "tier": "strong"}, {"value": "82857", "tier": "weak"}]}})
    assert engine_select.barcode_commitments(page, []) == ["82857"]
