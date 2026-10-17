"""
LabExtract AI - shared data contract (draft v0.1, for Tech Lead approval).

Sources: System Spec v3.0 (sections 8, 9, 11.2, 12.3), Appendix P1-3.0
(sections 1.1-1.4, 4), Appendix E v1.2 (sections 7.1, 8.2, 9).

Rules encoded here:
  * extraction_status says what is known from the source; approval_status says
    whether the value may enter the official output. They are independent.
  * AUTO_APPROVED requires auto_approval_rule_id + policy_version (P1 1.2).
  * Digit identifiers (id, central, commitment) are ALWAYS str (leading zeros).
  * A partial value keeps its unknown positions; '?' is internal only.
  * A technical failure is processing_status=FAILED, never MISSING.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Base(BaseModel):
    # forbid unknown keys: a typo in a field name must fail loudly, not vanish
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# ----------------------------------------------------------------- enums
class ExtractionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNCERTAIN = "UNCERTAIN"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT"


class ValueSource(str, Enum):
    DOCUMENT = "DOCUMENT"
    EXTERNAL_VERIFICATION = "EXTERNAL_VERIFICATION"


class ResolutionState(str, Enum):
    OPEN = "OPEN"
    AWAITING_EXTERNAL = "AWAITING_EXTERNAL"
    CLOSED = "CLOSED"


class ProcessingStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class SplitStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    MANUALLY_CONFIRMED = "MANUALLY_CONFIRMED"


class CommitmentStatus(str, Enum):
    READY = "READY"
    READY_WITH_CONFIRMED_GAPS = "READY_WITH_CONFIRMED_GAPS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    AWAITING_EXTERNAL = "AWAITING_EXTERNAL"
    FAILED = "FAILED"


class ExistenceStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"


class AssociationStatus(str, Enum):
    DEFAULT_EXAM_LEVEL = "DEFAULT_EXAM_LEVEL"
    ASSOCIATION_UNCERTAIN = "ASSOCIATION_UNCERTAIN"


class Readability(str, Enum):
    OK = "OK"
    DOUBT = "DOUBT"
    POOR = "POOR"


class Attribution(str, Enum):
    STRONG = "STRONG"
    MEDIUM = "MEDIUM"
    WEAK = "WEAK"


class Verification(str, Enum):
    INDEPENDENT = "INDEPENDENT"
    AGREEMENT = "AGREEMENT"
    NONE = "NONE"


class UncertaintyReason(str, Enum):
    RECOGNITION = "RECOGNITION"
    ATTRIBUTION = "ATTRIBUTION"
    CANDIDATE_CONFLICT = "CANDIDATE_CONFLICT"
    UNGROUNDED = "UNGROUNDED"


class IssueReason(str, Enum):
    COMMITMENT_COUNT_UNCERTAIN = "COMMITMENT_COUNT_UNCERTAIN"
    REVIEW_RELINK_REQUIRED = "REVIEW_RELINK_REQUIRED"
    UNREADABLE_DOCUMENT = "UNREADABLE_DOCUMENT"
    SPLIT_SUSPECT = "SPLIT_SUSPECT"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


class EngineMode(str, Enum):
    FULL = "FULL"
    LOCAL_ONLY = "LOCAL_ONLY"  # fallback: external service unavailable (App. E 5.1)


class FieldName(str, Enum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    ISRAELI_ID = "israeli_id"
    CENTRAL_NUMBERS = "central_numbers"
    SAMPLE_RECEIVED_DATE = "sample_received_date"
    SENDER = "sender"
    COMMITMENT_NUMBER = "commitment_number"


# ------------------------------------------------------------- building blocks
class Evidence(_Base):
    page: int = Field(ge=0)
    bbox: Optional[list[float]] = None  # [x0, y0, x1, y1] in SOURCE coordinates; None = no reliable position
    region_id: Optional[str] = None
    method: str
    engine_version: str
    role_basis: Optional[str] = None
    tier: Optional[Attribution] = None

    @model_validator(mode="after")
    def _bbox_shape(self):
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must have 4 numbers, or be None when no reliable position exists")
        return self


class Candidate(_Base):
    raw: str
    normalized: Optional[str] = None
    tier: Optional[Attribution] = None
    role_basis: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)


class Dimensions(_Base):
    readability: Readability
    attribution: Attribution
    verification: Verification


class FieldValue(_Base):
    field_name: FieldName
    raw: Optional[str] = None
    normalized: Optional[str] = None
    partial: bool = False
    unknown_positions: list[int] = Field(default_factory=list)

    extraction_status: ExtractionStatus
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    value_source: ValueSource = ValueSource.DOCUMENT
    resolution_state: ResolutionState = ResolutionState.OPEN

    dimensions: Optional[Dimensions] = None
    uncertainty_reason: list[UncertaintyReason] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    auto_approval_rule_id: Optional[str] = None
    policy_version: Optional[str] = None
    version: int = 1  # optimistic-locking counter (PATCH /fields/{id})

    @model_validator(mode="after")
    def _invariants(self):
        if self.approval_status == ApprovalStatus.AUTO_APPROVED and not (
            self.auto_approval_rule_id and self.policy_version
        ):
            raise ValueError("AUTO_APPROVED requires auto_approval_rule_id and policy_version (P1 1.2)")
        if self.approval_status == ApprovalStatus.AUTO_APPROVED and self.extraction_status != ExtractionStatus.COMPLETE:
            raise ValueError("AUTO_APPROVED is only allowed on a COMPLETE extraction")
        if self.approval_status == ApprovalStatus.CONFIRMED_ABSENT and self.extraction_status != ExtractionStatus.MISSING:
            raise ValueError("CONFIRMED_ABSENT is only valid for MISSING (a human says: absent in source)")
        if self.partial and not self.unknown_positions:
            raise ValueError("a partial value must say which positions are unknown")
        if self.extraction_status == ExtractionStatus.PARTIAL and not self.partial:
            raise ValueError("extraction_status=PARTIAL requires partial=True")
        if self.approval_status in (ApprovalStatus.AUTO_APPROVED, ApprovalStatus.HUMAN_APPROVED):
            if self.partial or not self.normalized:
                raise ValueError("an approved value must be complete and non-empty")
        return self

    # --- helpers used by status derivation and export --------------------
    @property
    def is_approved_with_value(self) -> bool:
        return self.approval_status in (ApprovalStatus.AUTO_APPROVED, ApprovalStatus.HUMAN_APPROVED) and bool(
            self.normalized
        )

    @property
    def is_confirmed_absent(self) -> bool:
        return self.approval_status == ApprovalStatus.CONFIRMED_ABSENT


# ---------------------------------------------------------------- entities
class Commitment(_Base):
    commitment_id: str
    examination_id: str
    existence_status: ExistenceStatus = ExistenceStatus.CONFIRMED
    existence_evidence_ref: Optional[str] = None
    origin_anchor: Optional[str] = None  # stable location; do NOT rely on internal_index after a split change
    internal_index: int = 0
    number: FieldValue  # may be empty/partial; the record still exists (P1 1.1)

    @model_validator(mode="after")
    def _number_field(self):
        if self.number.field_name != FieldName.COMMITMENT_NUMBER:
            raise ValueError("Commitment.number must have field_name=commitment_number")
        return self


class Examination(_Base):
    examination_id: str
    form_id: str
    association_status: AssociationStatus = AssociationStatus.DEFAULT_EXAM_LEVEL
    first_name: FieldValue
    last_name: FieldValue
    israeli_id: FieldValue
    sample_received_date: FieldValue
    sender: FieldValue
    central_numbers: list[FieldValue] = Field(default_factory=list)  # one FieldValue per number; never merged
    commitments: list[Commitment] = Field(default_factory=list)


class Issue(_Base):
    issue_id: str
    form_id: str
    commitment_id: Optional[str] = None
    reason: IssueReason
    resolution_state: ResolutionState = ResolutionState.OPEN
    note: Optional[str] = None


class Form(_Base):
    form_id: str
    file_id: str
    page_regions: list[dict] = Field(default_factory=list)
    split_status: SplitStatus = SplitStatus.UNCERTAIN
    split_revision: int = 1
    processing_status: ProcessingStatus = ProcessingStatus.QUEUED
    examinations: list[Examination] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class EngineRun(_Base):
    run_id: str
    engine_version: str
    mode: EngineMode = EngineMode.FULL


class EngineResult(_Base):
    """What extraction/ returns for ONE form. extraction/ never writes to the DB and never approves."""

    engine_run: EngineRun
    form: Form


# ------------------------------------------------------------- derived rules
_SHARED = ("first_name", "last_name", "israeli_id", "sample_received_date", "sender")


def derive_commitment_status(exam: Examination, c: Commitment, form_failed: bool = False) -> CommitmentStatus:
    """Spec v3 section 8.2 + P1 1.3. READY needs all seven fields approved with a value."""
    if form_failed:
        return CommitmentStatus.FAILED
    fields = [getattr(exam, n) for n in _SHARED] + [c.number]
    # central_numbers: an empty list means "nothing was even considered" -> not closed
    if not exam.central_numbers:
        return CommitmentStatus.NEEDS_REVIEW
    fields += exam.central_numbers

    if any(f.resolution_state == ResolutionState.AWAITING_EXTERNAL for f in fields):
        return CommitmentStatus.AWAITING_EXTERNAL
    if all(f.is_approved_with_value or f.is_confirmed_absent for f in fields):
        return (
            CommitmentStatus.READY_WITH_CONFIRMED_GAPS
            if any(f.is_confirmed_absent for f in fields)
            else CommitmentStatus.READY
        )
    return CommitmentStatus.NEEDS_REVIEW


class ExportCell(_Base):
    value: Optional[str]
    orange: bool


def export_cell(fv: FieldValue) -> ExportCell:
    """Spec v3 section 8.3: approved -> value; confirmed absent -> empty, no colour; anything else -> empty + orange."""
    if fv.is_approved_with_value:
        return ExportCell(value=fv.normalized, orange=False)
    if fv.is_confirmed_absent:
        return ExportCell(value=None, orange=False)
    return ExportCell(value=None, orange=True)


def export_central_cell(values: list[FieldValue], sep: str = ";") -> ExportCell:
    """ASSUMPTION [to verify with the lab template]: all central numbers go in one text cell joined by ';'.
    If ANY of them is not closed the whole cell is empty+orange (safe choice; partial lists could mislead)."""
    if not values:
        return ExportCell(value=None, orange=True)
    cells = [export_cell(v) for v in values]
    if any(c.orange for c in cells):
        return ExportCell(value=None, orange=True)
    parts = [c.value for c in cells if c.value]
    return ExportCell(value=sep.join(parts) if parts else None, orange=False)
