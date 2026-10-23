"""Handwriting regions found deterministically from the colored-ink mask (no model guesses WHERE to look).

Each region is cropped with generous margins (so the printed label next to the writing is inside the crop) and sent
as [original crop] over [ink-only strokes] with a prompt that says it is fluent Hebrew cursive.
"""
import io

import cv2
import numpy as np
from PIL import Image

from . import clients, ink

HAND_PROMPT = """This image is a sheet of numbered PANELS ("#1", "#2", ...), each a crop from the same scanned Israeli
medical form. In each panel, TOP: the original crop; BOTTOM: the same crop with only the colored-pen handwriting kept
(dark strokes on white) - use it to see the strokes clearly. Answer once per panel (panel = its number).

The handwriting is FLUENT HEBREW CURSIVE (כתב יד עברי רהוט), written right-to-left, and/or digits. Hebrew cursive
letter shapes differ from print: י is a tiny hook/apostrophe at mid height; ו is a short vertical stroke; ל is the only
letter rising high above the line (like a tall "ℓ" or "f"); א often looks like "K" or "lc"; ש looks like a rounded "e"
or "C" with an inner stroke; ר is an open curve like a mirrored "c" with no corner; ס is a closed "o". Dots or commas
between words are separators, not letters.

Tasks:
1. handwriting: transcribe ALL the handwritten text in reading order, converting each cursive letter to its printed
   Hebrew letter; copy digits exactly incl. leading zeros. Separate words with a single space.
2. label: the printed (typed) label that this handwriting fills in or sits next to, copied exactly (e.g. "שם פרטי",
   "שם משפחה", "ת.ז.", "שם הנבדק", "ID / PASS"); "" if there is none.
3. For the PATIENT only (never a physician / father / clerk signature):
   - id: patient ID digits if the handwriting is the patient ID, else "".
   - name_words: the patient's name words in reading order (right to left), else [].
   - first_name / last_name: ONLY if a printed label tells which word is the first name and which is the family name
     (separate "שם פרטי" and "שם משפחה" fields, or a label like "שם ומשפחה" / "שם פרטי ושם משפחה"); otherwise "".
4. legibility: "clear" only if you are certain of EVERY letter and digit you returned; "unsure" otherwise.
Never invent or complete a name from typical names - transcribe the strokes."""

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "panel": {"type": "integer"},
        "handwriting": {"type": "string"},
        "label": {"type": "string"},
        "id": {"type": "string"},
        "name_words": {"type": "array", "items": {"type": "string"}},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "legibility": {"type": "string", "enum": ["clear", "unsure", "none"]},
    },
    "required": ["panel", "handwriting", "label", "id", "name_words", "first_name", "last_name", "legibility"],
    "additionalProperties": False,
}
HAND_SCHEMA = {"type": "object", "properties": {"panels": {"type": "array", "items": PANEL_SCHEMA}},
               "required": ["panels"], "additionalProperties": False}


def regions(jpeg, min_ink_px=250, max_regions=6):
    """Bounding boxes (l, t, r, b) in pixels of colored handwriting clusters, biggest first."""
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    m = ink.ink_mask(bgr)
    H, W = m.shape
    k = max(9, W // 60)
    merged = cv2.dilate(m, np.ones((k // 2, k * 2), np.uint8))  # join letters/words of one line
    n, lab, stats, _ = cv2.connectedComponentsWithStats(merged)
    boxes = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        ink_px = int((m[y:y + h, x:x + w] > 0).sum())
        if ink_px >= min_ink_px and h < H * 0.25:
            boxes.append((ink_px, (int(x), int(y), int(x + w), int(y + h))))
    boxes.sort(reverse=True)
    return [b for _, b in boxes[:max_regions]], (W, H)


def crop_region(jpeg, box, size):
    W, H = size
    l, t, r, b = box
    w, h = r - l, b - t
    mx, my = int(w * 0.6 + W * 0.08), int(h * 1.2 + H * 0.015)  # wide margin: labels sit beside the writing
    img = Image.open(io.BytesIO(jpeg)).crop((max(0, l - mx), max(0, t - my), min(W, r + mx), min(H, b + my)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=95)
    return ink.enhance(buf.getvalue(), scale=2.0)


def sheet(crops, width=1600):
    """Stack crops vertically, each under a numbered header bar, into one JPEG (one API call per reader)."""
    panels = []
    for i, c in enumerate(crops, 1):
        img = cv2.imdecode(np.frombuffer(c, np.uint8), cv2.IMREAD_COLOR)
        img = cv2.resize(img, (width, int(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
        bar = np.full((70, width, 3), 235, np.uint8)
        cv2.putText(bar, f"#{i}", (20, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.7, (0, 0, 200), 4)
        panels += [bar, img, np.full((16, width, 3), 90, np.uint8)]
    rgb = cv2.cvtColor(np.vstack(panels), cv2.COLOR_BGR2RGB)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def read_regions(jpeg, slots):
    """[{box, reads: {model: panel json}}] for every handwriting region on the page; one call per reader slot."""
    boxes, size = regions(jpeg)
    if not boxes:
        return []
    img = sheet([crop_region(jpeg, b, size) for b in boxes])
    out = [{"box": b, "reads": {}} for b in boxes]
    for slot in slots:
        try:
            model, r = clients.ask_slot(slot, HAND_PROMPT, img, schema=HAND_SCHEMA)
        except Exception as e:
            for o in out:
                o["reads"][slot[0]] = {"_error": str(e)[:150]}
            continue
        for p in r.get("panels", []):
            k = p.get("panel")
            if isinstance(k, int) and 1 <= k <= len(out):
                out[k - 1]["reads"][model] = p
    return out
