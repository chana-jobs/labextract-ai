"""Server-side commitment classification (READY / NEEDS_REVIEW) and the automatic approval policy.

Classification: the contract's derive_commitment_status (all seven fields approved with a value), then demoted to
NEEDS_REVIEW while the row is not closed: any open issue of its form (form-level or linked to this row), an uncertain
commitment existence, or an uncertain association. The v2 review UI applies the same demotions (frontend/src/v2/model.ts
rowStatus); the export counts rows with this function only.

Automatic approval (P1 1.2: AUTO_APPROVED carries a rule id and a policy version). A field is approved by the engine
only when the engine itself reports it verified; a proposal is never an approval:
  - COMPLETE extraction, complete and non-empty, still PENDING;
  - readability OK, attribution STRONG, verification INDEPENDENT or AGREEMENT (Verification.NONE never qualifies);
  - no uncertainty reason, no flag, no disagreeing candidate, and the same format checks as a human approval;
  - its form is processed and has no open blocking issue; a commitment number only when the row's existence is
    CONFIRMED.
No confidence is inferred here: today's engines (pdf_text, lab-ocr-1.0) report Verification.NONE for every value, so
nothing is approved automatically until an engine reports verification.
"""
from __future__ import annotations

from backend.fields import _not_approvable_reason
from backend.review import BLOCKING_ISSUES
from shared.contracts import (
    ApprovalStatus, AssociationStatus, Attribution, CommitmentStatus, ExistenceStatus, ExtractionStatus, FieldValue,
    Form, ProcessingStatus, Readability, ResolutionState, Verification, derive_commitment_status,
)

AUTO_APPROVAL_RULE_ID = "engine-verified-complete"
AUTO_APPROVAL_POLICY_VERSION = "auto-approval-1"
VERIFIED = {Verification.INDEPENDENT, Verification.AGREEMENT}
_SCALARS = ("first_name", "last_name", "israeli_id", "sample_received_date", "sender")


def commitment_status(form: Form, exam, commitment) -> CommitmentStatus:
    status = derive_commitment_status(exam, commitment, form.processing_status == ProcessingStatus.FAILED)
    if status not in (CommitmentStatus.READY, CommitmentStatus.READY_WITH_CONFIRMED_GAPS):
        return status
    open_issue = any(
        issue.resolution_state != ResolutionState.CLOSED
        and issue.commitment_id in (None, commitment.commitment_id)
        for issue in form.issues
    )
    # a number a person entered and approved from the source confirms that the commitment exists
    existence_open = (commitment.existence_status != ExistenceStatus.CONFIRMED
                      and commitment.number.approval_status != ApprovalStatus.HUMAN_APPROVED)
    if open_issue or existence_open or exam.association_status == AssociationStatus.ASSOCIATION_UNCERTAIN:
        return CommitmentStatus.NEEDS_REVIEW
    return status


def _verified(fv: FieldValue) -> bool:
    d = fv.dimensions
    return (
        fv.extraction_status == ExtractionStatus.COMPLETE
        and fv.approval_status == ApprovalStatus.PENDING
        and d is not None and d.readability == Readability.OK and d.attribution == Attribution.STRONG
        and d.verification in VERIFIED
        and not fv.uncertainty_reason and not fv.flags
        and all(c.normalized == fv.normalized for c in fv.candidates)
        and _not_approvable_reason(fv) is None
    )


def _approve(fv: FieldValue) -> FieldValue:
    if not _verified(fv):
        return fv
    return fv.model_copy(update={"approval_status": ApprovalStatus.AUTO_APPROVED,
                                 "auto_approval_rule_id": AUTO_APPROVAL_RULE_ID,
                                 "policy_version": AUTO_APPROVAL_POLICY_VERSION})


def apply_auto_approval(form: Form) -> Form:
    """Approves the engine-verified fields of a freshly extracted form; everything else stays PENDING."""
    if form.processing_status != ProcessingStatus.DONE or any(
            issue.reason in BLOCKING_ISSUES and issue.resolution_state != ResolutionState.CLOSED
            for issue in form.issues):
        return form
    form = form.model_copy(deep=True)
    for exam in form.examinations:
        for name in _SCALARS:
            setattr(exam, name, _approve(getattr(exam, name)))
        exam.central_numbers = [_approve(fv) for fv in exam.central_numbers]
        for commitment in exam.commitments:
            if commitment.existence_status == ExistenceStatus.CONFIRMED:
                commitment.number = _approve(commitment.number)
    return Form.model_validate(form.model_dump())       # the contract re-checks every AUTO_APPROVED invariant
