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


def _d(source, model, value, leg="clear"):
    return {"source": source, "model": model, "field": "received_date", "raw": value, "legibility": leg,
            "value": value}


ACCEPTED = {"value": "2026-08-20", "status": "accepted", "reason": "2 models agree", "candidates": []}


def test_date_is_kept_when_the_zoomed_stamp_confirms_it():
    crop = [_d("crop", "a", "2026-08-20"), _d("crop", "b", "2026-08-20")]
    assert engine._confirm_date(ACCEPTED, crop)["status"] == "accepted"


def test_unsure_zoom_blocks_a_date_both_models_read_clearly_on_the_page():
    crop = [_d("crop", "a", "2026-08-20", "unsure"), _d("crop", "b", "2026-08-20", "unsure")]
    e = engine._confirm_date(ACCEPTED, crop)
    assert e["status"] == "review" and e["value"] is None


def test_zoom_reading_another_date_blocks():
    crop = [_d("crop", "a", "2026-08-20"), _d("crop", "b", "2025-08-20")]
    assert engine._confirm_date(ACCEPTED, crop)["status"] == "review"


def test_stamp_not_located_blocks():
    assert engine._confirm_date(ACCEPTED, [])["status"] == "review"


def test_zoom_never_adds_a_value():
    pending = {"value": None, "status": "review", "reason": "1 models clear (< 2)", "candidates": []}
    assert engine._confirm_date(pending, [_d("crop", "a", "2026-08-20")]) is pending
