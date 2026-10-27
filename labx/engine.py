"""Multi-reader consensus extraction for: first name, last name, ID, received date.

Stage 1: each reader model reads the whole page (one call per model).
Stage 2 (only for fields still empty): two models locate the field, the crop is re-read at high resolution by
         each reader.
Every reading becomes a VOTE (source, field, normalized value, legibility, raw text).

Decision per field (precision first) - ACCEPTED only if
  * at least MIN_AGREE votes from at least MIN_AGREE DIFFERENT models are "clear" and equal,
  * NO vote (any stage, any legibility) carries a different value or an unparseable reading,
  * the deterministic validators pass (ID shape; date is a real past date, not before the form's own dates;
    crop labels are patient labels, not physician / father / issuer).
Otherwise the value stays EMPTY and the field is flagged "review" with all candidates for a human.
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from . import clients, hand, orient, render, validate, zoom
from .prompt import PROMPT, SCHEMA

# Two reader SLOTS. Each slot falls back along its own list when a model is overloaded / out of daily quota.
# No model appears in both slots, so an accepted value always rests on two DIFFERENT models.
DEFAULT_SLOTS = [
    ["gemini:gemini-3.5-flash", "gemini:gemini-3.7-flash", "gemini:gemini-3.5-flash-lite"],
    ["gemini:gemini-3.6-flash", "gemini:gemini-3.8-flash", "gemini:gemini-3-flash-preview", "gemini:gemini-flash-latest",
     # 27.10: a free model with its own daily quota, so the second reader still answers when the others are used up
     # (part 2 ran on one reader only). Not "gemini-flash-lite-latest": an alias that may be slot 1's own model.
     "gemini:gemini-3.1-flash-lite"],
]
DEFAULT_READERS = [s[0] for s in DEFAULT_SLOTS]
MIN_AGREE = 2
FIELDS = ["first_name", "last_name", "id", "received_date"]
ACCEPT_NAMES = __import__("os").environ.get("LABX_ACCEPT_NAMES") == "1"


def _ask_all(slots, prompt, jpeg, schema):
    def one(slot):
        try:
            m, r = clients.ask_slot(slot, prompt, jpeg, schema=schema)
            return m, r, None
        except Exception as e:
            return slot[0], None, str(e)[:200]

    with ThreadPoolExecutor(max_workers=len(slots)) as ex:
        return list(ex.map(one, slots))


def _slots(readers):
    """Accept either slot lists or plain model names (each name = its own one-model slot)."""
    if not readers:
        return DEFAULT_SLOTS
    return [r if isinstance(r, list) else [r] for r in readers]


def _vote(votes, source, model, field, raw, legibility, norm):
    if (raw or "").strip("/ "):
        votes.append({"source": source, "model": model, "field": field, "raw": raw, "legibility": legibility,
                      "value": norm(raw) if callable(norm) else norm})


def page_votes(reads):
    votes, form_dates = [], []
    for model, r, err in reads:
        if r is None:
            continue
        for f, key in (("first_name", "patient_first_name"), ("last_name", "patient_last_name")):
            _vote(votes, "page", model, f, r.get(key, ""), r.get(key + "_legibility", "none"), validate.norm_name)
        if validate.id_label_ok(r.get("patient_id_label")):
            id_norm = validate.norm_id
        else:  # no ID label (e.g. SAP screens): only a number with a valid check digit can be the ID
            id_norm = lambda s: (lambda c: c if c and validate.id_checksum_ok(c) else None)(validate.norm_id(s))
        _vote(votes, "page", model, "id", r.get("patient_id", ""), r.get("patient_id_legibility", "none"), id_norm)
        raw_d = "/".join(r.get(k, "") for k in ("received_day", "received_month", "received_year"))
        d = validate.parse_date(r.get("received_day"), r.get("received_month"), r.get("received_year"))
        if r.get("received_date_source") == "none":
            d = None
        _vote(votes, "page", model, "received_date", raw_d, r.get("received_date_legibility", "none"),
              d.isoformat() if d else None)
        form_dates += r.get("form_dates") or []
    return votes, form_dates


def crop_votes(region, reads):
    votes = []
    for model, r in reads.items():
        if not r or "_error" in r:
            continue
        leg = r.get("legibility", "none")
        if region == "patient_name":
            ok = zoom.label_ok(r.get("label"))
            for f in ("first_name", "last_name"):
                _vote(votes, "crop", model, f, r.get(f, ""), leg, (lambda s: validate.norm_name(s)) if ok else None)
        elif region == "patient_id":
            ok = zoom.label_ok(r.get("label")) and validate.id_label_ok(r.get("label"))
            _vote(votes, "crop", model, "id", r.get("id", ""), leg, validate.norm_id if ok else None)
        else:
            d = validate.parse_date(r.get("day"), r.get("month"), r.get("year"))
            if not r.get("is_received_stamp_or_receipt_note"):
                d = None
            raw_d = "/".join(r.get(k, "") for k in ("day", "month", "year"))
            _vote(votes, "crop", model, "received_date", raw_d, leg, d.isoformat() if d else None)
    return votes


def _split_by_label(label, words, model_first, model_last):
    """(first, last) only when the printed label tells which is which; else (None, None)."""
    lab = (label or "").replace("״", '"').strip()
    words = [w for w in (words or []) if w.strip(".,·")]
    has_first, has_family = "פרטי" in lab, "משפחה" in lab
    if has_first and has_family and model_first and model_last:
        if "ומשפחה" in lab or "ושם משפחה" in lab:   # one combined field "first and family name"
            return (words[0], words[1]) if len(words) == 2 else (None, None)
        return model_first, model_last               # two separate labeled fields inside the crop
    if lab.startswith("שם ומשפחה") and len(words) == 2:
        return words[0], words[1]
    if has_first and not has_family and len(words) == 1:
        return words[0], None
    if has_family and not has_first and len(words) == 1:
        return None, words[0]
    return None, None


def hand_votes(regions):
    votes = []
    for reg in regions:
        for model, r in reg["reads"].items():
            if not r or "_error" in r:
                continue
            leg, label = r.get("legibility", "none"), r.get("label", "")
            if not zoom.label_ok(label):
                continue                                   # physician / father / issuer writing
            first, last = _split_by_label(label, r.get("name_words"), r.get("first_name"), r.get("last_name"))
            if first:
                _vote(votes, "hand", model, "first_name", first, leg, validate.norm_name)
            if last:
                _vote(votes, "hand", model, "last_name", last, leg, validate.norm_name)
            raw_id = r.get("id", "")
            if raw_id:
                core = validate.norm_id(raw_id)
                labeled = validate.id_label_ok(label)
                # unlabeled number: only an ID if the check digit is valid AND a name is written right beside it
                ok = labeled or (core and validate.id_checksum_ok(core) and r.get("name_words"))
                _vote(votes, "hand", model, "id", raw_id, leg, core if ok else None)
    return votes


def is_lite(model):
    """A "lite" model: cheaper and weaker; two of them agree on the same misreading too often to accept alone."""
    return "lite" in model.split(":")[-1]


def _id_votes(vs):
    """Leading zeros are padding, not a different ID ("0011223344" = "11223344"). A crop / handwriting reading that
    is a cut-off piece of a whole-page reading (the crop edge cut digits: "1223344" inside "11223344") is no
    contradiction and is dropped; it can never add a value. Measured on part 1: 13 -> 15 accepted, 0 wrong."""
    vs = [dict(v, value=v["value"].lstrip("0") or None) if v["value"] else v for v in vs]
    page = {v["value"] for v in vs if v["source"] == "page" and v["value"]}
    return [v for v in vs if v["source"] == "page" or not v["value"]
            or not any(v["value"] != p and v["value"] in p for p in page)]


def decide_field(field, votes, form_dates, min_agree=MIN_AGREE, today=None):
    vs = [v for v in votes if v["field"] == field]
    entry = {"value": None, "status": "missing", "reason": "",
             "candidates": [f"{v['source']}:{v['model'].split(':')[-1]}={v['raw']}({v['legibility'][0]})" for v in vs]}
    if not vs:
        entry["reason"] = "not found"
        return entry
    if field == "id":
        vs = _id_votes(vs)
    distinct = {v["value"] for v in vs}
    entry["readings"] = sorted(v for v in distinct if v)    # valid normalized readings, for the reviewer
    clear_models = {v["model"] for v in vs if v["legibility"] == "clear" and v["value"]}
    if len(distinct) > 1 or None in distinct:
        entry.update(status="review", reason="readings disagree or invalid")
        return entry
    if len(clear_models) < min_agree:
        entry.update(status="review", reason=f"{len(clear_models)} models clear (< {min_agree})")
        return entry
    value = vs[0]["value"]
    if field in ("first_name", "last_name") and not ACCEPT_NAMES:
        # measured on the dev set: two different models agreed on the same INVENTED name (0 correct, 2 wrong)
        # the agreed value is offered as a proposal; a person approves or corrects it
        entry.update(status="review", reason="names always go to human review (models invent names)", proposed=value)
        return entry
    if not any(not is_lite(m) for m in clear_models):
        # 27.10, part 1 with the main models' quota used up: two lite models agreed on 3 WRONG values (a faded stamp
        # day, a misread date, a central number). Their agreement only proposes; a full model must be among them.
        entry.update(status="review", reason="only lite models agree")
        return entry
    if field == "received_date":
        ok, why = validate.date_plausible(dt.date.fromisoformat(value), form_dates, today)
        if not ok:
            entry.update(status="review", reason=f"validator: {why}")
            return entry
    entry.update(value=value, status="accepted", reason=f"{len(clear_models)} models agree")
    if field == "id":
        entry["checksum_ok"] = validate.id_checksum_ok(value)
    return entry


def _confirm_date(entry, crop):
    """A date read on the whole page is accepted only when the zoomed stamp confirms it: located, and every zoom
    reading "clear" and equal. Measured on part 1: a faded stamp read "20" instead of "26" by two models as clear on
    the page came back "unsure" on the zoom (7 correct + 1 wrong -> 5 correct + 0 wrong). Veto only, never a value."""
    if entry["status"] != "accepted":
        return entry
    if not crop:
        return dict(entry, value=None, status="review", reason="zoom did not locate the stamp to confirm the date")
    if any(v["legibility"] != "clear" for v in crop):
        return dict(entry, value=None, status="review", reason="zoomed stamp is not clear")
    if any(v["value"] != entry["value"] for v in crop):
        return dict(entry, value=None, status="review", reason="zoomed stamp reads a different date")
    return entry


def extract_page(jpeg, readers=None, min_agree=MIN_AGREE, use_zoom=True, date_zoom=True):
    slots = _slots(readers)
    reads = _ask_all(slots, PROMPT, jpeg, SCHEMA)
    votes, form_dates = page_votes(reads)
    fields = {f: decide_field(f, votes, form_dates, min_agree) for f in FIELDS}
    log = {"page_reads": {m: (r if r is not None else {"_error": e}) for m, r, e in reads}}

    pending = {f for f, e in fields.items() if e["status"] != "accepted"}
    # the handwriting round can only change a field we may accept: names only when ACCEPT_NAMES is on
    hand_fields = {"first_name", "last_name", "id"} if ACCEPT_NAMES else {"id"}
    if use_zoom and pending & hand_fields:
        regs = hand.read_regions(jpeg, slots)
        log["hand_regions"] = regs
        votes += hand_votes(regs)
        fields = {f: decide_field(f, votes, form_dates, min_agree) for f in FIELDS}
    # the stamp zoom is a veto only (on part 1 it also read a wrong year as "clear"): it runs when the page accepted
    # a date, and its readings are never counted as votes for a value
    zoom_date = use_zoom and date_zoom and fields["received_date"]["status"] == "accepted"
    crop = []
    if zoom_date:
        boxes = zoom.locate(jpeg, slots)
        log["boxes"] = boxes
        if "received_date" in boxes:
            r = zoom.read_region(zoom.crop(jpeg, boxes["received_date"]), "received_date", slots)
            log["date_crop_reads"] = r
            crop = crop_votes("received_date", r)
    if zoom_date:
        fields["received_date"] = _confirm_date(fields["received_date"], crop)
    return {"fields": fields, "log": log}


def extract_bytes(content, extension, page0, readers=None):
    """One 0-based page of an in-memory document (as the web app holds uploads). Returns the page result."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="labx_job_") as job:
        path = Path(job) / f"document{extension}"
        path.write_bytes(content)
        [res] = extract_file(path, readers=readers, pages={page0 + 1} if extension == ".pdf" else None)
    return res


def extract_file(path, readers=None, pages=None, min_agree=MIN_AGREE, use_zoom=True, date_zoom=True):
    out = []
    for n, jpeg in render.pages(path, only=pages):
        jpeg, deg = orient.upright(jpeg)
        res = extract_page(jpeg, readers, min_agree=min_agree, use_zoom=use_zoom, date_zoom=date_zoom)
        res["page"], res["rotated"] = n, deg
        out.append(res)
    return out
