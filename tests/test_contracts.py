import pytest
from pydantic import ValidationError

from extraction.mock_engine import make_mock_result
from shared.contracts import (
    ApprovalStatus, CommitmentStatus, EngineMode, EngineResult, ExtractionStatus, FieldName,
    FieldValue, IssueReason, ProcessingStatus, export_cell, export_central_cell,
    derive_commitment_status,
)


def _approve(fv: FieldValue) -> FieldValue:
    return fv.model_copy(update={"approval_status": ApprovalStatus.HUMAN_APPROVED})


# --- P1 1.2: the engine never approves; AUTO_APPROVED needs a rule + policy ---------------
def test_mock_engine_never_approves():
    exam = make_mock_result().form.examinations[0]
    for f in [exam.first_name, exam.israeli_id, *exam.central_numbers]:
        assert f.extraction_status == ExtractionStatus.COMPLETE
        assert f.approval_status == ApprovalStatus.PENDING


def test_auto_approved_requires_rule_and_policy():
    with pytest.raises(ValidationError):
        FieldValue(field_name=FieldName.ISRAELI_ID, raw="1", normalized="1",
                   extraction_status=ExtractionStatus.COMPLETE, approval_status=ApprovalStatus.AUTO_APPROVED)
    ok = FieldValue(field_name=FieldName.ISRAELI_ID, raw="1", normalized="1",
                    extraction_status=ExtractionStatus.COMPLETE, approval_status=ApprovalStatus.AUTO_APPROVED,
                    auto_approval_rule_id="r1", policy_version="p0")
    assert ok.is_approved_with_value


def test_cannot_approve_partial_or_empty():
    with pytest.raises(ValidationError):
        FieldValue(field_name=FieldName.COMMITMENT_NUMBER, raw="872?41596", partial=True, unknown_positions=[3],
                   extraction_status=ExtractionStatus.PARTIAL, approval_status=ApprovalStatus.HUMAN_APPROVED)


def test_partial_needs_unknown_positions_and_confirmed_absent_needs_missing():
    with pytest.raises(ValidationError):
        FieldValue(field_name=FieldName.COMMITMENT_NUMBER, raw="87?", partial=True,
                   extraction_status=ExtractionStatus.PARTIAL)
    with pytest.raises(ValidationError):
        FieldValue(field_name=FieldName.SENDER, raw="x", normalized="x",
                   extraction_status=ExtractionStatus.COMPLETE, approval_status=ApprovalStatus.CONFIRMED_ABSENT)


# --- identifiers stay strings (leading zeros) -----------------------------------------
def test_leading_zeros_survive_round_trip_and_ints_are_rejected():
    r = make_mock_result()
    back = EngineResult.model_validate_json(r.model_dump_json())
    assert back.form.examinations[0].commitments[0].number.normalized == "000730001"
    with pytest.raises(ValidationError):
        FieldValue(field_name=FieldName.ISRAELI_ID, raw=730001, extraction_status=ExtractionStatus.COMPLETE)


def test_unknown_keys_are_rejected():
    d = make_mock_result().model_dump(mode="json")
    d["form"]["surprise"] = 1
    with pytest.raises(ValidationError):
        EngineResult.model_validate(d)


# --- P1 1.1: a commitment with an unreadable number is still a row -------------------
def test_unreadable_commitment_number_keeps_the_row_and_is_orange():
    exam = make_mock_result().form.examinations[0]
    assert len(exam.commitments) == 3
    cell = export_cell(exam.commitments[2].number)
    assert cell.value is None and cell.orange
    partial = export_cell(exam.commitments[1].number)       # 872?41596 is never exported
    assert partial.value is None and partial.orange


def test_count_uncertain_creates_issue_and_no_invented_rows():
    form = make_mock_result(scenario="count_unsure").form
    assert form.examinations[0].commitments == []
    assert [i.reason for i in form.issues] == [IssueReason.COMMITMENT_COUNT_UNCERTAIN]


def test_technical_failure_is_failed_not_missing():
    form = make_mock_result(scenario="failed").form
    assert form.processing_status == ProcessingStatus.FAILED and form.examinations == []


# --- P1 1.3 / spec 8.2: READY vs READY_WITH_CONFIRMED_GAPS vs NEEDS_REVIEW -----------
def test_status_derivation():
    exam = make_mock_result().form.examinations[0]
    c = exam.commitments[0]
    assert derive_commitment_status(exam, c) == CommitmentStatus.NEEDS_REVIEW

    # approve everything the engine proposed; date/sender are still open
    exam2 = exam.model_copy(update={
        "first_name": _approve(exam.first_name), "last_name": _approve(exam.last_name),
        "israeli_id": _approve(exam.israeli_id),
        "central_numbers": [_approve(x) for x in exam.central_numbers],
    })
    c2 = c.model_copy(update={"number": _approve(c.number)})
    assert derive_commitment_status(exam2, c2) == CommitmentStatus.NEEDS_REVIEW

    # human supplies a date, and confirms the sender is absent in the source
    date = exam.sample_received_date.model_copy(update={
        "raw": "11/08/2026", "normalized": "2026-08-11", "extraction_status": ExtractionStatus.COMPLETE,
        "approval_status": ApprovalStatus.HUMAN_APPROVED})
    sender_absent = exam.sender.model_copy(update={"approval_status": ApprovalStatus.CONFIRMED_ABSENT})
    exam3 = exam2.model_copy(update={"sample_received_date": date, "sender": sender_absent})
    assert derive_commitment_status(exam3, c2) == CommitmentStatus.READY_WITH_CONFIRMED_GAPS

    sender_ok = sender_absent.model_copy(update={
        "raw": "כללית", "normalized": "כללית", "extraction_status": ExtractionStatus.COMPLETE,
        "approval_status": ApprovalStatus.HUMAN_APPROVED})
    exam4 = exam3.model_copy(update={"sender": sender_ok})
    assert derive_commitment_status(exam4, c2) == CommitmentStatus.READY
    assert derive_commitment_status(exam4, c2, form_failed=True) == CommitmentStatus.FAILED


# --- spec 8.3: export cell rule --------------------------------------------------------
def test_export_cells():
    exam = make_mock_result().form.examinations[0]
    assert export_cell(exam.israeli_id).orange                      # pending -> empty + orange
    ok = _approve(exam.israeli_id)
    assert export_cell(ok).value == "000000018" and not export_cell(ok).orange
    absent = exam.sender.model_copy(update={"approval_status": ApprovalStatus.CONFIRMED_ABSENT})
    assert export_cell(absent).value is None and not export_cell(absent).orange


def test_central_cell_all_or_nothing():
    exam = make_mock_result().form.examinations[0]
    a, b = exam.central_numbers
    assert export_central_cell([_approve(a), _approve(b)]).value == "000987;000988"
    assert export_central_cell([_approve(a), b]).orange             # one still pending -> whole cell orange
    assert export_central_cell([]).orange


def test_local_only_mode_is_recorded():
    assert make_mock_result(mode=EngineMode.LOCAL_ONLY).engine_run.mode == EngineMode.LOCAL_ONLY
