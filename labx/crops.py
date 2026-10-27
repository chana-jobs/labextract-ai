"""Crops-first reading (privacy): only regions located LOCALLY are sent, never the whole page.

The caller locates the regions without a model (handwriting by pen colour, hand.regions; the received-date line from
the local OCR tokens) and passes them as (kind, jpeg). All crops of ONE form go in ONE request per reader slot, and the
answer uses the page schema, so the same consensus rules decide (engine.decide_field). A zoomed stamp is what the
model reads here, so no separate zoom veto is needed. If a field could not be located the caller sends the full page
instead (engine.extract_page); this module never does.
"""
import tempfile
from pathlib import Path

from . import engine, hand, orient, render
from .prompt import PROMPT, SCHEMA

KINDS = {
    "handwriting": "handwriting with the printed label next to it (top: the original crop; bottom: the same crop with "
                   "only the coloured pen strokes kept)",
    "date_line": "the date line of a received stamp (\"התקבל\", printed like \"18 -06- 2026\") or a handwritten "
                 "receipt date, with what surrounds it",
}

SCHEMA_CROPS = {**SCHEMA, "properties": {k: v for k, v in SCHEMA["properties"].items() if k != "form_dates"},
                "required": [k for k in SCHEMA["required"] if k != "form_dates"]}

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

RECEIVED DATE: only from a "date_line" image. Day, month, year exactly as written. received_date_source: "stamp"
(inside the התקבל stamp), "handwritten" (a handwritten receipt date) or "none".

A value that is not visible in these images does not exist for you: return "" and legibility "none".

{_RULES}"""


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
    for v in votes:
        v["source"] = "crops"
    fields = {f: engine.decide_field(f, votes, list(form_dates), min_agree) for f in engine.FIELDS}
    # no whole-page reading to compare with: a crop edge can cut digits off ("223344" of "11223344") and two models
    # then agree on the cut value. A real Israeli ID passes the check digit; a cut-off one almost never does.
    if fields["id"]["status"] == "accepted" and not fields["id"].get("checksum_ok"):
        fields["id"] = dict(fields["id"], value=None, status="review",
                            reason="crops: ID check digit fails (digits may be cut off at the crop edge)")
    log = {"crop_kinds": kinds, "crop_reads": {m: (r if r is not None else {"_error": e}) for m, r, e in reads}}
    return {"fields": fields, "log": log}
