
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import current_workspace
from backend.storage import Repository, VersionConflict, get_repository, validate_file_id
from shared.contracts import (
    ApprovalStatus,
    Candidate,
    Commitment,
    Examination,
    ExtractionStatus,
    FieldName,
    FieldValue,
    Form,
    IssueReason,
    ResolutionState,
    UncertaintyReason,
)

router = APIRouter(prefix="/review", tags=["Review"])

SHARED_FIELDS = (
    "first_name",
    "last_name",
    "israeli_id",
    "sender",
    "sample_received_date",
)

FORMAT_INVALID = "FORMAT_INVALID"
ID_CHECKSUM_WARNING = "ID_CHECKSUM_WARNING"
# Evidence.method of a location the OCR engine marks for the reviewer (ocr_engine/labextract_ocr/to_contract.py)
REVIEW_REGION_METHOD = "review_region"

# Open issues that block approval (Spec v3.2 7.6). A form-level one (no commitment_id) blocks the
# whole form; one linked to a commitment blocks that row. The commitment count issue blocks form
# closure, not field approval.
BLOCKING_ISSUES = {
    IssueReason.REVIEW_RELINK_REQUIRED,
    IssueReason.UNREADABLE_DOCUMENT,
    IssueReason.SPLIT_SUSPECT,
    IssueReason.TECHNICAL_FAILURE,
}

# A blocked request is rejected with 422 before anything is written.
BLOCKED = "BLOCKED"
# Cannot be done safely with the current request contract (needs an approved API change).
BLOCKED_CONTRACT_CHANGE = "BLOCKED_CONTRACT_CHANGE"


class ReviewInput(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    israeli_id: str | None = None
    sender: str | None = None
    sample_received_date: str | None = None

    central_numbers: list[str] = Field(default_factory=list)
    commitment_numbers: list[str] = Field(default_factory=list)

    approve: bool = False
    commitment_count_confirmed: bool = False


# ------------------------------------------------------------ field rules
def _norm(value) -> str:
    return value.strip() if value is not None else ""


def _current_values(fv: FieldValue) -> set[str]:
    """What a request may send to mean 'this value, unchanged'.

    A conflicting extraction has no single current value: any value sent for it is a human choice.
    """
    if _is_conflict(fv):
        return set()
    return {v.strip() for v in (fv.normalized, fv.raw) if v and v.strip()}


def _is_conflict(fv: FieldValue) -> bool:
    return (
        fv.extraction_status == ExtractionStatus.CONFLICT
        or UncertaintyReason.CANDIDATE_CONFLICT in fv.uncertainty_reason
    )


def _is_unchanged(old: FieldValue, value: str) -> bool:
    if not value:
        return not _current_values(old)
    return value in _current_values(old)


def _is_iso_date(value) -> bool:
    """Strictly YYYY-MM-DD (fromisoformat alone also accepts 20260811, 2026-W33-2, ...)."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _israeli_id_checksum_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(digits):
        n = int(char) * (1 if index % 2 == 0 else 2)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _classify(name: FieldName, value: str) -> tuple[str, list[str]]:
    """EMPTY / PARTIAL / FORMAT_INVALID / VALID, plus flags.

    A checksum failure is a warning only (Spec v3.2 7.6); it never blocks approval.
    """
    if not value:
        return "EMPTY", []
    if "?" in value:
        return "PARTIAL", []
    if name == FieldName.ISRAELI_ID:
        if not re.fullmatch(r"[0-9]{9}", value):
            return "FORMAT_INVALID", [FORMAT_INVALID]
        if not _israeli_id_checksum_ok(value):
            return "VALID", [ID_CHECKSUM_WARNING]
    return "VALID", []


def _approvable(fv: FieldValue) -> bool:
    if fv.partial or not fv.normalized or FORMAT_INVALID in fv.flags or _is_conflict(fv):
        return False
    if fv.field_name == FieldName.ISRAELI_ID:
        return bool(re.fullmatch(r"[0-9]{9}", fv.normalized))
    if fv.field_name == FieldName.SAMPLE_RECEIVED_DATE:
        return _is_iso_date(fv.normalized)
    return True


def _is_resolved(fv: FieldValue) -> bool:
    return fv.is_approved_with_value or fv.is_confirmed_absent


def _blocker(code: str, status: str, path: str) -> dict:
    return {"error": code, "status": status, "field": path}


def _build(name: FieldName, value: str, *, version: int = 1, candidates=(), value_source=None,
           resolution_state=None) -> FieldValue:
    """A saved (never approved) value. No evidence: a manual value is not verified by old evidence."""
    category, flags = _classify(name, value)
    common = {
        "field_name": name,
        "candidates": list(candidates),
        "flags": flags,
        "version": version,
        "approval_status": ApprovalStatus.PENDING,
    }
    if value_source is not None:
        common["value_source"] = value_source
    if resolution_state is not None:
        common["resolution_state"] = resolution_state

    if category == "VALID":
        return FieldValue(raw=value, normalized=value, extraction_status=ExtractionStatus.COMPLETE, **common)
    if category == "PARTIAL":
        return FieldValue(
            raw=value,
            partial=True,
            unknown_positions=[i for i, char in enumerate(value) if char == "?"],
            extraction_status=ExtractionStatus.PARTIAL,
            **common,
        )
    if category == "FORMAT_INVALID":
        return FieldValue(raw=value, extraction_status=ExtractionStatus.UNCERTAIN, **common)
    return FieldValue(extraction_status=ExtractionStatus.UNCERTAIN, **common)


def _rebuild(old: FieldValue, value: str, path: str, blockers: list) -> FieldValue:
    """Replace a value without losing the source evidence of the value it replaces.

    An extracted value (it has evidence or dimensions) is kept as a Candidate together with
    its original evidence, so the evidence stays attached to the value it supports.
    """
    candidates = list(old.candidates)
    # A review region ("review_region") is a place to look, not a reading: a field that has ONLY regions and no raw
    # value (e.g. a handwritten name / ID the engine located but could not read) has no extracted value to preserve.
    only_regions = old.raw is None and old.evidence and all(e.method == REVIEW_REGION_METHOD for e in old.evidence)
    if (old.evidence or old.dimensions is not None) and not only_regions:
        if old.raw is None:
            if old.evidence:
                # Candidate.raw is required: the evidence cannot be kept with its value.
                blockers.append(_blocker("EVIDENCE_PRESERVATION_BLOCKED", BLOCKED_CONTRACT_CHANGE, path))
                return old
        else:
            previous = Candidate(
                raw=old.raw,
                normalized=old.normalized,
                tier=old.dimensions.attribution if old.dimensions else None,
                role_basis=old.evidence[0].role_basis if old.evidence else None,
                evidence=list(old.evidence),
            )
            if previous not in candidates:
                candidates.append(previous)

    return _build(
        old.field_name,
        value,
        version=old.version + 1,
        candidates=candidates,
        value_source=old.value_source,
        resolution_state=old.resolution_state,
    )


def _review_field(old: FieldValue, raw_value, approve: bool, path: str, blockers: list) -> FieldValue:
    value = _norm(raw_value)

    if _is_unchanged(old, value):
        if approve and old.approval_status == ApprovalStatus.PENDING and _current_values(old):
            if _approvable(old):
                return old.model_copy(
                    update={"approval_status": ApprovalStatus.HUMAN_APPROVED, "version": old.version + 1}
                )
            blockers.append(_blocker("NOT_APPROVABLE", BLOCKED, path))
        return old

    if approve:
        # Until D-18 is approved: a value is saved first and only a saved value is approved.
        blockers.append(_blocker("APPROVAL_REQUIRES_SAVED_VALUE", BLOCKED, path))
        return old

    if (
        old.field_name == FieldName.SAMPLE_RECEIVED_DATE
        and _classify(old.field_name, value)[0] == "VALID"
        and not _is_iso_date(value)
    ):
        raise HTTPException(422, "Date must be YYYY-MM-DD")

    return _rebuild(old, value, path, blockers)


# ------------------------------------------------------------ list rules
def _review_central_numbers(existing: list[FieldValue], values: list[str], approve: bool,
                            blockers: list) -> list[FieldValue]:
    remaining = [(i, _norm(v)) for i, v in enumerate(values) if _norm(v)]
    result = []

    for index, old in enumerate(existing):
        path = f"central_numbers[{index}]"
        current = _current_values(old)
        if not current:
            result.append(old)          # nothing a request can refer to: kept as is
            continue
        match = next((k for k, (_, v) in enumerate(remaining) if v in current), None)
        if match is None:
            # removed or edited: not identifiable without an item id -> never deleted silently
            blockers.append(_blocker("CENTRAL_NUMBER_CHANGE_AMBIGUOUS", BLOCKED_CONTRACT_CHANGE, path))
            result.append(old)
            continue
        _, value = remaining.pop(match)
        result.append(_review_field(old, value, approve, path, blockers))

    for request_index, value in remaining:
        path = f"central_numbers[{request_index}]"
        if approve:
            blockers.append(_blocker("APPROVAL_REQUIRES_SAVED_VALUE", BLOCKED, path))
            continue
        result.append(_build(FieldName.CENTRAL_NUMBERS, value))

    return result


def _review_commitments(existing: list[Commitment], values: list[str], approve: bool,
                        blockers: list) -> list[Commitment]:
    remaining = [(i, _norm(v)) for i, v in enumerate(values) if _norm(v)]
    result = []
    unmatched = 0

    for commitment in existing:
        current = _current_values(commitment.number)
        match = next((k for k, (_, v) in enumerate(remaining) if v in current), None) if current else None
        if match is None:
            # empty number or omitted from the request: the record is kept unchanged
            unmatched += 1
            result.append(commitment)
            continue
        _, value = remaining.pop(match)
        path = f"commitments[{commitment.commitment_id}].number"
        number = _review_field(commitment.number, value, approve, path, blockers)
        result.append(commitment if number is commitment.number else commitment.model_copy(update={"number": number}))

    for request_index, _ in remaining:
        # Without a commitment id and evidence of existence, an unmatched number cannot be told
        # apart from an edit; creating or re-identifying a record here would be a guess.
        code = "COMMITMENT_EDIT_AMBIGUOUS" if unmatched else "COMMITMENT_ADD_BLOCKED"
        blockers.append(_blocker(code, BLOCKED_CONTRACT_CHANGE, f"commitment_numbers[{request_index}]"))

    return result


def _unresolved_paths(exam: Examination) -> list[str]:
    paths = [name for name in SHARED_FIELDS if not _is_resolved(getattr(exam, name))]
    if not exam.central_numbers:
        paths.append("central_numbers")
    paths += [f"central_numbers[{i}]" for i, fv in enumerate(exam.central_numbers) if not _is_resolved(fv)]
    paths += [
        f"commitments[{c.commitment_id}].number" for c in exam.commitments if not _is_resolved(c.number)
    ]
    return paths


def _issue_blockers(form: Form) -> list[dict]:
    """Approval is refused while a blocking issue is open on the form or on one of its rows."""
    return [
        _blocker(
            "OPEN_BLOCKING_ISSUE",
            BLOCKED,
            f"commitments[{issue.commitment_id}]" if issue.commitment_id else f"issues[{issue.issue_id}]",
        )
        for issue in form.issues
        if issue.reason in BLOCKING_ISSUES and issue.resolution_state != ResolutionState.CLOSED
    ]


def _blocked_detail(blockers: list) -> dict:
    status = (
        BLOCKED_CONTRACT_CHANGE
        if any(b["status"] == BLOCKED_CONTRACT_CHANGE for b in blockers)
        else BLOCKED
    )
    return {"error": blockers[0]["error"], "status": status, "fields": blockers}


# ------------------------------------------------------------ endpoint
@router.put("/{file_id}/{form_id}")
def save_review(
    file_id: str,
    form_id: str,
    data: ReviewInput,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    validate_file_id(file_id)
    drafts = repo.get_drafts(workspace_id, file_id)

    if drafts is None:
        raise HTTPException(404, "Document drafts not found")

    stored, version = drafts
    forms = [Form.model_validate(item) for item in stored]

    form = next(
        (f for f in forms if f.form_id == form_id),
        None,
    )

    if form is None:
        raise HTTPException(404, "Form not found")

    if not form.examinations:
        raise HTTPException(422, "Form has no examination")

    exam = form.examinations[0]
    blockers: list[dict] = []

    if data.approve:
        blockers += _issue_blockers(form)

    # A key the client did not send is left untouched (never read as "clear this field").
    sent = data.model_fields_set
    updates = {
        name: _review_field(getattr(exam, name), getattr(data, name), data.approve, name, blockers)
        for name in SHARED_FIELDS
        if name in sent
    }
    if "central_numbers" in sent:
        updates["central_numbers"] = _review_central_numbers(
            exam.central_numbers, data.central_numbers, data.approve, blockers
        )
    if "commitment_numbers" in sent:
        updates["commitments"] = _review_commitments(
            exam.commitments, data.commitment_numbers, data.approve, blockers
        )
    reviewed = exam.model_copy(update=updates)

    if data.approve and not blockers:
        # never report approval while any field of the examination is still open
        blockers += [_blocker("APPROVAL_INCOMPLETE", BLOCKED, p) for p in _unresolved_paths(reviewed)]

    if blockers:
        raise HTTPException(422, detail=_blocked_detail(blockers))

    form.examinations[0] = reviewed

    if data.commitment_count_confirmed:
        form.issues = [
            issue for issue in form.issues
            if issue.reason != IssueReason.COMMITMENT_COUNT_UNCERTAIN
        ]

    # Validate the complete result against the shared contract.
    validated = Form.model_validate(form.model_dump())

    result = [
        validated if item.form_id == form_id else item
        for item in forms
    ]

    try:
        repo.save_drafts(
            workspace_id,
            file_id,
            [item.model_dump(mode="json") for item in result],
            expected_version=version,
        )
    except VersionConflict as conflict:
        # another save landed between our read and write: nothing was overwritten
        raise HTTPException(409, detail={
            "error": "VERSION_CONFLICT",
            "status": BLOCKED,
            "server_version": conflict.server_version,
        })

    return {
        "saved": True,
        "form_id": form_id,
        "approved": data.approve,
        "form": validated.model_dump(mode="json"),
    }
