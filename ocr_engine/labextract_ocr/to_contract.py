"""PROPOSED adapter (lab only): engine page result -> existing shared.contracts objects.

It imports the product's contracts read-only and changes nothing there. Integration into the backend is a
separate, approved step (see INTEGRATION.md). Mapping:
  proposed  -> COMPLETE, approval PENDING, attribution from the rule, verification NONE (same-engine re-reads are
               not independent verification; flag "same_engine_reread"). INDEPENDENT is reserved for e.g. barcodes.
  uncertain -> UNCERTAIN (CONFLICT when candidates disagree), reasons from the evidence
  missing   -> MISSING + UNGROUNDED  (never CONFIRMED_ABSENT: that is a human decision)
Evidence.bbox = source-page PDF points (same convention as extraction/pdf_text.py). Evidence.page is 0-based.
"""
from __future__ import annotations

import sys
from pathlib import Path

import os  # noqa: E402
# shared.contracts: importable as-is inside the backend repo; otherwise from LABOCR_CONTRACTS_REPO, else the sibling
# folder <repo root>/labextract_contracts (dev layout)
CONTRACTS_REPO = Path(os.environ.get("LABOCR_CONTRACTS_REPO") or Path(__file__).resolve().parents[2] / "labextract_contracts")
if CONTRACTS_REPO.exists() and str(CONTRACTS_REPO) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_REPO))
from shared.contracts import (  # noqa: E402
    Attribution, Candidate, Commitment, Dimensions, Evidence, Examination, ExistenceStatus, ExtractionStatus,
    FieldName, FieldValue, Form, Issue, IssueReason, ProcessingStatus, Readability, SplitStatus,
    UncertaintyReason, Verification,
)

METHOD = "local_paddle_ocr"
REGION_METHOD = "review_region"     # a location to look at, not a reading
SCALAR = {"first_name": FieldName.FIRST_NAME, "last_name": FieldName.LAST_NAME, "israeli_id": FieldName.ISRAELI_ID,
          "received_date": FieldName.SAMPLE_RECEIVED_DATE, "sender": FieldName.SENDER}


def _tier(c: dict) -> Attribution:
    t = c.get("tier")
    if t in ("strong", "medium", "weak"):
        return Attribution[t.upper()]
    return Attribution.STRONG if (c.get("label_sim") or c.get("top_sim") or 0) >= 0.8 else Attribution.MEDIUM


def _evidence(c: dict, page0: int, engine_version: str) -> Evidence:
    return Evidence(page=page0, bbox=c.get("bbox_pt"), method=c.get("method", METHOD), engine_version=engine_version,
                    role_basis="; ".join(c["evidence"])[:500], tier=_tier(c))


def _candidates(f: dict, page0: int, ev: str) -> list[Candidate]:
    return [Candidate(raw=str(c.get("raw") or c["value"]), normalized=str(c["value"]), tier=_tier(c),
                      role_basis="; ".join(c["evidence"])[:500], evidence=[_evidence(c, page0, ev)])
            for c in f["candidates"]]


def _reasons(f: dict) -> list[UncertaintyReason]:
    values = {c["value"] for c in f["candidates"]}
    out = []
    if len(values) > 1:
        out.append(UncertaintyReason.CANDIDATE_CONFLICT)
    if any(c.get("stable") is False or "partial" in " ".join(c["evidence"]) for c in f["candidates"]):
        out.append(UncertaintyReason.RECOGNITION)
    if not out:
        out.append(UncertaintyReason.ATTRIBUTION)
    return out


def _field(name: FieldName, f: dict, page0: int, ev: str, value: str | None = None) -> FieldValue:
    if f["status"] == "proposed" and value is not None:
        c = next((c for c in f["candidates"] if c["value"] == value), f["candidates"][0])
        return FieldValue(field_name=name, raw=str(c.get("raw") or value), normalized=value,
                          extraction_status=ExtractionStatus.COMPLETE,
                          dimensions=Dimensions(readability=Readability.OK, attribution=_tier(c),
                                                verification=Verification.NONE),
                          flags=["same_engine_reread"] if c.get("rereads") else [],
                          evidence=[_evidence(c, page0, ev)], candidates=_candidates(f, page0, ev))
    # regions where the value should be (e.g. a handwritten ID): Evidence with a box, never a value
    regions = [Evidence(page=page0, bbox=r.get("bbox_pt"), method=REGION_METHOD, engine_version=ev,
                        role_basis=r["evidence"][:500]) for r in f.get("regions", [])]
    if f["status"] == "missing":
        return FieldValue(field_name=name, extraction_status=ExtractionStatus.MISSING,
                          uncertainty_reason=[UncertaintyReason.UNGROUNDED], flags=[f["reason"][:120]],
                          evidence=regions)
    reasons = _reasons(f)
    status = ExtractionStatus.CONFLICT if UncertaintyReason.CANDIDATE_CONFLICT in reasons else ExtractionStatus.UNCERTAIN
    return FieldValue(field_name=name, extraction_status=status, uncertainty_reason=reasons,
                      candidates=_candidates(f, page0, ev), flags=[f["reason"][:120]], evidence=regions)


def page_to_form(file_id: str, page: dict, engine_version: str) -> Form:
    page0 = page["page"] - 1
    form_id = f"{file_id}-p{page['page']}"
    if page.get("processing_status") == "FAILED":          # a technical failure is FAILED, never MISSING
        return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page0}],
                    split_status=SplitStatus.UNCERTAIN, processing_status=ProcessingStatus.FAILED, examinations=[],
                    issues=[Issue(issue_id=f"{form_id}-fail", form_id=form_id, reason=IssueReason.TECHNICAL_FAILURE,
                                  note=f"page processing failed: {page.get('error', 'unknown')}"[:200])])
    exam_id = f"{form_id}-e1"
    F = page["fields"]
    scalars = {SCALAR[k].value if k != "received_date" else "sample_received_date":
               _field(SCALAR[k], F[k], page0, engine_version, F[k].get("value")) for k in SCALAR}
    cen = F["central_numbers"]
    central = ([_field(FieldName.CENTRAL_NUMBERS, cen, page0, engine_version, v) for v in cen["values"]]
               if cen["status"] == "proposed" else
               ([_field(FieldName.CENTRAL_NUMBERS, cen, page0, engine_version)] if cen["candidates"] else []))
    com = F["commitment_numbers"]
    issues, commitments = [], []
    if com.get("items"):
        # one Commitment per commitment row: existence (existence_status), the number's extraction
        # (number.extraction_status) and approval (always PENDING here) are separate states
        for i, it in enumerate(com["items"]):
            cs = [c for c in com["candidates"] if c.get("item") == it["item_key"]]
            item_field = {"status": "proposed" if it["status"] == "proposed" else "uncertain", "candidates": cs,
                          "reason": "; ".join(it["uncertainty"]) or com["reason"]}
            anchor = f"p{page0}:bbox{cs[0]['bbox_source']}"
            commitments.append(Commitment(
                commitment_id=f"{form_id}-{it['item_id']}", examination_id=exam_id,
                existence_status=ExistenceStatus.CONFIRMED if it["existence"] == "confirmed" else ExistenceStatus.UNCERTAIN,
                existence_evidence_ref=anchor, origin_anchor=anchor, internal_index=i,
                number=_field(FieldName.COMMITMENT_NUMBER, item_field, page0, engine_version, it["value"])))
        if com["count_uncertain"]:
            issues.append(Issue(issue_id=f"{form_id}-count", form_id=form_id,
                                reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                                note=("The list may be incomplete or contain unresolved rows: "
                                      f"{sum(it['status'] == 'proposed' for it in com['items'])} of "
                                      f"{len(com['items'])} rows have a proposed number; the total count is not confirmed")))
    elif com["status"] == "proposed":
        for i, v in enumerate(com["values"]):
            c = next(c for c in com["candidates"] if c["value"] == v)
            anchor = f"p{page0}:bbox{c['bbox_source']}"
            commitments.append(Commitment(commitment_id=f"{form_id}-c{i + 1}", examination_id=exam_id,
                                          existence_status=ExistenceStatus.CONFIRMED, existence_evidence_ref=anchor,
                                          origin_anchor=anchor, internal_index=i,
                                          number=_field(FieldName.COMMITMENT_NUMBER, com, page0, engine_version, v)))
    elif com["status"] == "uncertain":
        anchor = f"p{page0}:bbox{com['candidates'][0]['bbox_source']}"
        commitments.append(Commitment(commitment_id=f"{form_id}-c1", examination_id=exam_id,
                                      existence_status=ExistenceStatus.UNCERTAIN, existence_evidence_ref=anchor,
                                      origin_anchor=anchor, internal_index=0,
                                      number=_field(FieldName.COMMITMENT_NUMBER, com, page0, engine_version)))
        issues.append(Issue(issue_id=f"{form_id}-count", form_id=form_id, reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                            note="Commitment number candidates need review; the count is not confirmed"))
    else:
        issues.append(Issue(issue_id=f"{form_id}-count", form_id=form_id, reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                            note="No commitment number found next to a label; absence is not confirmed"))
    exam = Examination(examination_id=exam_id, form_id=form_id, central_numbers=central, commitments=commitments,
                       **scalars)
    return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page0}], split_status=SplitStatus.UNCERTAIN,
                processing_status=ProcessingStatus.DONE, examinations=[exam], issues=issues)
