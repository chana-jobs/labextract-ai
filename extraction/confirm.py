"""A second, independent source for values the OCR read alone, so they can be approved automatically.

A value the OCR proposed (COMPLETE) is still read by one engine. It becomes approvable (backend/readiness.py) only
when a second source reads the same value:
  - a decoded barcode with the same digits                        -> verification INDEPENDENT (other modality)
  - two different models reading the same value in its crop      -> verification AGREEMENT (labx_overlay)
The confirmed value is rewritten as a clean field: readability OK, attribution STRONG, no flags, only the matching
candidates (the auto-approval refuses any doubt). A value no second source confirmed is left exactly as it was.
"""
from __future__ import annotations

import os

from shared.contracts import (
    Attribution, Dimensions, Evidence, ExtractionStatus, FieldValue, Form, Readability, Verification,
)

METHOD = "second_source_confirmation"


def digits(s: str | None) -> str:
    return "".join(ch for ch in s or "" if ch.isdigit()).lstrip("0")


def confirmable(fv: FieldValue) -> bool:
    return fv.extraction_status == ExtractionStatus.COMPLETE and bool(fv.normalized) and not fv.partial


def confirmed(fv: FieldValue, verification: Verification, page0: int, note: str) -> FieldValue:
    return fv.model_copy(update={
        "dimensions": Dimensions(readability=Readability.OK, attribution=Attribution.STRONG, verification=verification),
        "flags": [], "uncertainty_reason": [],
        "candidates": [c for c in fv.candidates if c.normalized == fv.normalized],
        "evidence": list(fv.evidence) + [Evidence(page=page0, bbox=None, method=METHOD, engine_version="confirm-1",
                                                  role_basis=note[:500], tier=Attribution.STRONG)],
    })


def enabled() -> bool:
    """A verified value is approved automatically (readiness), so confirming needs the auto-approve switch
    (backend/auto_approve.txt) AND the crops-first switch (backend/crops_first.txt): the second-source confirmation
    is part of that measured mode and turns on with it. Otherwise nothing is marked and every value stays as read."""
    return os.environ.get("LABX_AUTO_APPROVE") == "1" and os.environ.get("LABX_MODE") == "crops_first"


def with_barcodes(form: Form, barcodes: list[dict] | None, page0: int) -> Form:
    """Central and commitment numbers whose digits equal a barcode decoded on the same page."""
    codes = {digits(b.get("text")) for b in barcodes or []} - {""}
    if not enabled() or not codes or not form.examinations:
        return form
    values = [fv for e in form.examinations for fv in list(e.central_numbers) + [c.number for c in e.commitments]]
    if not any(confirmable(fv) and digits(fv.normalized) in codes for fv in values):
        return form
    form = form.model_copy(deep=True)
    for exam in form.examinations:
        exam.central_numbers = [
            confirmed(fv, Verification.INDEPENDENT, page0, "barcode on the page decodes to the same digits")
            if confirmable(fv) and digits(fv.normalized) in codes else fv for fv in exam.central_numbers]
        for c in exam.commitments:
            if confirmable(c.number) and digits(c.number.normalized) in codes:
                c.number = confirmed(c.number, Verification.INDEPENDENT, page0,
                                     "barcode on the page decodes to the same digits")
    return Form.model_validate(form.model_dump())
