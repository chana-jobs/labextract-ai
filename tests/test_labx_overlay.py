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


DATE_MISSING = FieldValue(field_name=FieldName.SAMPLE_RECEIVED_DATE, extraction_status=ExtractionStatus.MISSING)


def test_unaccepted_date_is_offered_as_a_clean_value():
    entry = {"value": None, "status": "review", "reason": "1 models clear (< 2)",
             "candidates": ["page:gemini-3.5-flash=16/6/26(c)"], "readings": ["2026-06-16"]}
    fv = labx_overlay._merge(DATE_MISSING, "received_date", entry, 0, auto=False)
    assert fv.normalized is None and fv.extraction_status == ExtractionStatus.UNCERTAIN
    assert [(c.raw, c.normalized) for c in fv.candidates] == [("2026-06-16", "2026-06-16")]
    assert "gemini-3.5-flash" in fv.candidates[0].role_basis          # who read it is kept, not shown in the cell


def test_unparseable_reading_is_raw_text_only():
    entry = {"value": None, "status": "review", "reason": "readings disagree or invalid",
             "candidates": ["page:a=1?/6/26(u)", "page:b=1?/6/26(u)"], "readings": []}
    fv = labx_overlay._merge(DATE_MISSING, "received_date", entry, 0, auto=False)
    assert [(c.raw, c.normalized) for c in fv.candidates] == [("1?/6/26", None)]
