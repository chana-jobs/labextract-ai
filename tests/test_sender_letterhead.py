"""The sender by its letterhead (labx_overlay._decide_sender). No API calls."""
from pathlib import Path

import pytest

from extraction import labx_overlay
from shared.contracts import ExtractionStatus, FieldName, FieldValue, Verification


@pytest.fixture(autouse=True)
def ocr_list(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "ocr_engine"))


def _ocr(value):
    return FieldValue(field_name=FieldName.SENDER, raw=value, normalized=value,
                      extraction_status=ExtractionStatus.COMPLETE)


TWO = lambda text, leg="clear": [("a", text, leg), ("b", text, leg)]  # noqa: E731


def test_letterhead_replaces_the_patients_fund():
    """part 2 p13: the OCR took 'מכבי' (the patient's fund), the letterhead is Wolfson."""
    fv = labx_overlay._decide_sender(_ocr("מכבי"), TWO("מרכז רפואי וולפסון"), 0, auto=True)
    assert fv.extraction_status == ExtractionStatus.COMPLETE and "וולפסון" in fv.normalized
    assert fv.dimensions.verification == Verification.AGREEMENT


def test_letterhead_confirms_the_same_institution_written_differently():
    fv = labx_overlay._decide_sender(_ocr('מרכז רפואי ע"ש רמב"ם'), TWO('רמב"ם'), 0, auto=True)
    assert fv.normalized == 'מרכז רפואי ע"ש רמב"ם' and fv.dimensions.verification == Verification.AGREEMENT


def test_a_fund_that_is_the_real_sender_is_confirmed():
    """part 1: a fund's own form - its logo is the letterhead."""
    fv = labx_overlay._decide_sender(_ocr("מאוחדת"), TWO("מאוחדת"), 0, auto=True)
    assert fv.normalized == "מאוחדת" and fv.dimensions.verification == Verification.AGREEMENT


def test_without_a_letterhead_decision_a_fund_goes_to_review():
    fv = labx_overlay._decide_sender(_ocr("מכבי"), [("a", "מרכז רפואי וולפסון", "clear")], 0, auto=True)
    assert fv.extraction_status == ExtractionStatus.UNCERTAIN and fv.normalized is None
    assert [c.normalized for c in fv.candidates] == ["מכבי"]


def test_without_a_letterhead_decision_a_hospital_stays_as_read():
    old = _ocr("מרכז רפואי ע\"ש א. וולפסון")
    assert labx_overlay._decide_sender(old, [], 0, auto=True) is old


def test_models_naming_different_institutions_decide_nothing():
    readings = [("a", "מרכז רפואי וולפסון", "clear"), ("b", "מרכז רפואי ברזילי", "clear")]
    fv = labx_overlay._decide_sender(_ocr("מכבי"), readings, 0, auto=True)
    assert fv.extraction_status == ExtractionStatus.UNCERTAIN


def test_unsure_readings_decide_nothing():
    old = _ocr("מרכז רפואי ע\"ש א. וולפסון")
    assert labx_overlay._decide_sender(old, TWO("מרכז רפואי וולפסון", "unsure"), 0, auto=True) is old


def test_the_receiving_laboratory_is_never_the_sender():
    old = _ocr("מרכז רפואי ע\"ש א. וולפסון")
    assert labx_overlay._decide_sender(old, TWO("המעבדה המרכזית לנגיפים"), 0, auto=True) is old


def test_two_lite_models_do_not_replace_the_ocr_sender():
    readings = [("gemini:gemini-3.5-flash-lite", "מרכז רפואי וולפסון", "clear"),
                ("gemini:gemini-3.1-flash-lite", "מרכז רפואי וולפסון", "clear")]
    fv = labx_overlay._decide_sender(_ocr("מכבי"), readings, 0, auto=True)
    assert fv.extraction_status == ExtractionStatus.UNCERTAIN          # held for review, not replaced


def test_two_lite_models_may_confirm_the_ocr_sender():
    """Confirming the OCR's own institution: the OCR is the independent second source."""
    readings = [("gemini:gemini-3.5-flash-lite", 'רמב"ם', "clear"), ("gemini:gemini-3.1-flash-lite", 'רמב"ם', "clear")]
    fv = labx_overlay._decide_sender(_ocr('מרכז רפואי ע"ש רמב"ם'), readings, 0, auto=True)
    assert fv.dimensions.verification == Verification.AGREEMENT
