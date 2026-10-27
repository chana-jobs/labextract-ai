"""Crops-first reading (privacy): only regions located LOCALLY are sent, never the whole page.

The caller locates the regions without a model (handwriting by pen colour, hand.regions; the received-date line from
the local OCR tokens) and passes them as (kind, jpeg). All crops of ONE form go in ONE request per reader slot, and the
answer uses the page schema, so the same consensus rules decide (engine.decide_field). A zoomed stamp is what the
model reads here, so no separate zoom veto is needed. If a field could not be located the caller sends the full page
instead (engine.extract_page); this module never does.
"""
import datetime as dt
import tempfile
from pathlib import Path

from . import engine, hand, orient, render, validate
from .prompt import PROMPT, SCHEMA

KINDS = {
    "handwriting": "handwriting with the printed label next to it (top: the original crop; bottom: the same crop with "
                   "only the coloured pen strokes kept)",
    "date_line": "the date line of a received stamp (\"התקבל\", printed like \"18 -06- 2026\") or a handwritten "
                 "receipt date, with what surrounds it",
    "central_annotation": "the lab's own handwritten note: the central number (6 digits, e.g. \"939158\"), usually "
                          "with the receipt date written under it (e.g. \"16/6/26\")",
}

_EXTRA = {"central_number": {"type": "string"},
          "central_number_legibility": SCHEMA["properties"]["received_date_legibility"]}
SCHEMA_CROPS = {**SCHEMA,
                "properties": {**{k: v for k, v in SCHEMA["properties"].items() if k != "form_dates"}, **_EXTRA},
                "required": [k for k in SCHEMA["required"] if k != "form_dates"] + list(_EXTRA)}
CENTRAL_DIGITS = 6

_RULES = "VISION ONLY" + PROMPT.split("VISION ONLY", 1)[1]


def prompt(kinds):
    legend = "\n".join(f"- image {i}: {KINDS[k]}" for i, k in enumerate(kinds, 1))
    return f"""You get {len(kinds)} images, all cut from ONE scanned Israeli medical "commitment" form (התחייבות) that
arrived at the Central Virology Laboratory. You do NOT see the whole page, only these regions:
{legend}

PATIENT (the person tested - not the physician, not the father, not the clerk, not the issuer):
- patient_first_name / patient_last_name: the patient's name in Hebrew, often in FLUENT HEBREW CURSIVE - convert each
  cursive letter to its printed letter. Only when a printed label inside the image says which word is which;
  otherwise leave both empty.
- patient_id: the patient's ID digits exactly as written, including leading zeros, only next to an ID label
  (ת.ז. / תעודת זהות / מספר זהות / ID / PASS / מ.ז.). patient_id_label: that printed label, copied exactly; "" if none.

RECEIVED DATE: only from a "date_line" image, or the date written under the central number in a "central_annotation"
image. Day, month, year exactly as written. received_date_source: "stamp" (inside the התקבל stamp), "handwritten"
(a handwritten receipt date) or "none".

CENTRAL NUMBER: only from a "central_annotation" image: the lab's handwritten number, all its digits exactly as
written (usually 6). Not the date under it, not a phone, barcode, ID or commitment number. "" if there is none.

A value that is not visible in these images does not exist for you: return "" and legibility "none".

{_RULES}"""


def _central(raw):
    """The lab's central number: exactly CENTRAL_DIGITS digits as written, else not a valid reading."""
    digits = "".join(ch for ch in raw or "" if ch.isdigit())
    if "?" in (raw or "") or len(digits) != CENTRAL_DIGITS or len(digits) != len((raw or "").strip()):
        return None
    return digits


def _not_before_form(entry, form_dates):
    if entry["status"] != "accepted":
        return entry
    received = dt.date.fromisoformat(entry["value"])
    printed = [d for d in (validate.parse_loose_date(x) for x in form_dates) if d and d <= dt.date.today()]
    if printed and received < max(printed):
        return dict(entry, value=None, status="review", reason="validator: before_form_date")
    return entry


def handwriting_crops(content, extension, page0):
    """[("handwriting", jpeg)] for the pen-colour handwriting of one page (located locally, no model)."""
    with tempfile.TemporaryDirectory(prefix="labx_crops_") as job:
        path = Path(job) / f"document{extension}"
        path.write_bytes(content)
        [(_, jpeg)] = list(render.pages(path, only={page0 + 1} if extension == ".pdf" else None))
    jpeg, _ = orient.upright(jpeg)
    boxes, size = hand.regions(jpeg)
    return [("handwriting", hand.crop_region(jpeg, b, size)) for b in boxes]


def extract(crops, readers=None, min_agree=engine.MIN_AGREE, form_dates=()):
    """crops: [(kind, jpeg)] of one form. Returns {"fields", "log"} like engine.extract_page."""
    kinds, images = [k for k, _ in crops], [j for _, j in crops]
    reads = engine._ask_all(engine._slots(readers), prompt(kinds), images, SCHEMA_CROPS)
    votes, _ = engine.page_votes(reads)
    for model, r, _ in reads:
        if r is not None:
            engine._vote(votes, "page", model, "central_number", r.get("central_number", ""),
                         r.get("central_number_legibility", "none"), _central)
    for v in votes:
        v["source"] = "crops"
    # the page path holds a date equal to a printed one for review (the reader may have taken the printed date); a
    # crop shows only the stamp / the lab's note, where the receipt date often IS the print day (part 1: p02, p04,
    # p19, p25). Kept: a receipt date before a date printed on the form is impossible.
    fields = {f: engine.decide_field(f, votes, [], min_agree) for f in engine.FIELDS}
    fields["received_date"] = _not_before_form(fields["received_date"], form_dates)
    fields["central_number"] = engine.decide_field("central_number", votes, [], min_agree)
    # the same digits accepted as the patient ID and as the central number: one of the two is misassigned
    if fields["central_number"]["status"] == "accepted" and fields["central_number"]["value"] in (
            fields["id"].get("readings") or []):
        fields["central_number"] = dict(fields["central_number"], value=None, status="review",
                                        reason="crops: central number equals an ID reading")
    # no whole-page reading to compare with: a crop edge can cut digits off ("223344" of "11223344") and two models
    # then agree on the cut value. A real Israeli ID passes the check digit; a cut-off one almost never does.
    if fields["id"]["status"] == "accepted" and not fields["id"].get("checksum_ok"):
        fields["id"] = dict(fields["id"], value=None, status="review",
                            reason="crops: ID check digit fails (digits may be cut off at the crop edge)")
    log = {"crop_kinds": kinds, "crop_reads": {m: (r if r is not None else {"_error": e}) for m, r, e in reads}}
    return {"fields": fields, "log": log}
