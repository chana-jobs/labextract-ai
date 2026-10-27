"""Per-field review: save one field (D9) and approve one field (D10, action=APPROVE only).

Every write names its target exactly and states the version it was based on:
  - a shared field by name;
  - a commitment number by its stable commitment_id;
  - a central number by index AND the value currently stored there (no item id exists in the
    shared contract yet), plus the item version.
A stale or ambiguous target is refused with 409 and nothing is written. A replaced value keeps
its source evidence (as a candidate); an extracted value is never deleted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.auth import current_workspace
from backend.review import (
    BLOCKING_ISSUES, FORMAT_INVALID, SHARED_FIELDS, _build, _classify, _is_conflict, _is_iso_date, _is_unchanged,
    _norm, _rebuild,
)
from backend.storage import Repository, VersionConflict, get_repository, validate_file_id
from shared.contracts import (
    ApprovalStatus, Examination, ExistenceStatus, FieldName, FieldValue, Form, IssueReason, ResolutionState,
)

router = APIRouter(prefix="/review", tags=["Review"])


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FieldName
    index: int | None = None              # central_numbers only
    expected_value: str | None = None     # central_numbers only: the value currently at `index`
    commitment_id: str | None = None      # commitment_number only


class FieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Target
    op: Literal["set", "add", "remove"] = "set"
    value: str | None = None
    expected_version: int | None = None   # version of the targeted field (set, remove)
    expected_count: int | None = None     # number of central numbers the client saw (add)


class FieldApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Target
    action: Literal["APPROVE"]
    expected_version: int


def _fail(status: int, code: str, **extra):
    raise HTTPException(status, detail={"error": code, **extra})


def _value_of(fv: FieldValue) -> str | None:
    return fv.normalized if fv.normalized is not None else fv.raw


def _identity(fv: FieldValue) -> set[str]:
    """Values that identify a list item (conflicting readings included)."""
    return {v.strip() for v in (fv.normalized, fv.raw) if v and v.strip()} or {""}


def _central_list(exam: Examination) -> list[dict]:
    return [{"index": i, "value": _value_of(fv), "version": fv.version} for i, fv in enumerate(exam.central_numbers)]


@dataclass
class Slot:
    path: str
    get: Callable[[], FieldValue]
    put: Callable[[FieldValue], None]
    commitment_id: str | None = None


def _slot(exam: Examination, target: Target) -> Slot:
    name = target.field.value
    if name in SHARED_FIELDS:
        if target.index is not None or target.commitment_id is not None or target.expected_value is not None:
            _fail(422, "TARGET_INVALID")
        return Slot(name, lambda: getattr(exam, name), lambda fv: setattr(exam, name, fv))

    if target.field == FieldName.COMMITMENT_NUMBER:
        if target.commitment_id is None or target.index is not None or target.expected_value is not None:
            _fail(422, "TARGET_INVALID")
        for commitment in exam.commitments:
            if commitment.commitment_id == target.commitment_id:
                return Slot(f"commitments[{commitment.commitment_id}].number", lambda: commitment.number,
                            lambda fv: setattr(commitment, "number", fv), commitment.commitment_id)
        _fail(404, "COMMITMENT_NOT_FOUND")

    # central_numbers: index + the value the client saw there
    if target.commitment_id is not None or target.index is None or target.expected_value is None:
        _fail(422, "TARGET_INVALID")
    index = target.index
    items = exam.central_numbers
    if not 0 <= index < len(items) or _norm(target.expected_value) not in _identity(items[index]):
        _fail(409, "STALE_TARGET", central_numbers=_central_list(exam))
    return Slot(f"central_numbers[{index}]", lambda: items[index], lambda fv: items.__setitem__(index, fv))


def _check_version(fv: FieldValue, expected: int | None) -> None:
    if expected is None:
        _fail(422, "EXPECTED_VERSION_REQUIRED")
    if fv.version != expected:
        _fail(409, "VERSION_CONFLICT", server_version=fv.version, server_value=_value_of(fv))


def _load(repo: Repository, workspace_id: str, file_id: str, form_id: str):
    validate_file_id(file_id)
    drafts = repo.get_drafts(workspace_id, file_id)
    if drafts is None:
        _fail(404, "DRAFTS_NOT_FOUND")
    stored, drafts_version = drafts
    forms = [Form.model_validate(item) for item in stored]
    form = next((f for f in forms if f.form_id == form_id), None)
    if form is None:
        _fail(404, "FORM_NOT_FOUND")
    if not form.examinations:
        _fail(422, "NO_EXAMINATION")
    return forms, form, drafts_version


def _store(repo: Repository, workspace_id: str, file_id: str, forms: list[Form], form: Form, drafts_version: int) -> Form:
    validated = Form.model_validate(form.model_dump())       # the shared contract is the final check
    result = [validated if f.form_id == form.form_id else f for f in forms]
    try:
        repo.save_drafts(workspace_id, file_id, [f.model_dump(mode="json") for f in result],
                         expected_version=drafts_version)
    except VersionConflict as conflict:
        _fail(409, "VERSION_CONFLICT", scope="drafts", server_version=conflict.server_version)
    return validated


def _response(form: Form, field: FieldValue | None, changed: bool, **extra) -> dict:
    return {"form_id": form.form_id, "changed": changed,
            "field": field.model_dump(mode="json") if field is not None else None,
            "form": form.model_dump(mode="json"), **extra}


# ------------------------------------------------------------------ D9: save one field
@router.patch("/{file_id}/{form_id}/field")
def save_field(file_id: str, form_id: str, data: FieldPatch,
               repo: Repository = Depends(get_repository), workspace_id: str = Depends(current_workspace)):
    forms, form, drafts_version = _load(repo, workspace_id, file_id, form_id)
    exam = form.examinations[0]
    is_central = data.target.field == FieldName.CENTRAL_NUMBERS

    if data.op == "add":
        if not is_central or data.target.index is not None or data.target.expected_value is not None:
            _fail(422, "OP_INVALID")
        if data.expected_count is None:
            _fail(422, "EXPECTED_COUNT_REQUIRED")
        if data.expected_count != len(exam.central_numbers):
            _fail(409, "STALE_TARGET", central_numbers=_central_list(exam))
        value = _norm(data.value)
        if not value:
            _fail(422, "VALUE_REQUIRED")
        if any(value in _identity(item) for item in exam.central_numbers):
            _fail(422, "DUPLICATE_VALUE")
        added = _build(FieldName.CENTRAL_NUMBERS, value)
        exam.central_numbers.append(added)
        return _response(_store(repo, workspace_id, file_id, forms, form, drafts_version), added, True)

    slot = _slot(exam, data.target)
    old = slot.get()
    _check_version(old, data.expected_version)

    if data.op == "remove":
        if not is_central:
            _fail(422, "OP_INVALID")
        if old.evidence or old.dimensions is not None or old.candidates:
            # an extracted reading is never deleted; only an item a person added can be removed
            _fail(422, "NOT_REMOVABLE")
        exam.central_numbers.pop(data.target.index)
        return _response(_store(repo, workspace_id, file_id, forms, form, drafts_version), None, True)

    if data.value is None:
        _fail(422, "VALUE_REQUIRED")
    value = _norm(data.value)
    if _is_unchanged(old, value):
        return _response(form, old, False)              # nothing to save; nothing written

    if (old.field_name == FieldName.SAMPLE_RECEIVED_DATE and _classify(old.field_name, value)[0] == "VALID"
            and not _is_iso_date(value)):
        _fail(422, "DATE_FORMAT_INVALID", expected="YYYY-MM-DD")

    blockers: list[dict] = []
    new = _rebuild(old, value, slot.path, blockers)
    if blockers:
        _fail(422, blockers[0]["error"], blocked=blockers[0]["status"], field=slot.path)
    slot.put(new)
    return _response(_store(repo, workspace_id, file_id, forms, form, drafts_version), new, True)


# ------------------------------------------------------------------ D10: approve one field
def _not_approvable_reason(fv: FieldValue) -> str | None:
    if _is_conflict(fv):
        return "CONFLICT"
    if fv.partial:
        return "PARTIAL"
    if not fv.normalized:
        return "UNREADABLE" if fv.raw else "EMPTY"
    if FORMAT_INVALID in fv.flags:
        return "FORMAT_INVALID"
    if fv.field_name == FieldName.ISRAELI_ID and not (fv.normalized.isascii() and fv.normalized.isdigit()
                                                      and len(fv.normalized) == 9):
        return "FORMAT_INVALID"
    if fv.field_name == FieldName.SAMPLE_RECEIVED_DATE and not _is_iso_date(fv.normalized):
        return "FORMAT_INVALID"
    return None


def _open_blocking_issues(form: Form, commitment_id: str | None) -> list[str]:
    """Form-level blocking issues block every approval; an issue linked to a commitment blocks that row."""
    return [
        issue.issue_id for issue in form.issues
        if issue.reason in BLOCKING_ISSUES and issue.resolution_state != ResolutionState.CLOSED
        and (issue.commitment_id is None or issue.commitment_id == commitment_id)
    ]


@router.post("/{file_id}/{form_id}/field/approval")
def approve_field(file_id: str, form_id: str, data: FieldApproval,
                  repo: Repository = Depends(get_repository), workspace_id: str = Depends(current_workspace)):
    forms, form, drafts_version = _load(repo, workspace_id, file_id, form_id)
    slot = _slot(form.examinations[0], data.target)
    old = slot.get()
    _check_version(old, data.expected_version)

    if old.approval_status in (ApprovalStatus.HUMAN_APPROVED, ApprovalStatus.AUTO_APPROVED):
        return _response(form, old, False, approved=True)
    if old.approval_status == ApprovalStatus.CONFIRMED_ABSENT:
        _fail(422, "NOT_APPROVABLE", reason="CONFIRMED_ABSENT")

    blocking = _open_blocking_issues(form, slot.commitment_id)
    if blocking:
        _fail(422, "OPEN_BLOCKING_ISSUE", issues=blocking)
    reason = _not_approvable_reason(old)
    if reason:
        _fail(422, "NOT_APPROVABLE", reason=reason)

    # only this field: no other field is approved alongside it
    approved = old.model_copy(update={"approval_status": ApprovalStatus.HUMAN_APPROVED, "version": old.version + 1})
    slot.put(approved)
    return _response(_store(repo, workspace_id, file_id, forms, form, drafts_version), approved, True, approved=True)


# ------------------------------------------------------------------ the reviewer confirms the commitment rows
class CommitmentCountConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commitment_count: int                    # the rows the reviewer looked at; a stale screen gets a 409


@router.post("/{file_id}/{form_id}/commitments/confirm")
def confirm_commitments(file_id: str, form_id: str, data: CommitmentCountConfirmation,
                        repo: Repository = Depends(get_repository), workspace_id: str = Depends(current_workspace)):
    """The reviewer looked at the source and states: the form has exactly these commitment rows. Closes the
    'commitment count uncertain' issue and marks every row's existence confirmed - a decision only a person makes;
    the screen had no way to make it, so such a row could never become READY. Field values are not touched."""
    forms, form, drafts_version = _load(repo, workspace_id, file_id, form_id)
    exam = form.examinations[0]
    if len(exam.commitments) != data.commitment_count:
        _fail(409, "COMMITMENT_COUNT_CHANGED", server_count=len(exam.commitments))
    if not exam.commitments:
        _fail(422, "NO_COMMITMENT_ROWS")
    form = form.model_copy(update={"issues": [i for i in form.issues
                                              if i.reason != IssueReason.COMMITMENT_COUNT_UNCERTAIN]})
    form.examinations[0] = exam.model_copy(update={"commitments": [
        c.model_copy(update={"existence_status": ExistenceStatus.CONFIRMED}) for c in exam.commitments]})
    return _response(_store(repo, workspace_id, file_id, forms, form, drafts_version), None, True)
