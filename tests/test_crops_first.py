"""Crops-first privacy path (LABX_MODE=crops_first). No API calls: the readers and the croppers are stubbed."""
import pytest

from extraction import engine_select, labx_overlay
from labx import crops, engine


def _tok(text, x0, y0, x1, y1):
    return {"text": text, "bbox": [x0, y0, x1, y1], "conf": 0.9}


PRINT = [_tok("מילה", 100 + 60 * i, 100, 150 + 60 * i, 118) for i in range(20)]       # median height 18


def test_stamp_month_token_is_a_date_line():
    [box] = engine_select.date_line_boxes(PRINT + [_tok("-08-", 400, 870, 450, 905), _tok("2026", 466, 874, 525, 917)])
    assert box[0] < 400 and box[2] > 525 and box[1] < 870 and box[3] > 917     # one line, widened


def test_a_tall_standalone_year_is_a_date_line_but_a_printed_one_is_not():
    assert engine_select.date_line_boxes(PRINT + [_tok("2026", 400, 500, 460, 530)])        # 30 >= 1.4 x 18
    assert not engine_select.date_line_boxes(PRINT + [_tok("2026", 400, 500, 460, 518)])    # print size
    assert not engine_select.date_line_boxes(PRINT + [_tok("06/08/2026", 400, 500, 520, 518)])


def _read(**kw):
    base = {k: "" for k in crops.SCHEMA_CROPS["required"]}
    base.update({k: "none" for k in base if k.endswith("legibility")}, received_date_source="none")
    base.update(kw)
    return base


def test_crops_are_decided_by_the_same_consensus(monkeypatch):
    r = _read(patient_id="11223344", patient_id_label="ת.ז.", patient_id_legibility="clear",
              received_day="18", received_month="06", received_year="2026", received_date_legibility="clear",
              received_date_source="stamp")
    monkeypatch.setattr(engine, "_ask_all", lambda slots, prompt, images, schema: [("a", r, None), ("b", r, None)])
    res = crops.extract([("handwriting", b"h"), ("date_line", b"d")])
    assert res["fields"]["id"]["status"] == "accepted" and res["fields"]["received_date"]["value"] == "2026-06-18"
    assert res["log"]["crop_kinds"] == ["handwriting", "date_line"]


def test_one_unsure_crop_reading_keeps_the_date_for_review(monkeypatch):
    a = _read(received_day="20", received_month="08", received_year="2026", received_date_legibility="clear",
              received_date_source="stamp")
    b = dict(a, received_date_legibility="unsure")
    monkeypatch.setattr(engine, "_ask_all", lambda *_: [("a", a, None), ("b", b, None)])
    assert crops.extract([("date_line", b"d")])["fields"]["received_date"]["status"] == "review"


@pytest.fixture
def crops_mode(monkeypatch):
    monkeypatch.setenv("LABX_MODE", "crops_first")
    sent = {"crops": 0, "full_page": 0}
    fields = {f: {"value": None, "status": "missing", "reason": "not found", "candidates": []} for f in engine.FIELDS}
    monkeypatch.setattr(labx_overlay._crops(), "extract",
                        lambda c: sent.__setitem__("crops", sent["crops"] + 1) or {"fields": fields,
                                                                                    "log": {"crop_reads": {},
                                                                                            "crop_kinds": []}})
    monkeypatch.setattr(labx_overlay, "_run", lambda *a: sent.__setitem__("full_page", sent["full_page"] + 1)
                        or {"fields": fields, "log": {"page_reads": {}}})
    monkeypatch.setattr(labx_overlay, "_date_line_crops", lambda *a: [("date_line", b"d")])
    return sent


LINE = [{"bbox": [10, 10, 50, 30], "rotation": 0, "dpi": 200, "source_size": [100, 100]}]


def test_located_page_sends_only_crops(crops_mode, monkeypatch):
    monkeypatch.setattr(labx_overlay._crops(), "handwriting_crops", lambda *a: [("handwriting", b"h")])
    assert labx_overlay.start(b"", ".pdf", 0) is None                  # nothing is sent before the OCR result
    assert labx_overlay._run_crops(b"", ".pdf", 0, LINE)[0] == "sent"
    assert crops_mode == {"crops": 1, "full_page": 0}


@pytest.mark.parametrize("hand, lines, why", [([], LINE, "no handwriting located"),
                                              ([("handwriting", b"h")], [], "no date line located")])
def test_unlocated_field_escalates(crops_mode, monkeypatch, hand, lines, why):
    monkeypatch.setattr(labx_overlay._crops(), "handwriting_crops", lambda *a: hand)
    assert labx_overlay._run_crops(b"", ".pdf", 0, lines) == ("escalate", why)
    assert crops_mode["crops"] == 0


def test_date_lines_are_kept_per_page_and_used_once():
    labx_overlay.remember_date_lines("f", 0, LINE)
    assert labx_overlay._DATE_LINES.pop(("f", 0)) == LINE and ("f", 0) not in labx_overlay._DATE_LINES


def test_an_id_cut_off_at_the_crop_edge_is_not_accepted(monkeypatch):
    r = _read(patient_id="223344", patient_id_label="ת.ז.", patient_id_legibility="clear")
    monkeypatch.setattr(engine, "_ask_all", lambda *_: [("a", r, None), ("b", r, None)])
    e = crops.extract([("handwriting", b"h")])["fields"]["id"]
    assert e["status"] == "review" and e["value"] is None and "check digit" in e["reason"]
