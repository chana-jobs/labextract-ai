"""labx consensus rules for the ID (no API calls)."""
from labx import engine


def _v(source, model, value, leg="clear"):
    return {"source": source, "model": model, "field": "id", "raw": value, "legibility": leg, "value": value}


def test_cut_off_crop_reading_does_not_block_an_agreed_id():
    votes = [_v("page", "a", "11223344"), _v("page", "b", "11223344"), _v("hand", "b", "1223344")]
    e = engine.decide_field("id", votes, [])
    assert e["status"] == "accepted" and e["value"] == "11223344"


def test_leading_zeros_are_not_a_different_id():
    votes = [_v("page", "a", "0011223344"), _v("page", "b", "11223344")]
    assert engine.decide_field("id", votes, [])["value"] == "11223344"


def test_a_different_crop_reading_still_blocks():
    votes = [_v("page", "a", "11223344"), _v("page", "b", "11223344"), _v("hand", "b", "11923344")]
    assert engine.decide_field("id", votes, [])["status"] == "review"


def test_a_cut_off_whole_page_reading_still_blocks():
    votes = [_v("page", "a", "11223344"), _v("page", "b", "1223344")]
    assert engine.decide_field("id", votes, [])["status"] == "review"


def test_one_model_is_never_enough():
    votes = [_v("page", "a", "11223344"), _v("hand", "a", "11223344")]
    assert engine.decide_field("id", votes, [])["status"] == "review"
