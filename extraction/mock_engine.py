"""
Mock extraction engine: returns a contract-valid EngineResult with SYNTHETIC data only.
Purpose: let backend + frontend + Excel export be built and tested before the real engine exists
(Spec P1 2.4: day-5 gate = upload -> split -> extract/mock -> review -> Excel).

Nothing here is ground truth about any real form.
"""
from __future__ import annotations

from shared.contracts import (
    ApprovalStatus, Attribution, Candidate, Commitment, Dimensions, EngineMode, EngineResult,
    EngineRun, Evidence, Examination, ExistenceStatus, ExtractionStatus, FieldName, FieldValue,
    Form, Issue, IssueReason, ProcessingStatus, Readability, SplitStatus, UncertaintyReason,
    Verification,
)

ENGINE_VERSION = "mock-0.1"


def _ev(method: str = "mock", role_basis: str | None = None, tier: Attribution | None = None) -> Evidence:
    return Evidence(page=0, bbox=None, method=method, engine_version=ENGINE_VERSION, role_basis=role_basis, tier=tier)


def _complete(name: FieldName, raw: str, role_basis: str) -> FieldValue:
    """Engine proposes a full value. Still PENDING: approval is NOT the engine's job (P1 1.2)."""
    return FieldValue(
        field_name=name, raw=raw, normalized=raw,
        extraction_status=ExtractionStatus.COMPLETE, approval_status=ApprovalStatus.PENDING,
        dimensions=Dimensions(readability=Readability.OK, attribution=Attribution.STRONG,
                              verification=Verification.AGREEMENT),
        evidence=[_ev(role_basis=role_basis, tier=Attribution.STRONG)],
    )


def _missing(name: FieldName) -> FieldValue:
    return FieldValue(field_name=name, extraction_status=ExtractionStatus.MISSING,
                      uncertainty_reason=[UncertaintyReason.ATTRIBUTION])


def make_mock_result(form_id: str = "f1", file_id: str = "file1", mode: EngineMode = EngineMode.FULL,
                     scenario: str = "default") -> EngineResult:
    """
    scenario:
      default      - 1 form, 1 examination, 2 central numbers, 3 commitments:
                       c1 complete, c2 partial (872?41596), c3 exists but number unreadable (empty + orange)
                     receipt date is UNCERTAIN (stamp recognised by structure only -> needs human)
      count_unsure - number of commitments cannot be determined -> Form-level issue, no invented rows
      failed       - technical failure: processing_status=FAILED, no examinations (NOT 'missing')
    """
    run = EngineRun(run_id="r1", engine_version=ENGINE_VERSION, mode=mode)

    if scenario == "failed":
        return EngineResult(engine_run=run, form=Form(
            form_id=form_id, file_id=file_id, split_status=SplitStatus.CONFIRMED,
            processing_status=ProcessingStatus.FAILED,
            issues=[Issue(issue_id="i1", form_id=form_id, reason=IssueReason.TECHNICAL_FAILURE,
                          note="mock: simulated OCR timeout")]))

    exam_id = f"{form_id}-e1"

    # receipt date: stamp found by structure only, label unreadable -> candidate, never COMPLETE (App. E 7)
    receipt = FieldValue(
        field_name=FieldName.SAMPLE_RECEIVED_DATE, raw=None,
        extraction_status=ExtractionStatus.UNCERTAIN,
        uncertainty_reason=[UncertaintyReason.ATTRIBUTION], flags=["stamp_label_unreadable"],
        candidates=[Candidate(raw="2026-08-11", normalized="2026-08-11", tier=Attribution.MEDIUM,
                              role_basis="handwriting_below_central")],
    )

    def number_fv(raw: str | None, status: ExtractionStatus, partial: bool = False,
                  unknown: list[int] | None = None) -> FieldValue:
        return FieldValue(
            field_name=FieldName.COMMITMENT_NUMBER, raw=raw,
            normalized=raw if status == ExtractionStatus.COMPLETE else None,
            partial=partial, unknown_positions=unknown or [], extraction_status=status,
            evidence=[_ev(role_basis="label_commitment_adjacent")] if raw else [],
        )

    commitments = [
        Commitment(commitment_id=f"{form_id}-c1", examination_id=exam_id, origin_anchor="block-1", internal_index=0,
                   number=number_fv("000730001", ExtractionStatus.COMPLETE)),   # leading zeros stay (str)
        Commitment(commitment_id=f"{form_id}-c2", examination_id=exam_id, origin_anchor="block-2", internal_index=1,
                   number=number_fv("872?41596", ExtractionStatus.PARTIAL, partial=True, unknown=[3])),
        Commitment(commitment_id=f"{form_id}-c3", examination_id=exam_id, origin_anchor="block-3", internal_index=2,
                   existence_status=ExistenceStatus.CONFIRMED,
                   number=FieldValue(field_name=FieldName.COMMITMENT_NUMBER,
                                     extraction_status=ExtractionStatus.UNCERTAIN,
                                     uncertainty_reason=[UncertaintyReason.RECOGNITION])),
    ]
    issues: list[Issue] = []
    if scenario == "count_unsure":
        commitments = []
        issues.append(Issue(issue_id="i1", form_id=form_id, reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                            note="mock: cannot tell how many commitments exist; export may be missing rows"))

    exam = Examination(
        examination_id=exam_id, form_id=form_id,
        first_name=_complete(FieldName.FIRST_NAME, "ישראל", "patient_block"),
        last_name=_complete(FieldName.LAST_NAME, "ישראלי", "patient_block"),
        israeli_id=_complete(FieldName.ISRAELI_ID, "000000018", "label_id"),  # synthetic
        sample_received_date=receipt,
        sender=_missing(FieldName.SENDER),
        central_numbers=[_complete(FieldName.CENTRAL_NUMBERS, "000987", "sticker_label_central"),
                         _complete(FieldName.CENTRAL_NUMBERS, "000988", "sticker_label_central")],
        commitments=commitments,
    )
    return EngineResult(engine_run=run, form=Form(
        form_id=form_id, file_id=file_id, split_status=SplitStatus.CONFIRMED,
        processing_status=ProcessingStatus.DONE, examinations=[exam], issues=issues))
