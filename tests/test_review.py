"""Regression tests for PUT /review (T-01, T-02). Synthetic fixtures in a temporary folder only."""
import json

import pytest
from fastapi.testclient import TestClient

import backend.review as review
from backend.app import app
from backend.storage import LocalRepository, get_repository
from extraction.mock_engine import make_mock_result
from shared.contracts import (
    ApprovalStatus, Attribution, Commitment, Evidence, ExtractionStatus, FieldName, FieldValue, Form,
    Issue, IssueReason,
)

FILE_ID = "0123456789abcdef0123456789abcdef"
FORM_ID = "f1"
URL = f"/review/{FILE_ID}/{FORM_ID}"

client = TestClient(app)


@pytest.fixture(autouse=True)
def storage(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    yield tmp_path
    app.dependency_overrides.clear()


def _write(storage, form: Form):
    # drafts written in the pre-repository format (a bare list): still read, as version 1
    path = storage / f"{FILE_ID}.drafts.json"
    path.write_text(json.dumps([form.model_dump(mode="json")], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load(path) -> Form:
    stored = json.loads(path.read_text(encoding="utf-8"))
    forms = stored if isinstance(stored, list) else stored["forms"]
    return Form.model_validate(forms[0])


def _ev(role_basis="mock"):
    return Evidence(page=0, method="mock", engine_version="mock-0.1", role_basis=role_basis)


def _default_form() -> Form:
    """Mock default: c1 complete, c2 partial 872?41596, c3 exists with an unreadable number.
    The ID is already approved; an Issue is linked to c3."""
    form = make_mock_result(form_id=FORM_ID, file_id=FILE_ID).form
    exam = form.examinations[0]
    exam.israeli_id = exam.israeli_id.model_copy(update={"approval_status": ApprovalStatus.HUMAN_APPROVED})
    form.issues.append(Issue(issue_id="i-c3", form_id=FORM_ID, commitment_id=f"{FORM_ID}-c3",
                             reason=IssueReason.REVIEW_RELINK_REQUIRED))
    return form


def _clean_form() -> Form:
    """Every field has a complete extracted value; one commitment."""
    form = make_mock_result(form_id=FORM_ID, file_id=FILE_ID).form
    exam = form.examinations[0]

    def complete(name, value):
        return FieldValue(field_name=name, raw=value, normalized=value,
                          extraction_status=ExtractionStatus.COMPLETE, evidence=[_ev()])

    exam.sample_received_date = complete(FieldName.SAMPLE_RECEIVED_DATE, "2026-08-11")
    exam.sender = complete(FieldName.SENDER, "כללית")
    exam.commitments = exam.commitments[:1]
    return form


def _body(**over):
    body = dict(first_name="ישראל", last_name="ישראלי", israeli_id="000000018", sender=None,
                sample_received_date=None, central_numbers=["000987", "000988"],
                commitment_numbers=["000730001", "872?41596"], approve=False)
    body.update(over)
    return body


def _clean_body(**over):
    return _body(**{"sender": "כללית", "sample_received_date": "2026-08-11",
                    "commitment_numbers": ["000730001"], **over})


def _assert_blocked(response, path, before, code):
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert code in [f["error"] for f in detail["fields"]]
    assert path.read_bytes() == before                     # nothing written
    assert not list(path.parent.glob("*.tmp"))
    return detail


def _exam(response):
    return Form.model_validate(response.json()["form"]).examinations[0]


# ---- IF-1: unchanged fields keep everything -------------------------------------------
def test_saving_one_field_keeps_other_approvals(storage):
    _write(storage, _default_form())
    r = client.put(URL, json=_body(first_name="ישראלה"))
    assert r.status_code == 200
    assert _exam(r).israeli_id.approval_status == ApprovalStatus.HUMAN_APPROVED


def test_unchanged_fields_are_identical(storage):
    before = _default_form().examinations[0]
    _write(storage, _default_form())
    after = _exam(client.put(URL, json=_body(first_name="ישראלה")))
    for name in ("last_name", "israeli_id", "sender", "sample_received_date"):
        assert getattr(after, name) == getattr(before, name)
    assert after.central_numbers == before.central_numbers
    assert after.commitments == before.commitments
    assert after.sample_received_date.candidates                     # candidate kept
    assert after.sender.uncertainty_reason                           # uncertainty kept


# ---- IF-1 + evidence preservation ------------------------------------------------------
def test_changed_field_is_pending_and_old_evidence_moves_to_candidates(storage):
    old = _default_form().examinations[0].first_name
    _write(storage, _default_form())
    fv = _exam(client.put(URL, json=_body(first_name="ישראלה"))).first_name
    assert fv.normalized == "ישראלה" and fv.approval_status == ApprovalStatus.PENDING
    assert fv.version == old.version + 1
    assert fv.evidence == [] and fv.dimensions is None              # not presented as verified
    [candidate] = fv.candidates
    assert candidate.raw == old.raw and candidate.evidence == old.evidence
    assert candidate.tier == Attribution.STRONG and candidate.role_basis == "patient_block"


def test_second_edit_does_not_duplicate_candidates(storage):
    path = _write(storage, _default_form())
    client.put(URL, json=_body(first_name="א"))
    r = client.put(URL, json=_body(first_name="ב"))
    fv = _exam(r).first_name
    assert [c.raw for c in fv.candidates] == ["ישראל"]               # manual "א" is not a source reading
    assert fv.version == 3
    assert _load(path).examinations[0].first_name == fv


def test_evidence_without_raw_value_blocks_the_change(storage):
    form = _default_form()
    form.examinations[0].sender = form.examinations[0].sender.model_copy(update={"evidence": [_ev()]})
    path = _write(storage, form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_body(sender="כללית")), path, before,
                             "EVIDENCE_PRESERVATION_BLOCKED")
    assert detail["status"] == "BLOCKED_CONTRACT_CHANGE"


# ---- IF-2: partial values are saved, never approved ------------------------------------
def test_value_with_question_mark_is_saved_as_partial(storage):
    _write(storage, _default_form())
    r = client.put(URL, json=_body(israeli_id="0000000?8", sample_received_date="2026-08-?1"))
    assert r.status_code == 200
    exam = _exam(r)
    for fv, positions in ((exam.israeli_id, [7]), (exam.sample_received_date, [8])):
        assert fv.extraction_status == ExtractionStatus.PARTIAL and fv.partial
        assert fv.unknown_positions == positions and fv.normalized is None
        assert fv.approval_status == ApprovalStatus.PENDING


def test_approve_with_changed_value_is_rejected(storage):
    path = _write(storage, _clean_form())
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_clean_body(first_name="דנה", approve=True)), path, before,
                    "APPROVAL_REQUIRES_SAVED_VALUE")


def test_save_then_approve(storage):
    path = _write(storage, _clean_form())
    assert client.put(URL, json=_clean_body(first_name="דנה")).status_code == 200
    r = client.put(URL, json=_clean_body(first_name="דנה", approve=True))
    assert r.status_code == 200 and r.json()["approved"] is True
    exam = _exam(r)
    fields = [getattr(exam, n) for n in review.SHARED_FIELDS] + exam.central_numbers + [exam.commitments[0].number]
    assert all(f.approval_status == ApprovalStatus.HUMAN_APPROVED for f in fields)
    assert exam.first_name.version == 3                               # saved (2), then approved (3)
    assert exam.last_name.version == 2 and exam.last_name.evidence    # approval keeps evidence
    assert _load(path).examinations[0] == exam


def test_approve_with_saved_partial_value_is_rejected(storage):
    path = _write(storage, _default_form())
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_body(approve=True)), path, before, "NOT_APPROVABLE")
    assert f"commitments[{FORM_ID}-c2].number" in [f["field"] for f in detail["fields"]]


def test_approve_never_reports_success_with_open_fields(storage):
    form = _clean_form()
    form.examinations[0].sender = FieldValue(field_name=FieldName.SENDER, extraction_status=ExtractionStatus.MISSING)
    path = _write(storage, form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_clean_body(sender=None, approve=True)), path, before,
                             "APPROVAL_INCOMPLETE")
    assert "sender" in [f["field"] for f in detail["fields"]]

    form = _clean_form()
    exam = form.examinations[0]
    exam.commitments.append(Commitment(
        commitment_id="f1-c9", examination_id=exam.examination_id, origin_anchor="block-9", internal_index=1,
        number=FieldValue(field_name=FieldName.COMMITMENT_NUMBER, extraction_status=ExtractionStatus.UNCERTAIN)))
    path = _write(storage, form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_clean_body(approve=True)), path, before, "APPROVAL_INCOMPLETE")
    assert "commitments[f1-c9].number" in [f["field"] for f in detail["fields"]]


def test_invalid_id_format_is_saved_but_not_approvable(storage):
    path = _write(storage, _clean_form())
    r = client.put(URL, json=_clean_body(israeli_id="12345"))
    assert r.status_code == 200
    fv = _exam(r).israeli_id
    assert fv.raw == "12345" and fv.normalized is None and "FORMAT_INVALID" in fv.flags
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_clean_body(israeli_id="12345", approve=True)), path, before,
                    "NOT_APPROVABLE")


def test_checksum_warning_does_not_block_approval(storage):
    _write(storage, _clean_form())
    assert client.put(URL, json=_clean_body(israeli_id="123456789")).status_code == 200
    r = client.put(URL, json=_clean_body(israeli_id="123456789", approve=True))
    assert r.status_code == 200
    fv = _exam(r).israeli_id
    assert fv.approval_status == ApprovalStatus.HUMAN_APPROVED and "ID_CHECKSUM_WARNING" in fv.flags


# ---- IF-3 / IF-4: commitments ----------------------------------------------------------
def test_commitment_with_empty_number_is_not_deleted(storage):
    before = _default_form().examinations[0].commitments
    _write(storage, _default_form())
    r = client.put(URL, json=_body(commitment_numbers=["000730001", "872?41596", ""]))
    assert _exam(r).commitments == before
    r = client.put(URL, json=_body(commitment_numbers=[]))            # all omitted
    assert r.status_code == 200 and _exam(r).commitments == before


def test_new_commitment_number_is_rejected(storage):
    path = _write(storage, make_mock_result(form_id=FORM_ID, file_id=FILE_ID, scenario="count_unsure").form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_body(commitment_numbers=["000730001"])), path, before,
                             "COMMITMENT_ADD_BLOCKED")
    assert detail["status"] == "BLOCKED_CONTRACT_CHANGE"
    r = client.put(URL, json=_body(commitment_numbers=["", "  "]))
    assert r.status_code == 200 and _exam(r).commitments == []


def test_editing_a_commitment_number_is_rejected(storage):
    path = _write(storage, _default_form())
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_body(commitment_numbers=["000730009", "872?41596"])), path, before,
                    "COMMITMENT_EDIT_AMBIGUOUS")


def test_ids_anchors_and_linked_issues_are_kept(storage):
    _write(storage, _default_form())
    r = client.put(URL, json=_body(first_name="ישראלה", commitment_count_confirmed=True))
    form = Form.model_validate(r.json()["form"])
    commitments = form.examinations[0].commitments
    assert [c.commitment_id for c in commitments] == ["f1-c1", "f1-c2", "f1-c3"]
    assert [c.origin_anchor for c in commitments] == ["block-1", "block-2", "block-3"]
    assert [i.commitment_id for i in form.issues] == ["f1-c3"]


# ---- central numbers -------------------------------------------------------------------
@pytest.mark.parametrize("numbers", [["000987"], ["000987", "000981"], []])
def test_central_number_removal_or_edit_is_rejected(storage, numbers):
    path = _write(storage, _default_form())
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_body(central_numbers=numbers)), path, before,
                    "CENTRAL_NUMBER_CHANGE_AMBIGUOUS")


def test_central_numbers_reorder_keeps_items_and_addition_is_pending(storage):
    before = _default_form().examinations[0].central_numbers
    _write(storage, _default_form())
    assert _exam(client.put(URL, json=_body(central_numbers=["000988", "000987"]))).central_numbers == before
    added = _exam(client.put(URL, json=_body(central_numbers=["000987", "000988", "000989"]))).central_numbers
    assert added[:2] == before
    assert added[2].normalized == "000989" and added[2].approval_status == ApprovalStatus.PENDING


# ---- F1: an omitted key is left untouched ----------------------------------------------
def test_omitted_keys_are_left_untouched(storage):
    before = _default_form().examinations[0]
    _write(storage, _default_form())
    r = client.put(URL, json={"first_name": "ישראלה"})
    assert r.status_code == 200
    after = _exam(r)
    assert after.first_name.normalized == "ישראלה"
    for name in ("last_name", "israeli_id", "sender", "sample_received_date"):
        assert getattr(after, name) == getattr(before, name)
    assert after.central_numbers == before.central_numbers           # not read as "remove all"
    assert after.commitments == before.commitments


def test_explicit_null_still_clears_a_value(storage):
    _write(storage, _default_form())
    fv = _exam(client.put(URL, json={"first_name": None})).first_name
    assert fv.raw is None and fv.normalized is None and fv.approval_status == ApprovalStatus.PENDING
    assert [c.raw for c in fv.candidates] == ["ישראל"]               # the extracted value is kept


def test_approve_does_not_approve_fields_that_were_not_sent(storage):
    path = _write(storage, _clean_form())
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json={"approve": True, "first_name": "ישראל"}), path, before,
                             "APPROVAL_INCOMPLETE")
    assert "last_name" in [f["field"] for f in detail["fields"]]


# ---- F2: open blocking issues and conflicts block approval ------------------------------
def test_open_form_level_blocking_issue_blocks_approval(storage):
    form = _clean_form()
    form.issues.append(Issue(issue_id="i-split", form_id=FORM_ID, reason=IssueReason.SPLIT_SUSPECT))
    path = _write(storage, form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_clean_body(approve=True)), path, before, "OPEN_BLOCKING_ISSUE")
    assert "issues[i-split]" in [f["field"] for f in detail["fields"]]


def test_open_issue_linked_to_a_commitment_blocks_approval(storage):
    form = _clean_form()
    form.issues.append(Issue(issue_id="i-c1", form_id=FORM_ID, commitment_id=f"{FORM_ID}-c1",
                             reason=IssueReason.REVIEW_RELINK_REQUIRED))
    path = _write(storage, form)
    before = path.read_bytes()
    detail = _assert_blocked(client.put(URL, json=_clean_body(approve=True)), path, before, "OPEN_BLOCKING_ISSUE")
    assert f"commitments[{FORM_ID}-c1]" in [f["field"] for f in detail["fields"]]


def test_closed_issue_and_count_issue_do_not_block_approval(storage):
    from shared.contracts import ResolutionState
    form = _clean_form()
    form.issues += [
        Issue(issue_id="i-closed", form_id=FORM_ID, reason=IssueReason.SPLIT_SUSPECT,
              resolution_state=ResolutionState.CLOSED),
        Issue(issue_id="i-count", form_id=FORM_ID, reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN),
    ]
    _write(storage, form)
    r = client.put(URL, json=_clean_body(approve=True))
    assert r.status_code == 200 and r.json()["approved"] is True


@pytest.mark.parametrize("conflict", [
    {"extraction_status": ExtractionStatus.CONFLICT},
    {"extraction_status": ExtractionStatus.UNCERTAIN, "uncertainty_reason": ["CANDIDATE_CONFLICT"]},
])
def test_conflicting_value_is_not_approved_until_a_value_is_chosen_and_saved(storage, conflict):
    form = _clean_form()
    exam = form.examinations[0]
    exam.last_name = FieldValue.model_validate({**exam.last_name.model_dump(), **conflict})
    path = _write(storage, form)
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_clean_body(approve=True)), path, before,
                    "APPROVAL_REQUIRES_SAVED_VALUE")                 # echoing it is not a choice
    assert client.put(URL, json=_clean_body()).status_code == 200   # the person saves the chosen value
    saved = _load(path).examinations[0].last_name
    assert saved.extraction_status == ExtractionStatus.COMPLETE and saved.approval_status == ApprovalStatus.PENDING
    assert saved.candidates[-1].evidence                              # the conflicting reading is kept
    r = client.put(URL, json=_clean_body(approve=True))
    assert r.status_code == 200 and _exam(r).last_name.approval_status == ApprovalStatus.HUMAN_APPROVED


# ---- F3: dates are strictly YYYY-MM-DD ---------------------------------------------------
@pytest.mark.parametrize("value", ["20260811", "2026-W33-2", "2026-02-30", "11/08/2026", "2026-8-11"])
def test_non_strict_date_is_rejected_without_write(storage, value):
    path = _write(storage, _default_form())
    before = path.read_bytes()
    r = client.put(URL, json=_body(sample_received_date=value))
    assert r.status_code == 422 and r.json()["detail"] == "Date must be YYYY-MM-DD"
    assert path.read_bytes() == before


def test_stored_non_strict_date_is_not_approvable(storage):
    form = _clean_form()
    form.examinations[0].sample_received_date = form.examinations[0].sample_received_date.model_copy(
        update={"raw": "20260811", "normalized": "20260811"})
    path = _write(storage, form)
    before = path.read_bytes()
    _assert_blocked(client.put(URL, json=_clean_body(sample_received_date="20260811", approve=True)), path, before,
                    "NOT_APPROVABLE")


# ---- persistence -----------------------------------------------------------------------
def test_saved_form_reloads_identically(storage):
    path = _write(storage, _default_form())
    r = client.put(URL, json=_body(first_name="ישראלה", israeli_id="0000000?8"))
    assert _load(path) == Form.model_validate(r.json()["form"])
