"""Server classification READY / NEEDS_REVIEW and the automatic approval policy (acceptance A-E).
Synthetic values only; forms are built in the test or extracted from a synthetic PDF."""
import io

import openpyxl
import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.excel_export import build_export
from backend.readiness import (
    AUTO_APPROVAL_POLICY_VERSION, AUTO_APPROVAL_RULE_ID, apply_auto_approval, commitment_status,
)
from backend.storage import LocalRepository, get_repository
from shared.contracts import (
    ApprovalStatus, Attribution, Commitment, CommitmentStatus, Dimensions, Evidence, Examination, ExistenceStatus,
    ExtractionStatus, FieldName, FieldValue, Form, Issue, IssueReason, ProcessingStatus, Readability,
    UncertaintyReason, Verification,
)

VALUES = {"first_name": "Dana", "last_name": "Levi", "israeli_id": "000000018",
          "sample_received_date": "2026-08-23", "sender": "Synthetic Clinic"}


def _fv(name: FieldName, value: str, verification=Verification.INDEPENDENT, **extra) -> FieldValue:
    return FieldValue(field_name=name, raw=value, normalized=value, extraction_status=ExtractionStatus.COMPLETE,
                      dimensions=Dimensions(readability=Readability.OK, attribution=Attribution.STRONG,
                                            verification=verification),
                      evidence=[Evidence(page=0, method="synthetic", engine_version="test")], **extra)


def _form(unverified: str | None = None, missing: str | None = None, issues=(), existence=ExistenceStatus.CONFIRMED,
          **extra) -> Form:
    def field(name):
        if name == missing:
            return FieldValue(field_name=FieldName(name), extraction_status=ExtractionStatus.MISSING,
                              uncertainty_reason=[UncertaintyReason.UNGROUNDED])
        value = VALUES.get(name, {"central_numbers": "000987", "commitment_number": "000730001"}.get(name))
        verification = Verification.NONE if name == unverified else Verification.INDEPENDENT
        return _fv(FieldName(name), value, verification, **extra.get(name, {}))

    exam = Examination(examination_id="f1-e1", form_id="f1", central_numbers=[field("central_numbers")],
                       commitments=[Commitment(commitment_id="f1-c1", examination_id="f1-e1", existence_status=existence,
                                               number=field("commitment_number"))],
                       **{name: field(name) for name in VALUES})
    return Form(form_id="f1", file_id="file1", processing_status=ProcessingStatus.DONE, examinations=[exam],
                issues=[Issue(issue_id=f"f1-i{n}", form_id="f1", reason=r) for n, r in enumerate(issues)])


def _status(form: Form) -> CommitmentStatus:
    exam = form.examinations[0]
    return commitment_status(form, exam, exam.commitments[0])


def _fields(form: Form) -> list[FieldValue]:
    exam = form.examinations[0]
    return [getattr(exam, n) for n in VALUES] + exam.central_numbers + [exam.commitments[0].number]


# ------------------------------------------------------------------ A
def test_a_seven_verified_fields_without_issues_are_ready_automatically():
    form = apply_auto_approval(_form())
    assert all(f.approval_status == ApprovalStatus.AUTO_APPROVED for f in _fields(form))
    assert {(f.auto_approval_rule_id, f.policy_version) for f in _fields(form)} == {
        (AUTO_APPROVAL_RULE_ID, AUTO_APPROVAL_POLICY_VERSION)}
    assert _status(form) == CommitmentStatus.READY
    data, summary = build_export([form])
    assert (summary.ready, summary.orange_cells) == (1, 0)
    row = list(openpyxl.load_workbook(io.BytesIO(data)).worksheets[0].iter_rows(min_row=2, values_only=True))[0]
    assert None not in row


# ------------------------------------------------------------------ B
@pytest.mark.parametrize("field", ["first_name", "israeli_id", "sample_received_date", "central_numbers",
                                   "commitment_number"])
def test_b_one_unverified_value_keeps_the_row_in_review(field):
    form = apply_auto_approval(_form(unverified=field))
    pending = [f for f in _fields(form) if f.approval_status == ApprovalStatus.PENDING]
    assert [f.field_name.value for f in pending] == [field]          # a proposal is never an approval
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW
    assert build_export([form])[1].ready == 0


@pytest.mark.parametrize("doubt", [
    {"sender": {"flags": ["same_engine_reread"]}},
    {"israeli_id": {"uncertainty_reason": [UncertaintyReason.RECOGNITION]}},
])
def test_b_a_flagged_or_doubtful_value_is_not_approved(doubt):
    form = apply_auto_approval(_form(**doubt))
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW


def test_b_open_issue_keeps_a_fully_verified_row_in_review():
    form = apply_auto_approval(_form(issues=[IssueReason.COMMITMENT_COUNT_UNCERTAIN]))
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW
    _, summary = build_export([form])
    assert summary.ready == 0 and summary.is_partial
    assert summary.orange_cells == 0                          # every cell is filled...
    assert any("שורות מלאות אך אינן מוכנות" in w for w in summary.warnings)      # ...and still flagged


def test_filled_row_with_an_open_split_issue_is_flagged_and_partial():
    form = apply_auto_approval(_form())
    form.issues.append(Issue(issue_id="f1-split", form_id="f1", reason=IssueReason.SPLIT_SUSPECT))
    _, summary = build_export([form])
    assert (summary.ready, summary.orange_cells, summary.is_partial) == (0, 0, True)
    assert any("שורות מלאות אך אינן מוכנות" in w for w in summary.warnings)


def test_b_blocking_issue_prevents_any_automatic_approval():
    form = apply_auto_approval(_form(issues=[IssueReason.SPLIT_SUSPECT]))
    assert all(f.approval_status == ApprovalStatus.PENDING for f in _fields(form))
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW


def test_b_uncertain_commitment_existence_is_not_ready():
    form = apply_auto_approval(_form(existence=ExistenceStatus.UNCERTAIN))
    assert form.examinations[0].commitments[0].number.approval_status == ApprovalStatus.PENDING
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW


# ------------------------------------------------------------------ C
@pytest.mark.parametrize("field", ["sender", "sample_received_date"])
def test_c_missing_field_is_needs_review(field):
    form = apply_auto_approval(_form(missing=field))
    assert _status(form) == CommitmentStatus.NEEDS_REVIEW
    assert build_export([form])[1].ready == 0


# ------------------------------------------------------------------ D + E through the routes
client = TestClient(app)


@pytest.fixture
def repo(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    yield
    app.dependency_overrides.pop(get_repository, None)


def _pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for n, text in enumerate(["First name: Dana", "Last name: Levi", "Israeli ID: 000000018",
                              "Sample received date: 2026-08-23", "Sender: Synthetic Clinic",
                              "Central number: 000987", "Commitment number: 000730001"]):
        page.insert_text((72, 80 + 24 * n), text, fontsize=11)
    return doc.tobytes()


def _approve_everything(file_id: str, form_id: str):
    exam = client.get(f"/documents/{file_id}/drafts").json()[0]["examinations"][0]
    targets = [({"field": n}, exam[n]["version"]) for n in VALUES]
    targets += [({"field": "central_numbers", "index": i, "expected_value": c["normalized"]}, c["version"])
                for i, c in enumerate(exam["central_numbers"])]
    targets += [({"field": "commitment_number", "commitment_id": c["commitment_id"]}, c["number"]["version"])
                for c in exam["commitments"]]
    for target, version in targets:
        r = client.post(f"/review/{file_id}/{form_id}/field/approval",
                        json={"target": target, "action": "APPROVE", "expected_version": version})
        assert r.status_code == 200, r.text


def test_d_extraction_is_never_auto_approved_but_human_approval_reaches_ready(repo):
    file_id = client.post("/documents/upload", files={"file": ("s.pdf", _pdf(), "application/pdf")}).json()["file_id"]
    forms = client.post(f"/documents/{file_id}/drafts/init").json()
    exam = forms[0]["examinations"][0]
    # the text-layer engine reports Verification.NONE: proposals stay PENDING
    assert {exam[n]["approval_status"] for n in VALUES} == {"PENDING"}
    assert client.get(f"/documents/{file_id}/export/summary").json()["ready"] == 0

    _approve_everything(file_id, forms[0]["form_id"])
    summary = client.get(f"/documents/{file_id}/export/summary").json()
    assert (summary["ready"], summary["orange_cells"]) == (1, 0)
    # E: the export agrees with the server classification of the stored form
    stored = Form.model_validate(client.get(f"/documents/{file_id}/drafts").json()[0])
    assert _status(stored) == CommitmentStatus.READY


def test_form_without_an_identified_commitment_number_is_a_row_to_review(repo):
    """The other fields are extracted; the missing commitment number is an empty cell to fill, not a lost form."""
    doc = pymupdf.open()
    page = doc.new_page()
    for n, text in enumerate(["First name: Dana", "Last name: Levi", "Israeli ID: 000000018"]):
        page.insert_text((72, 80 + 24 * n), text, fontsize=11)
    file_id = client.post("/documents/upload", files={"file": ("s.pdf", doc.tobytes(), "application/pdf")}).json()["file_id"]
    [form] = client.post(f"/documents/{file_id}/drafts/init").json()
    exam = form["examinations"][0]
    assert exam["first_name"]["normalized"] == "Dana" and exam["israeli_id"]["normalized"] == "000000018"
    [row] = exam["commitments"]
    assert row["existence_status"] == "UNCERTAIN" and row["number"]["normalized"] is None
    stored = Form.model_validate(form)
    assert _status(stored) == CommitmentStatus.NEEDS_REVIEW
    summary = client.get(f"/documents/{file_id}/export/summary").json()
    assert summary["rows"] == 1 and summary["ready"] == 0          # the row is exported, never as ready

    # a person enters the number from the source and approves it: existence is no longer in doubt
    target = {"field": "commitment_number", "commitment_id": row["commitment_id"]}
    saved = client.patch(f"/review/{file_id}/{form['form_id']}/field",
                         json={"target": target, "value": "000730001", "expected_version": row["number"]["version"]})
    assert saved.status_code == 200
    r = client.post(f"/review/{file_id}/{form['form_id']}/field/approval",
                    json={"target": target, "action": "APPROVE", "expected_version": saved.json()["field"]["version"]})
    assert r.status_code == 200
    stored = Form.model_validate(client.get(f"/documents/{file_id}/drafts").json()[0])
    number = stored.examinations[0].commitments[0].number
    assert number.normalized == "000730001" and number.approval_status == ApprovalStatus.HUMAN_APPROVED
    # the count of commitments on the form is still unconfirmed: the row stays in review until it is confirmed
    assert _status(stored) == CommitmentStatus.NEEDS_REVIEW
