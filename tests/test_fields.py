"""D9 (save one field) and D10 action=APPROVE (approve one field). Synthetic fixtures, temporary folder."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.storage import LocalRepository, get_repository
from extraction.mock_engine import make_mock_result
from shared.contracts import ApprovalStatus, ExtractionStatus, Form, Issue, IssueReason

FILE_ID = "0123456789abcdef0123456789abcdef"
FORM_ID = "f1"
PATCH = f"/review/{FILE_ID}/{FORM_ID}/field"
APPROVE = f"/review/{FILE_ID}/{FORM_ID}/field/approval"

client = TestClient(app)


@pytest.fixture(autouse=True)
def folder(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    yield tmp_path
    app.dependency_overrides.pop(get_repository, None)


def _write(folder, form: Form | None = None):
    form = form or make_mock_result(form_id=FORM_ID, file_id=FILE_ID).form
    path = folder / f"{FILE_ID}.drafts.json"
    path.write_text(json.dumps([form.model_dump(mode="json")], ensure_ascii=False), encoding="utf-8")
    return path


def _exam(path):
    stored = json.loads(path.read_text(encoding="utf-8"))
    forms = stored if isinstance(stored, list) else stored["forms"]
    return Form.model_validate(forms[0]).examinations[0]


def _unchanged(path, before, r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["detail"]["error"] == code
    assert path.read_bytes() == before and not list(path.parent.glob("*.tmp"))
    return r.json()["detail"]


# ---- D9: shared fields --------------------------------------------------------------------------
def test_save_shared_field_keeps_old_reading_and_touches_nothing_else(folder):
    path = _write(folder)
    before = _exam(path)
    r = client.patch(PATCH, json={"target": {"field": "first_name"}, "value": "Dina", "expected_version": 1})
    assert r.status_code == 200 and r.json()["changed"] is True
    field = r.json()["field"]
    assert field["normalized"] == "Dina" and field["approval_status"] == "PENDING" and field["version"] == 2
    assert field["evidence"] == [] and field["candidates"][0]["evidence"] == before.first_name.model_dump(mode="json")["evidence"]
    after = _exam(path)
    for name in ("last_name", "israeli_id", "sender", "sample_received_date"):
        assert getattr(after, name) == getattr(before, name)
    assert after.central_numbers == before.central_numbers and after.commitments == before.commitments


def test_stale_field_version_is_409_and_nothing_is_written(folder):
    path = _write(folder)
    before = path.read_bytes()
    detail = _unchanged(path, before, client.patch(PATCH, json={
        "target": {"field": "first_name"}, "value": "Dina", "expected_version": 7}), 409, "VERSION_CONFLICT")
    assert detail["server_version"] == 1 and detail["server_value"] == "ישראל"


def test_same_value_is_not_a_change(folder):
    path = _write(folder)
    before = path.read_bytes()
    r = client.patch(PATCH, json={"target": {"field": "first_name"}, "value": " ישראל ", "expected_version": 1})
    assert r.status_code == 200 and r.json()["changed"] is False and path.read_bytes() == before


@pytest.mark.parametrize("body,code", [
    ({"target": {"field": "first_name"}, "value": "x"}, "EXPECTED_VERSION_REQUIRED"),
    ({"target": {"field": "first_name", "index": 0}, "value": "x", "expected_version": 1}, "TARGET_INVALID"),
    ({"target": {"field": "first_name"}, "expected_version": 1}, "VALUE_REQUIRED"),
    ({"target": {"field": "sample_received_date"}, "value": "20260811", "expected_version": 1}, "DATE_FORMAT_INVALID"),
    ({"target": {"field": "first_name"}, "op": "remove", "expected_version": 1}, "OP_INVALID"),
])
def test_invalid_requests_write_nothing(folder, body, code):
    path = _write(folder)
    _unchanged(path, path.read_bytes(), client.patch(PATCH, json=body), 422, code)


def test_unknown_keys_are_rejected(folder):
    path = _write(folder)
    before = path.read_bytes()
    r = client.patch(PATCH, json={"target": {"field": "first_name"}, "value": "x", "expected_version": 1, "approve": True})
    assert r.status_code == 422 and path.read_bytes() == before


def test_partial_value_is_saved_as_partial(folder):
    path = _write(folder)
    r = client.patch(PATCH, json={"target": {"field": "israeli_id"}, "value": "0000000?8", "expected_version": 1})
    field = r.json()["field"]
    assert field["extraction_status"] == "PARTIAL" and field["unknown_positions"] == [7] and field["normalized"] is None


# ---- D9: commitments by stable id ------------------------------------------------------------------
def test_edit_commitment_number_by_id(folder):
    path = _write(folder)
    r = client.patch(PATCH, json={"target": {"field": "commitment_number", "commitment_id": "f1-c1"},
                                  "value": "000730009", "expected_version": 1})
    assert r.status_code == 200
    commitments = _exam(path).commitments
    assert [c.commitment_id for c in commitments] == ["f1-c1", "f1-c2", "f1-c3"]
    assert commitments[0].number.normalized == "000730009" and commitments[0].origin_anchor == "block-1"
    assert commitments[0].number.candidates[0].raw == "000730001"           # the extracted number is kept


def test_fill_an_unreadable_commitment_number(folder):
    path = _write(folder)
    r = client.patch(PATCH, json={"target": {"field": "commitment_number", "commitment_id": "f1-c3"},
                                  "value": "000730003", "expected_version": 1})
    assert r.status_code == 200 and _exam(path).commitments[2].number.normalized == "000730003"


def test_unknown_commitment_id_is_404(folder):
    path = _write(folder)
    _unchanged(path, path.read_bytes(), client.patch(PATCH, json={
        "target": {"field": "commitment_number", "commitment_id": "f1-c9"}, "value": "1", "expected_version": 1}),
        404, "COMMITMENT_NOT_FOUND")


# ---- D9: central numbers by index + current value ---------------------------------------------------
def test_edit_central_number_by_index_and_value(folder):
    path = _write(folder)
    r = client.patch(PATCH, json={"target": {"field": "central_numbers", "index": 1, "expected_value": "000988"},
                                  "value": "000989", "expected_version": 1})
    assert r.status_code == 200
    items = _exam(path).central_numbers
    assert [i.normalized for i in items] == ["000987", "000989"] and items[1].candidates[0].raw == "000988"


@pytest.mark.parametrize("target", [
    {"field": "central_numbers", "index": 1, "expected_value": "000987"},      # value moved / stale
    {"field": "central_numbers", "index": 5, "expected_value": "000988"},      # index out of range
])
def test_stale_central_target_is_409(folder, target):
    path = _write(folder)
    detail = _unchanged(path, path.read_bytes(), client.patch(PATCH, json={
        "target": target, "value": "000999", "expected_version": 1}), 409, "STALE_TARGET")
    assert detail["central_numbers"] == [{"index": 0, "value": "000987", "version": 1},
                                         {"index": 1, "value": "000988", "version": 1}]


def test_extracted_central_number_cannot_be_removed(folder):
    path = _write(folder)
    _unchanged(path, path.read_bytes(), client.patch(PATCH, json={
        "op": "remove", "target": {"field": "central_numbers", "index": 0, "expected_value": "000987"},
        "expected_version": 1}), 422, "NOT_REMOVABLE")


def test_add_then_remove_a_manual_central_number(folder):
    path = _write(folder)
    add = client.patch(PATCH, json={"op": "add", "target": {"field": "central_numbers"}, "value": "000990",
                                    "expected_count": 2})
    assert add.status_code == 200 and add.json()["field"]["approval_status"] == "PENDING"
    assert client.patch(PATCH, json={"op": "add", "target": {"field": "central_numbers"}, "value": "000991",
                                     "expected_count": 2}).status_code == 409          # stale count: no double add
    assert client.patch(PATCH, json={"op": "add", "target": {"field": "central_numbers"}, "value": "000987",
                                     "expected_count": 3}).json()["detail"]["error"] == "DUPLICATE_VALUE"
    removed = client.patch(PATCH, json={"op": "remove", "expected_version": 1,
                                        "target": {"field": "central_numbers", "index": 2, "expected_value": "000990"}})
    assert removed.status_code == 200 and [i.normalized for i in _exam(path).central_numbers] == ["000987", "000988"]


def test_concurrent_drafts_write_is_409(folder):
    path = _write(folder)
    repo = LocalRepository(folder)

    class StaleRead(LocalRepository):
        def get_drafts(self, workspace_id, fid):
            forms, version = super().get_drafts(workspace_id, fid)
            repo.save_drafts(workspace_id, fid, forms, expected_version=version)
            return forms, version

    app.dependency_overrides[get_repository] = lambda: StaleRead(folder)
    r = client.patch(PATCH, json={"target": {"field": "first_name"}, "value": "Dina", "expected_version": 1})
    assert r.status_code == 409 and r.json()["detail"] == {"error": "VERSION_CONFLICT", "scope": "drafts",
                                                           "server_version": 2}
    assert _exam(path).first_name.normalized == "ישראל"


# ---- D10: approve one field -------------------------------------------------------------------------
def test_approve_one_field_only(folder):
    path = _write(folder)
    before = _exam(path)
    r = client.post(APPROVE, json={"target": {"field": "last_name"}, "action": "APPROVE", "expected_version": 1})
    assert r.status_code == 200 and r.json()["approved"] is True
    after = _exam(path)
    assert after.last_name.approval_status == ApprovalStatus.HUMAN_APPROVED and after.last_name.version == 2
    assert after.last_name.evidence == before.last_name.evidence
    for name in ("first_name", "israeli_id"):                                # nothing else is approved
        assert getattr(after, name).approval_status == ApprovalStatus.PENDING
    assert all(c.approval_status == ApprovalStatus.PENDING for c in after.central_numbers)


def test_save_then_approve_one_field(folder):
    path = _write(folder)
    saved = client.patch(PATCH, json={"target": {"field": "sender"}, "value": "Synthetic Clinic", "expected_version": 1})
    version = saved.json()["field"]["version"]
    stale = client.post(APPROVE, json={"target": {"field": "sender"}, "action": "APPROVE", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "VERSION_CONFLICT"
    ok = client.post(APPROVE, json={"target": {"field": "sender"}, "action": "APPROVE", "expected_version": version})
    assert ok.status_code == 200 and _exam(path).sender.approval_status == ApprovalStatus.HUMAN_APPROVED
    again = client.post(APPROVE, json={"target": {"field": "sender"}, "action": "APPROVE", "expected_version": version + 1})
    assert again.status_code == 200 and again.json()["changed"] is False


@pytest.mark.parametrize("target,reason", [
    ({"field": "commitment_number", "commitment_id": "f1-c2"}, "PARTIAL"),      # 872?41596
    ({"field": "commitment_number", "commitment_id": "f1-c3"}, "EMPTY"),
    ({"field": "sample_received_date"}, "EMPTY"),
    ({"field": "sender"}, "EMPTY"),
])
def test_values_that_cannot_be_approved(folder, target, reason):
    path = _write(folder)
    detail = _unchanged(path, path.read_bytes(), client.post(APPROVE, json={
        "target": target, "action": "APPROVE", "expected_version": 1}), 422, "NOT_APPROVABLE")
    assert detail["reason"] == reason


def test_conflict_is_not_approvable(folder):
    form = make_mock_result(form_id=FORM_ID, file_id=FILE_ID).form
    exam = form.examinations[0]
    exam.last_name = exam.last_name.model_copy(update={"extraction_status": ExtractionStatus.CONFLICT})
    path = _write(folder, form)
    assert _unchanged(path, path.read_bytes(), client.post(APPROVE, json={
        "target": {"field": "last_name"}, "action": "APPROVE", "expected_version": 1}), 422,
        "NOT_APPROVABLE")["reason"] == "CONFLICT"


def test_blocking_issues(folder):
    form = make_mock_result(form_id=FORM_ID, file_id=FILE_ID).form
    form.issues.append(Issue(issue_id="i-c1", form_id=FORM_ID, commitment_id="f1-c1",
                             reason=IssueReason.REVIEW_RELINK_REQUIRED))
    path = _write(folder, form)
    detail = _unchanged(path, path.read_bytes(), client.post(APPROVE, json={
        "target": {"field": "commitment_number", "commitment_id": "f1-c1"}, "action": "APPROVE",
        "expected_version": 1}), 422, "OPEN_BLOCKING_ISSUE")
    assert detail["issues"] == ["i-c1"]
    # an issue on one row does not block a shared field
    assert client.post(APPROVE, json={"target": {"field": "first_name"}, "action": "APPROVE",
                                      "expected_version": 1}).status_code == 200

    form.issues.append(Issue(issue_id="i-split", form_id=FORM_ID, reason=IssueReason.SPLIT_SUSPECT))
    path = _write(folder, form)
    assert _unchanged(path, path.read_bytes(), client.post(APPROVE, json={
        "target": {"field": "last_name"}, "action": "APPROVE", "expected_version": 1}), 422,
        "OPEN_BLOCKING_ISSUE")["issues"] == ["i-split"]


def test_other_actions_are_not_available(folder):
    _write(folder)
    r = client.post(APPROVE, json={"target": {"field": "sender"}, "action": "CONFIRM_ABSENT", "expected_version": 1})
    assert r.status_code == 422                                               # not implemented: refused by schema
