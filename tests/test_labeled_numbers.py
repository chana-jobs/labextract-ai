"""An accepted labx ID that is really a case / sample number printed on the page goes to review. No API calls."""
import pytest

from extraction import engine_select, labx_overlay


def _tok(text, x0, y0, x1, y1):
    return {"text": text, "bbox": [x0, y0, x1, y1], "conf": 0.9}


# part 1 p7 layout: the case number on the line of the "מקרה" label, the gender value on the line above
P7 = [_tok("מין", 1512, 76, 1550, 99), _tok("נקבה", 1239, 76, 1295, 97),
      _tok("3011587", 1191, 101, 1293, 120), _tok("מקרה", 1483, 105, 1549, 125),
      _tok("112233", 1139, 1018, 1242, 1056)]


def test_number_on_a_case_label_line_is_collected():
    assert engine_select.labeled_numbers(P7) == ["3011587"]


def test_number_on_another_line_is_not_collected():
    assert engine_select.labeled_numbers([_tok("30115877", 100, 300, 200, 320), _tok("מקרה", 300, 100, 360, 120)]) == []


def _accepted(value):
    return {"value": value, "status": "accepted", "reason": "2 models agree", "candidates": ["page:a=x(c)"]}


@pytest.mark.parametrize("value", ["30115877", "030115877", "3011587"])
def test_accepted_id_equal_to_a_labeled_number_goes_to_review(value):
    e = labx_overlay._id_is_labeled_number(_accepted(value), ["3011587"])     # the OCR dropped one digit
    assert e["status"] == "review" and e["value"] is None and e["readings"] == [value]


def test_accepted_id_two_digits_away_is_kept():
    e = _accepted("30115800")
    assert labx_overlay._id_is_labeled_number(e, ["3011587"]) is e
    assert labx_overlay._id_is_labeled_number(e, []) is e
