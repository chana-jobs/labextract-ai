"""Stage 2: for fields the whole-page pass left empty, locate the field, crop it at high resolution, re-read.

Safety: the crop is located by TWO different models and their boxes must overlap (so one model picking the
physician / father name instead of the patient is not enough), and each crop reader must report the printed label
it sees, which is checked against a patient-label allowlist / role blocklist.
"""
import io

from PIL import Image

from . import clients

LOCATE_PROMPT = """This is a scanned Israeli medical commitment form (may be rotated). Find these regions and give a
bounding box [ymin, xmin, ymax, xmax] on a 0-1000 scale for each, or [] if absent:
- patient_name: the PATIENT's handwritten or printed name together with its printed label (not the physician, father,
  clerk or issuer).
- patient_id: the PATIENT's ID number (ת.ז./תעודת זהות/ID) together with its printed label.
- received_date: the date written inside the "התקבל" rubber stamp, or the handwritten receipt date (usually under a
  handwritten 6-digit central number or inside an empty stamp frame). Include the whole stamp/date.
"""

LOCATE_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "array", "items": {"type": "integer"}} for k in ("patient_name", "patient_id", "received_date")},
    "required": ["patient_name", "patient_id", "received_date"],
    "additionalProperties": False,
}

READ_PROMPTS = {
    "patient_name": """This crop is from an Israeli medical form and shows the patient's name field. Transcribe the
handwritten/printed Hebrew name letter by letter. Report the printed label next to it exactly as printed.
Never guess: if any letter is not certain, legibility = "unsure". If you cannot read it, return "".
If the field shows a single combined name, put the first word in first_name and the rest in last_name only when
the label says which is which or the order is unambiguous; otherwise leave both "" and legibility "unsure".""",
    "patient_id": """This crop is from an Israeli medical form and shows the patient's ID number (ת.ז.). Copy every
digit exactly, including leading zeros. Report the printed label next to it exactly as printed. Never guess: if any
digit is not certain, legibility = "unsure".""",
    "received_date": """This crop shows a receipt date from an Israeli lab form: a rubber stamp "התקבל" with a date like
"18 -06- 2026", or a handwritten date. Give day, month and year exactly as written. Never guess: if any digit is not
certain (faint ink, stamp overlap), legibility = "unsure".""",
}

_LEG = {"type": "string", "enum": ["clear", "unsure", "none"]}
READ_SCHEMAS = {
    "patient_name": {"type": "object", "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"},
                                                      "label": {"type": "string"}, "legibility": _LEG},
                     "required": ["first_name", "last_name", "label", "legibility"], "additionalProperties": False},
    "patient_id": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"type": "string"}, "legibility": _LEG},
                   "required": ["id", "label", "legibility"], "additionalProperties": False},
    "received_date": {"type": "object", "properties": {"day": {"type": "string"}, "month": {"type": "string"},
                                                       "year": {"type": "string"}, "is_received_stamp_or_receipt_note": {"type": "boolean"},
                                                       "legibility": _LEG},
                      "required": ["day", "month", "year", "is_received_stamp_or_receipt_note", "legibility"],
                      "additionalProperties": False},
}

# labels that mean the value belongs to someone other than the patient
ROLE_BLOCK = ["רופא", "האב", "האם", "מנהל", "הופק", "מפיק", "מאשר", "חתימה", "פקס", "טלפון", "רישיון", "דוקטור", "ד\"ר", "מנפיק"]


def label_ok(label):
    lab = (label or "").strip()
    return not any(b in lab for b in ROLE_BLOCK)


def iou(a, b):
    if len(a) != 4 or len(b) != 4:
        return 0.0
    y0, x0 = max(a[0], b[0]), max(a[1], b[1])
    y1, x1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, y1 - y0) * max(0, x1 - x0)
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    u = area(a) + area(b) - inter
    return inter / u if u else 0.0


def union_box(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def crop(jpeg, box, pad=0.04, min_side=900):
    img = Image.open(io.BytesIO(jpeg))
    W, H = img.size
    y0, x0, y1, x1 = [v / 1000 for v in box]
    py, px = pad + (y1 - y0) * 0.3, pad + (x1 - x0) * 0.15
    l, t = max(0, int((x0 - px) * W)), max(0, int((y0 - py) * H))
    r, b = min(W, int((x1 + px) * W)), min(H, int((y1 + py) * H))
    c = img.crop((l, t, r, b))
    if max(c.size) < min_side:
        f = min_side / max(c.size)
        c = c.resize((int(c.width * f), int(c.height * f)), Image.LANCZOS)
    buf = io.BytesIO()
    c.convert("RGB").save(buf, "JPEG", quality=95)
    return buf.getvalue()


def locate(jpeg, locators):
    """Return {region: box} for regions where the two locators agree (IoU >= 0.2)."""
    boxes = []
    for slot in locators:
        try:
            boxes.append(clients.ask_slot(slot, LOCATE_PROMPT, jpeg, schema=LOCATE_SCHEMA)[1])
        except Exception:
            return {}
    agreed = {}
    for k in ("patient_name", "patient_id", "received_date"):
        bs = [b.get(k) or [] for b in boxes]
        if all(len(b) == 4 for b in bs) and all(iou(bs[0], b) >= 0.2 for b in bs[1:]):
            box = bs[0]
            for b in bs[1:]:
                box = union_box(box, b)
            agreed[k] = box
    return agreed


def read_region(crop_jpeg, region, readers):
    out = {}
    for slot in readers:
        try:
            m, r = clients.ask_slot(slot, READ_PROMPTS[region], crop_jpeg, schema=READ_SCHEMAS[region])
            out[m] = r
        except Exception as e:
            out[slot[0]] = {"_error": str(e)[:150]}
    return out
