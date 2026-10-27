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
                        lambda c, **kw: sent.__setitem__("crops", sent["crops"] + 1) or {"fields": fields,
                                                                                    "log": {"crop_reads": {},
                                                                                            "crop_kinds": []}})
    monkeypatch.setattr(labx_overlay, "_run", lambda *a: sent.__setitem__("full_page", sent["full_page"] + 1)
                        or {"fields": fields, "log": {"page_reads": {}}})
    monkeypatch.setattr(labx_overlay, "_region_crops", lambda *a: [("date_line", b"d")])
    return sent


LINE = {"regions": [{"kind": "date_line", "bbox": [10, 10, 50, 30], "rotation": 0, "dpi": 200,
                     "source_size": [100, 100]}], "form_dates": []}
NONE = labx_overlay.NO_REGIONS


def test_located_page_sends_only_crops(crops_mode, monkeypatch):
    monkeypatch.setattr(labx_overlay._crops(), "handwriting_crops", lambda *a: [("handwriting", b"h")])
    assert labx_overlay.start(b"", ".pdf", 0) is None                  # nothing is sent before the OCR result
    assert labx_overlay._run_crops(b"", ".pdf", 0, LINE)[0] == "sent"
    assert crops_mode == {"crops": 1, "full_page": 0}


@pytest.mark.parametrize("hand, lines, why", [([], LINE, "no handwriting located"),
                                              ([("handwriting", b"h")], NONE, "no received-date region located")])
def test_unlocated_field_escalates(crops_mode, monkeypatch, hand, lines, why):
    monkeypatch.setattr(labx_overlay._crops(), "handwriting_crops", lambda *a: hand)
    assert labx_overlay._run_crops(b"", ".pdf", 0, lines) == ("escalate", why)
    assert crops_mode["crops"] == 0


def test_regions_are_kept_per_page_and_used_once():
    labx_overlay.remember_regions("f", 0, LINE)
    assert labx_overlay._REGIONS.pop(("f", 0)) == LINE and ("f", 0) not in labx_overlay._REGIONS


def test_an_id_cut_off_at_the_crop_edge_is_not_accepted(monkeypatch):
    r = _read(patient_id="223344", patient_id_label="ת.ז.", patient_id_legibility="clear")
    monkeypatch.setattr(engine, "_ask_all", lambda *_: [("a", r, None), ("b", r, None)])
    e = crops.extract([("handwriting", b"h")])["fields"]["id"]
    assert e["status"] == "review" and e["value"] is None and "check digit" in e["reason"]


# ---------------------------------------------------------------- central number (lab annotation crop)
from shared.contracts import ExtractionStatus, FieldName, FieldValue  # noqa: E402


def test_central_annotation_alone_locates_the_received_date(crops_mode, monkeypatch):
    """The date is written under the central number: no stamp line is needed to stay on crops."""
    monkeypatch.setattr(labx_overlay._crops(), "handwriting_crops", lambda *a: [("handwriting", b"h")])
    monkeypatch.setattr(labx_overlay, "_region_crops", lambda *a: [("central_annotation", b"c")])
    local = {"regions": [dict(LINE["regions"][0], kind="central_annotation")], "form_dates": []}
    assert labx_overlay._run_crops(b"", ".pdf", 0, local)[0] == "sent" and crops_mode["full_page"] == 0


@pytest.mark.parametrize("raw, ok", [("939158", "939158"), ("93915", None), ("9391581", None), ("939?58", None),
                                     ("939 158", None), ("16/6/26", None)])
def test_central_number_must_be_six_clean_digits(raw, ok):
    assert crops._central(raw) == ok


def _central_reads(value, leg="clear", **kw):
    r = _read(central_number=value, central_number_legibility=leg, **kw)
    return lambda *_: [("a", r, None), ("b", r, None)]


def test_two_models_agreeing_accept_the_central_number(monkeypatch):
    monkeypatch.setattr(engine, "_ask_all", _central_reads("939158"))
    e = crops.extract([("central_annotation", b"c")])["fields"]["central_number"]
    assert e["status"] == "accepted" and e["value"] == "939158"


def test_central_number_equal_to_an_id_reading_is_not_accepted(monkeypatch):
    monkeypatch.setattr(engine, "_ask_all", _central_reads("112233", patient_id="112233", patient_id_label="ת.ז.",
                                                           patient_id_legibility="clear"))
    assert crops.extract([("central_annotation", b"c")])["fields"]["central_number"]["status"] == "review"


ACCEPTED_CENTRAL = {"value": "939158", "status": "accepted", "reason": "2 models agree", "candidates": ["x"]}


def test_ocr_central_number_is_never_replaced():
    ocr = [FieldValue(field_name=FieldName.CENTRAL_NUMBERS, raw="939376", normalized="939376",
                      extraction_status=ExtractionStatus.COMPLETE)]
    assert labx_overlay._merge_central(ocr, ACCEPTED_CENTRAL, 0, False) is ocr


def test_accepted_central_fills_an_unread_one():
    [fv] = labx_overlay._merge_central([], ACCEPTED_CENTRAL, 0, False)
    assert fv.normalized == "939158" and fv.extraction_status == ExtractionStatus.COMPLETE
    assert fv.approval_status.value == "PENDING"


def test_printed_dates_skip_the_located_regions_and_handwriting():
    toks = PRINT + [_tok("06/08/2026", 1000, 300, 1130, 318),         # printed on the form
                    _tok("16/6/26", 200, 520, 300, 538),              # inside the annotation region
                    _tok("19/08/2026", 600, 700, 760, 760)]           # handwriting-sized
    assert engine_select.printed_dates(toks, [[150, 480, 400, 600]]) == ["06/08/2026"]


def _date_reads(day, month, year="2026"):
    r = _read(received_day=day, received_month=month, received_year=year, received_date_legibility="clear",
              received_date_source="handwritten")
    return lambda *_: [("a", r, None), ("b", r, None)]


def test_crop_date_equal_to_a_printed_date_is_accepted(monkeypatch):
    """p02: the lab wrote 16/6/26 and the form was printed on 16.06.2026 - the same day, not a misread."""
    monkeypatch.setattr(engine, "_ask_all", _date_reads("16", "6", "26"))
    e = crops.extract([("central_annotation", b"c")], form_dates=["16.06.2026"])["fields"]["received_date"]
    assert e["status"] == "accepted" and e["value"] == "2026-06-16"


def test_crop_date_before_a_printed_date_is_not_accepted(monkeypatch):
    monkeypatch.setattr(engine, "_ask_all", _date_reads("10", "06"))
    e = crops.extract([("date_line", b"d")], form_dates=["16.06.2026"])["fields"]["received_date"]
    assert e["status"] == "review" and "before_form_date" in e["reason"]
