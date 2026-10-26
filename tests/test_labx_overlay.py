"""labx names are never accepted, but what the models read reaches the reviewer (no API calls)."""
from extraction import labx_overlay
from shared.contracts import ApprovalStatus, ExtractionStatus, FieldName, FieldValue

MISSING = FieldValue(field_name=FieldName.FIRST_NAME, extraction_status=ExtractionStatus.MISSING)
REASON = "names always go to human review (models invent names)"


def _entry(**kw):
    return {"value": None, "status": "review", "reason": REASON, "candidates": ["page:a=דנה(c)", "page:b=דנה(c)"],
            **kw}


def test_agreed_name_is_prefilled_but_not_approved():
    fv = labx_overlay._merge(MISSING, "first_name", _entry(proposed="דנה", readings=["דנה"]), 0, auto=True)
    assert fv.normalized == "דנה" and fv.extraction_status == ExtractionStatus.UNCERTAIN
    assert fv.approval_status == ApprovalStatus.PENDING and fv.dimensions is None     # never auto-approved
    assert "labx_name_proposal" in fv.flags and [c.normalized for c in fv.candidates] == ["דנה"]


def test_disagreeing_names_are_candidates_without_a_value():
    entry = _entry(reason="readings disagree or invalid", readings=["דנה", "רנה"])
    fv = labx_overlay._merge(MISSING, "first_name", entry, 0, auto=False)
    assert fv.normalized is None and fv.extraction_status == ExtractionStatus.UNCERTAIN
    assert [c.normalized for c in fv.candidates] == ["דנה", "רנה"]


def test_no_reading_keeps_the_page_engine_field():
    entry = {"value": None, "status": "missing", "reason": "not found", "candidates": []}
    assert labx_overlay._merge(MISSING, "first_name", entry, 0, auto=False) is MISSING


def test_complete_page_engine_name_is_kept():
    old = FieldValue(field_name=FieldName.FIRST_NAME, raw="דנה", normalized="דנה",
                     extraction_status=ExtractionStatus.COMPLETE)
    assert labx_overlay._merge(old, "first_name", _entry(proposed="רנה", readings=["רנה"]), 0, auto=False) is old
