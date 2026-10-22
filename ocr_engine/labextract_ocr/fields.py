"""Field assignment — separate from text recognition. Input: OCR tokens + the oriented page; output: per field a
status and candidates, each with its evidence and location. Nothing is repaired, padded or guessed.

status:
  proposed   one value backed by direct evidence (label / stamp / sticker) and stable under local re-reads.
             Still a proposal: a person approves it.
  uncertain  candidates exist but evidence is weak, conflicting, handwritten or unstable — sent to review.
  missing    nothing found. NOT a confirmed absence.
"""
from __future__ import annotations

import re
import time

import cv2
from collections import Counter, defaultdict

from .layout import h_overlap, in_box, row_text_ltr, rows as make_rows, union
from .ocr import Token
from .textmatch import best_window, digits_only, letters_ratio, norm_heb, number_value, parse_date, similarity
from .orient import enhance
from .privacy import MASK, is_print_footer
from .templates import classify, commitment_rules, generic_commitment_allowed
from .barcodes import MANDATORY_CHECK, numeric
from .visual import (find_barcodes, match_stamp_template, probe_vertical_label, read_stamp, read_stamp_votes,
                     stamp_regions)

# ------------------------------------------------------------------ vocabularies
COMMITMENT_LABELS = ["מספר התחייבות", "התחייבות מספר", "מס התחייבות", "התחייבות", "מספר דרישה", "דרישה", "מספר בקשה",
                     "מספר דגימה", "דגימה"]
# rows where the word is not a number label: validity, issuer, form title, and sample date/material/time/type
COMMITMENT_STOP = ["תוקף", "מפיק", "טופס", "תאריך", "חומר", "שעת", "סוג", "רישיון", "רשיון"]
MIN_GAP_PX = 320                                       # label->value distance floor (~1.6 in at 200 dpi)
ID_LABELS_LONG = ["תעודת זהות", "מספר זהות", "מספר תעודת זהות"]
ID_LABELS_SHORT = ["תז", "מז", "id", "idpass"]          # matched exactly after normalization
FIRST_NAME_LABELS = ["שם פרטי"]
LAST_NAME_LABELS = ["שם משפחה"]
FULL_NAME_LABELS = ["שם המבוטח", "שם הנבדק", "שם החולה", "שם המטופל"]
RECEIVED_STAMP = "התקבל"
RECEIVER_CUES = ["לנגיפים", "המעבדה המרכזית", "המרכז הרפואי המשולב"]

# closed list: the APPROVED list data/Institutions.xlsx (855 records, 21.10) via labextract_ocr/institutions.py;
# curated aliases + automatic full-name keys; being on the list is not evidence of being the sender
from .institutions import (RECEIVER_SIDE, RECORDS, SENDERS, display, has_institution_word,  # noqa: E402
                           match_row)


# ------------------------------------------------------------------ helpers
def is_number_token(t: Token, min_digits: int) -> bool:
    """Exactly one digit group of >= min_digits, and the token is mostly digits."""
    d = digits_only(t.text)
    return (number_value(t.text, min_digits) is not None
            and len(d) / max(1, len(norm_heb(t.text))) >= 0.8)


def looks_like_date_or_phone(text: str) -> bool:
    if parse_date(text):
        return True
    return bool(re.search(r"\d-\d", text)) or bool(re.match(r"^0[2-9]-?\d{7}$", text.strip()))


def cand(value, token_or_box, evidence: list[str], conf=None, raw=None) -> dict:
    bbox = token_or_box.bbox if isinstance(token_or_box, Token) else list(token_or_box)
    return {"value": value, "raw": raw if raw is not None else value, "conf": conf, "bbox": bbox,
            "evidence": evidence}


def find_labels(rows, labels: list[str], min_sim: float) -> list[tuple[float, list[Token], str]]:
    """Best match per row for any label variant: (similarity, label tokens, label)."""
    found = []
    for row in rows:
        texts = [t.text for t in row]
        best = None
        for label in labels:
            s, i, j = best_window(texts, label)
            if s >= min_sim and (best is None or s > best[0]):
                best = (s, row[i:j], label)
        if best:
            found.append(best)
    return found


def left_of(row: list[Token], label_tokens: list[Token], max_gap_h: float = 14) -> list[Token]:
    """Tokens left of the label on its row (RTL: the value follows the label to the left), nearest first."""
    lx0 = min(t.bbox[0] for t in label_tokens)
    lh = max(t.h for t in label_tokens)
    ids = {t.id for t in label_tokens}
    gap = max(max_gap_h * lh, MIN_GAP_PX)
    out = [t for t in row if t.id not in ids and t.cx < lx0 and lx0 - t.bbox[2] <= gap]
    return sorted(out, key=lambda t: -t.cx)


def header_like(row: list[Token], label_tokens: list[Token]) -> bool:
    """A label with no words to its left on the row — like a table column header."""
    return not any(letters_ratio(t.text) >= 0.6 for t in left_of(row, label_tokens))


def reread_label(ocr, gray, label_tokens: list[Token], label: str) -> tuple[float, str]:
    """Re-recognize the whole label region in one crop (Paddle often splits/garbles label words)."""
    box = union([t.bbox for t in label_tokens])
    best = (0.0, "")
    for text, _ in ocr.reread(gray, box, pads=((4, 3), (10, 6))):
        s = similarity(text, label)
        if s > best[0]:
            best = (s, text)
    return best


def below_in_column(all_rows, label_tokens: list[Token], max_rows: int = 6) -> list[list[Token]]:
    lbox = union([t.bbox for t in label_tokens])
    width = lbox[2] - lbox[0]
    col = [lbox[0] - width, lbox[1], lbox[2] + width, lbox[3]]
    out = []
    for row in all_rows:
        if row[0].cy <= lbox[3]:
            continue
        hits = [t for t in row if col[0] <= t.cx <= col[2]]
        if hits:
            out.append(hits)
        if len(out) >= max_rows:
            break
    return out


PERSON_GUARDS = ["הודפס", "עבור", "רופא", "האב", "האם", "מפיק", "נותן", "המבצע", "מנהל", "חתימה", "רישיון", "רשיון", MASK]


def person_guarded(row, institutions: bool = False) -> bool:
    """Row belongs to someone other than the patient (print footer, physician, father, issuer, provider…).
    Prefix match per token: Paddle merges words ('האב:אריה'). With institutions=True a row naming a closed-list
    institution is also not a person row (e.g. 'שם: בי"ח …' of the service provider)."""
    if is_print_footer(row):
        return True
    for t in row:
        n = norm_heb(t.text)
        if any(n.startswith(norm_heb(g)) for g in PERSON_GUARDS if norm_heb(g)) or t.text == MASK:
            return True
    if institutions:
        text = norm_heb("".join(t.text for t in row))
        if any(len(norm_heb(a)) >= 4 and norm_heb(a) in text for aliases in SENDERS.values() for a in aliases):
            return True
    return False


def stable(ocr, gray, token: Token) -> tuple[bool, list[str]]:
    """Local re-read with wider crops; stable iff every re-read gives the same digits. A PDF text-layer token is an
    exact reading (no re-read)."""
    from .textlayer import is_text_layer
    if is_text_layer(token):
        return True, [number_value(token.text) or ""]
    reads = [number_value(text) or "" for text, _ in ocr.reread(gray, token.bbox)]
    base = number_value(token.text)
    return all(r == base for r in reads), reads


CONFIRM_POLICY = __import__("os").environ.get("LABOCR_CONFIRM", "engines")  # engines (approved 21.10) | strict


def _tesseract_digits(img) -> str:
    import subprocess
    import tempfile
    from pathlib import Path
    from .config import TESSERACT
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.png"
        cv2.imencode(".png", img)[1].tofile(str(p))
        out = subprocess.run([TESSERACT, str(p), "stdout", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789"],
                             capture_output=True, text=True, encoding="utf-8").stdout
    return number_value(out, 4) or ""


def confirm_reading(token, readings: list[str], tokens, gray, ocr, views, barcodes) -> dict:
    """Independent evidence for one of several conflicting readings of a number (same-model re-reads disagree).
    Readers that are NOT the same-model re-read: a decoded barcode (check character), the same digits as another
    token elsewhere on the page (other region), Tesseract on the 300-dpi crop (other engine), Paddle on the 300-dpi
    crop (other resolution). Returns {value|None, evidence, reads}. Policy 'strict': only barcode / second occurrence
    may confirm. Policy 'engines' (experiment): also Paddle@300 == Tesseract@300 == one of the 200-dpi readings.
    Any independent reader giving a DIFFERENT full-length value blocks confirmation."""
    reads = {"paddle200": list(dict.fromkeys(r for r in readings if r))}
    decoded = {b["text"] for b in numeric(barcodes or [])}
    others = {number_value(t.text) for t in tokens if t.id != token.id and is_number_token(t, 5)}
    hires = views["hires"]() if views and views.get("hires") else None
    if hires is not None:
        k = hires.shape[1] / gray.shape[1]
        x0, y0, x1, y1 = token.bbox
        px, py = int(16 * k), int(8 * k)
        crop = hires[max(0, int(y0 * k) - py):int(y1 * k) + py, max(0, int(x0 * k) - px):int(x1 * k) + px]
        if crop.size:
            reads["paddle300"] = number_value(ocr.recognize(crop)[0]) or ""
            reads["tess300"] = _tesseract_digits(crop)
    for v in reads["paddle200"]:
        if v in decoded:
            return {"value": v, "evidence": f"barcode decoded on the page = '{v}'", "reads": reads}
    for v in reads["paddle200"]:
        if v in others:
            return {"value": v, "evidence": f"same number read in another region of the page = '{v}'", "reads": reads}
    if CONFIRM_POLICY == "engines":
        # approved 21.10: two DIFFERENT engines (Paddle, Tesseract) agree on the same full number. Paddle at another
        # resolution is a supporting read only. A competing reading of the same or greater length from any
        # reader blocks promotion; only a SHORTER reading is treated as a clipped/partial read.
        t3, p3 = reads.get("tess300"), reads.get("paddle300")
        paddle_reads = set(reads["paddle200"]) | ({p3} if p3 else set())
        if t3 and t3 in paddle_reads:
            all_reads = paddle_reads | {t3}
            # a different reading of the same OR GREATER length blocks: a longer reading means the agreed value may
            # itself be clipped (p07: both engines dropped a digit and agreed on 25112010; truth 251120160)
            competitors = {r for r in all_reads if r != t3 and len(r) >= len(t3)}
            if not competitors:
                return {"value": t3, "reads": reads,
                        "evidence": f"Tesseract@300 and Paddle agree on the full number '{t3}' "
                                    f"(Paddle@300 = '{p3 or '-'}'); no competing full-length reading"}
            reads["blocked"] = f"competing full-length readings: {sorted(competitors)}"
    return {"value": None, "evidence": None, "reads": reads}


def reread_alternatives(c: dict, min_digits: int) -> list[dict]:
    """Differing re-reads become their own candidates (a reviewer sees every reading; none is chosen)."""
    alts = []
    for r in dict.fromkeys(c.get("rereads") or []):
        if r and r != c["value"] and len(r) >= min_digits:
            alts.append({**c, "value": r, "raw": r, "conf": None, "stable": False, "rereads": None,
                         "evidence": c["evidence"] + [f"wider-crop re-read of '{c['raw']}'"]})
    return alts


# ------------------------------------------------------------------ fields
def central_numbers(tokens, gray, ocr, rows, barcodes=None) -> tuple[dict, set[int]]:
    cands, used = [], set()
    for b in find_barcodes(gray):
        box = b[:4]
        bh = box[3] - box[1]
        nums = [t for t in tokens if is_number_token(t, 4) and not looks_like_date_or_phone(t.text)
                and h_overlap(box, t.bbox) > 0.4
                and (in_box(t, box) or 0 <= t.bbox[1] - box[3] < 0.8 * bh or 0 <= box[1] - t.bbox[3] < 0.8 * bh)]
        if not nums:
            continue
        probe = probe_vertical_label(ocr, gray, union([box] + [t.bbox for t in nums]))
        for t in nums:
            ev = [f"barcode_sticker@{box}"]
            if probe and probe["similarity"] >= 0.6:
                ev.append(f"vertical_label '{probe['text']}' sim={probe['similarity']} {probe['side']}")
                tier = "strong"
            elif probe and probe["similarity"] >= 0.4:
                ev.append(f"vertical_label_partial '{probe['text']}' sim={probe['similarity']}")
                tier = "medium"
            else:
                tier = "weak"
            c = cand(number_value(t.text), t, ev, t.conf, t.text)
            c["tier"], c["token_id"] = tier, t.id
            cands.append(c)
    for s, ltoks, label in find_labels(rows, ["מרכזי", "מספר מרכזי", "מס מרכזי"], 0.8):
        row = next(r for r in rows if ltoks[0] in r)
        for t in left_of(row, ltoks, 8)[:1]:
            if is_number_token(t, 4) and not looks_like_date_or_phone(t.text):
                c = cand(number_value(t.text), t, [f"label '{label}' sim={s:.2f} same row"], t.conf, t.text)
                c["tier"], c["token_id"] = "strong", t.id
                cands.append(c)
    # decoded barcodes: independent, check-character-verified reading; role still needs the vertical label
    disagreements = []
    strong_ocr = {c["value"]: c for c in cands if c["tier"] == "strong"}
    for b in numeric(barcodes or []):
        x0, y0, x1, y1 = b["bbox"]
        bh = max(10, y1 - y0)
        sticker = [x0, max(0, int(y0 - 0.6 * bh)), x1, int(y1 + 0.9 * bh)]
        under = [t for t in tokens if is_number_token(t, 4) and in_box(t, sticker)]
        for t in under:                                      # printed digits on the same sticker disagree?
            v = number_value(t.text)
            if v and v != b["text"] and (v in b["text"] or b["text"] in v or len(v) == len(b["text"])):
                disagreements.append((b["text"], v))
        if b["text"] in strong_ocr:                          # label already proven on this sticker: no 2nd probe
            strong_ocr[b["text"]]["barcode_verified"] = True
            strong_ocr[b["text"]]["evidence"].append(f"barcode decoded ({b['format']}, check character)")
            continue
        probe = probe_vertical_label(ocr, gray, union([sticker] + [t.bbox for t in under]))
        tier = ("strong" if probe and probe["similarity"] >= 0.6 else
                "medium" if probe and probe["similarity"] >= 0.4 else "weak")
        ev = [f"barcode decoded ({b['format']}, check character)"]
        if probe and probe["similarity"] >= 0.4:
            ev.append(f"vertical_label '{probe['text']}' sim={probe['similarity']} {probe['side']}")
        c = cand(b["text"], b["bbox"], ev, None, b["text"])
        c.update(tier=tier, token_id=None, barcode_verified=True)
        cands.append(c)
    # one entry per value, best tier wins; a barcode decode of the same value verifies the reading
    best = {}
    order = {"strong": 0, "medium": 1, "weak": 2}
    for c in cands:
        b = best.get(c["value"])
        if b is None:
            best[c["value"]] = c
            continue
        winner, other = (c, b) if order[c["tier"]] < order[b["tier"]] else (b, c)
        winner["barcode_verified"] = winner.get("barcode_verified") or other.get("barcode_verified")
        winner["evidence"] = list(dict.fromkeys(winner["evidence"] + other["evidence"]))
        winner["token_id"] = winner.get("token_id") or other.get("token_id")
        best[c["value"]] = winner
    cands = sorted(best.values(), key=lambda c: order[c["tier"]])
    strong = [c for c in cands if c["tier"] == "strong"]
    for c in strong:
        if c.get("token_id") is not None:
            ok, reads = stable(ocr, gray, next(t for t in tokens if t.id == c["token_id"]))
            c["rereads"] = reads
            used.add(c["token_id"])
        else:
            ok = False
        c["stable"] = ok or bool(c.get("barcode_verified"))   # verified by an independent reader, or stable
    conflict = [d for d in disagreements if any(c["value"] in d for c in strong)]
    if strong and all(c["stable"] for c in strong) and not conflict:
        status, reason = "proposed", "sticker with a vertical 'מרכזי' label; reading stable or barcode-verified"
        if any(c["tier"] == "medium" for c in cands):
            status, reason = "uncertain", "another sticker has a partial 'מרכזי' label — count not sure"
    elif conflict:
        status, reason = "uncertain", f"barcode and printed digits disagree: {conflict[:2]}"
    elif strong:
        status, reason = "uncertain", "sticker found but the number is unstable under re-read"
    elif cands:
        status, reason = "uncertain", "number on a barcode sticker, but no 'מרכזי' label read"
    else:
        status, reason = "missing", "no central-number sticker found"
    values = [c["value"] for c in strong] if status == "proposed" else []
    cands += [a for c in strong for a in reread_alternatives(c, 4)]
    return {"status": status, "values": values, "candidates": cands, "reason": reason}, used


def leumit_on_page(rows) -> bool:
    """Leumit read on the page (closed-list match, sim >= 0.8, not ambiguous). The challenge brief: at Leumit the
    commitment number may be labelled 'דגימה'."""
    leumit = display("NAME-ONLY:לאומית")
    return any(h["sim"] >= 0.8 and not h["ambiguous"] and any(display(r) == leumit for r in h["recs"])
               for row in rows for h in match_row(row))


def adjacent_row_value(rows, row, ltoks, label: str, sim: float, exclude: set[int], leumit: bool):
    """Value printed in the row directly ABOVE or BELOW its label (e.g. p08: number above 'דגימה:'), to the label's
    left (RTL), vertically close. Used only when the label's own row has no value. Bare 'דגימה' (a sample number
    on most forms) counts only on a Leumit page; other labels need a strong match (>= 0.8).
    Returns (token, evidence) or (None, None)."""
    bare_sample = norm_heb(label) == norm_heb("דגימה")
    if bare_sample and not leumit:
        return None, None
    if not bare_sample and sim < 0.8:
        return None, None
    i = rows.index(row)
    lx0 = min(t.bbox[0] for t in ltoks)
    lh = max(t.h for t in ltoks)
    lcy = sum(t.cy for t in ltoks) / len(ltoks)
    best = None
    for j in (i - 1, i + 1):
        if not 0 <= j < len(rows):
            continue
        for t in rows[j]:
            if (t.cx < lx0 and lx0 - t.bbox[2] <= MIN_GAP_PX and abs(t.cy - lcy) <= 2.5 * max(lh, 12)
                    and is_number_token(t, 5) and not looks_like_date_or_phone(t.text) and t.id not in exclude):
                d = abs(t.cy - lcy) + 0.1 * (lx0 - t.bbox[2])
                if best is None or d < best[0]:
                    best = (d, t, "above" if j < i else "below")
    if not best:
        return None, None
    why = f"adjacent row ({best[2]} the label)" + (", Leumit form: 'דגימה' = commitment" if bare_sample else "")
    return best[1], why


BARCODE_LABEL_WORD = "התחייבות"   # only an explicit commitment label may own a barcode (not דגימה / דרישה / בקשה)
BARCODE_MAX_GAP_PX = 250          # label bottom -> barcode top (~1.25 in at 200 dpi)


def human_readable_reads(ocr, gray, views, box) -> dict:
    """Read the printed digits that accompany a barcode (the band between its label and the bars), with two
    different engines on the 300-dpi view when available."""
    img, k = gray, 1.0
    hires = views["hires"]() if views and views.get("hires") else None
    if hires is not None:
        img, k = hires, hires.shape[1] / gray.shape[1]
    x0, y0, x1, y1 = (int(v * k) for v in box)
    crop = img[max(0, y0):y1, max(0, x0):x1]
    if not crop.size:
        return {}
    return {"paddle": number_value(ocr.recognize(crop)[0], 4) or "", "tesseract": _tesseract_digits(crop)}


def tesseract_text(gray, views, box, lang="heb", psm=7) -> str:
    """Tesseract (a different engine from Paddle) on a page region, 300-dpi view when available."""
    import subprocess
    import tempfile
    from pathlib import Path
    from .config import TESSERACT
    img, k = gray, 1.0
    hires = views["hires"]() if views and views.get("hires") else None
    if hires is not None:
        img, k = hires, hires.shape[1] / gray.shape[1]
    x0, y0, x1, y1 = box
    crop = img[max(0, int((y0 - 4) * k)):int((y1 + 4) * k), max(0, int((x0 - 6) * k)):int((x1 + 6) * k)]
    if not crop.size:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.png"
        cv2.imencode(".png", crop)[1].tofile(str(p))
        return subprocess.run([TESSERACT, str(p), "stdout", "-l", lang, "--psm", str(psm)], capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()


def tesseract_heb_label(gray, views, box) -> str:
    """Hebrew Tesseract (a different engine from Paddle) on a label region, 300-dpi view when available."""
    import subprocess
    import tempfile
    from pathlib import Path
    from .config import TESSERACT
    img, k = gray, 1.0
    hires = views["hires"]() if views and views.get("hires") else None
    if hires is not None:
        img, k = hires, hires.shape[1] / gray.shape[1]
    x0, y0, x1, y1 = box
    crop = img[max(0, int((y0 - 4) * k)):int((y1 + 4) * k), max(0, int((x0 - 6) * k)):int((x1 + 6) * k)]
    if not crop.size:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.png"
        cv2.imencode(".png", crop)[1].tofile(str(p))
        return subprocess.run([TESSERACT, str(p), "stdout", "-l", "heb", "--psm", "7"], capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()


def barcode_below_label(rows, ltoks, label: str, sim: float, barcodes, exclude_values: set[str], ocr, gray,
                        views) -> list[dict]:
    """Decoded barcode directly BELOW an explicit commitment label, in the label's column (p26: 'מספר התחייבות'
    over a printed number and its Code 39 bars). Role comes from the label + geometry; the decode is the reading.
    A 4-digit value is allowed here only because the role is explicit. No other words may sit between label and
    bars (they would make the barcode another field's). Symbologies without a mandatory check character need the
    printed digits read by BOTH local engines as the same value; otherwise the candidate stays weak.
    Returns candidate dicts (several -> the caller leaves them for review)."""
    if BARCODE_LABEL_WORD not in norm_heb(label) or sim < 0.7:
        return []
    lbox = union([t.bbox for t in ltoks])
    lw = lbox[2] - lbox[0]
    col = [lbox[0] - lw // 2, lbox[1], lbox[2] + lw // 2, lbox[3]]
    out = []
    for b in numeric(barcodes or []):
        x0, y0, x1, y1 = b["bbox"]
        v = b["text"]
        if not (4 <= len(v) <= 12) or v in exclude_values:
            continue
        if not (0 <= y0 - lbox[3] <= BARCODE_MAX_GAP_PX and col[0] <= (x0 + x1) / 2 <= col[2]
                and h_overlap(col, b["bbox"]) > 0.5):
            continue
        between = [t for t in (t for r in rows for t in r)
                   if lbox[3] < t.cy < y0 and t.bbox[2] > min(x0, lbox[0]) and t.bbox[0] < max(x1, lbox[2])
                   and letters_ratio(t.text) >= 0.6]
        if between:
            continue
        label_ev = f"label '{label}' sim={sim:.2f}"
        if sim < 0.8:                   # Paddle garbled the label: a DIFFERENT engine must read it (>= 0.9)
            text2 = tesseract_heb_label(gray, views, lbox)
            s2 = similarity(text2, label) if text2 else 0.0
            if s2 < 0.9:
                continue
            label_ev += f", re-read by Tesseract-heb@300 '{text2}' sim={s2:.2f}"
        ev = [f"{label_ev}; barcode {b['format']} decoded directly below the label "
              f"(gap {y0 - lbox[3]} px, same column)"]
        c = cand(v, b["bbox"], ev, None, v)
        c.update(token_id=None, source="barcode", label_sim=sim, rule=f"barcode below label '{label}'",
                 family="generic", barcode_format=b["format"])
        if b["format"] in MANDATORY_CHECK:
            c.update(role="strong", stable=True, barcode_verified=True)
            ev.append(f"{b['format']} has a mandatory check character")
        else:
            hr = human_readable_reads(ocr, gray, views, [min(x0, lbox[0]), lbox[3], max(x1, lbox[2]), y0])
            c["human_readable_reads"] = hr
            # two physically different marks must agree: the bars (decode) and the printed digits (an OCR engine).
            # As in the engines policy, any OCR reading of the same or greater length that differs blocks; a
            # shorter reading counts as partial.
            agree = [e for e, r in hr.items() if r == v]
            competing = {r for r in hr.values() if r and r != v and len(r) >= len(v)}
            if agree and not competing:
                c.update(role="strong", stable=True, barcode_verified=True)
                ev.append(f"{b['format']} has no mandatory check character; the printed digits read "
                          f"'{v}' by {'+'.join(agree)} = the decode; no competing full-length reading ({hr})")
            else:
                c.update(role="weak", stable=False)
                ev.append(f"{b['format']} has no mandatory check character and the printed digits do not "
                          f"confirm the decode: {hr}")
        out.append(c)
    return out


def reread_table_cells(notes, tokens, gray, ocr, views, barcodes) -> tuple[list[dict], list]:
    """A table row whose number cell OCR split / garbled (p25 row 3: '280005102' + '644'): re-read the WHOLE cell
    (column x-extent x row) as one crop and confirm with the approved engines policy (confirm_reading: Paddle
    re-reads, Tesseract@300, competing full-length readings block). Generic for numeric table columns; the
    value's role comes from the table family. Confirmed -> a strong candidate and that row no longer makes the
    count uncertain. Not confirmed -> every reading stays a weak candidate and the count stays uncertain.
    A length different from the column's other values never promotes (demote-only check)."""
    out, resolved = [], 0
    cells = [n for n in notes if isinstance(n, dict) and "unread_cell" in n]
    for i, n in enumerate(cells):
        box = n["unread_cell"]
        tok = Token(id=-100 - i, text="", conf=0.0, bbox=box)
        reads = [number_value(text, 6) or "" for text, _ in ocr.reread(gray, box)]
        res = confirm_reading(tok, reads, tokens, gray, ocr, views, barcodes)
        ev = [f"table cell re-read as one crop (OCR tokens {n['tokens']})"]
        lengths = n.get("column_lengths") or []
        base = {"bbox": box, "token_id": None, "token_ids": n["token_ids"], "source": "table_cell", "rule": "insurance table, column 'מספר'",
                "family": "insurance_table", "label_sim": 1.0, "confirm_reads": res["reads"], "conf": None}
        if res["value"] and (not lengths or len(res["value"]) in lengths):
            out.append({**base, "value": res["value"], "raw": res["value"], "role": "strong", "stable": True,
                        "evidence": ["family insurance_table: insurance table, column 'מספר'"] + ev
                                    + [f"confirmed: {res['evidence']}"]})
            resolved += 1
        else:
            why = (res["reads"].get("blocked") or "no two engines agree on the full number") if not res["value"] \
                else f"length {len(res['value'])} differs from the column's values {lengths}"
            all_reads = list(dict.fromkeys([r for r in res["reads"].get("paddle200", []) if r]
                                           + [res["reads"].get(k) for k in ("paddle300", "tess300")
                                              if res["reads"].get(k)]))
            for r in all_reads:
                out.append({**base, "value": r, "raw": r, "role": "weak", "stable": False,
                            "evidence": ["family insurance_table: insurance table, column 'מספר'"] + ev
                                        + [f"not confirmed: {why}"]})
    # every uncertain row without a resolved cell keeps the count uncertain
    uncertain_rows = sum(1 for n in notes if n == "count_uncertain")
    rest = [n for n in notes if not isinstance(n, dict) and n != "count_uncertain"]
    rest += ["count_uncertain"] * (uncertain_rows - resolved)
    return out, rest


def sample_declared_commitment(rows) -> bool:
    """A row stating that the sample number IS the commitment number (sample + commitment words in one row)."""
    for row in rows:
        words = [t.text for t in row]
        if (any(similarity(w, "הדגימה") >= 0.8 for w in words)
                and any(similarity(w, "התחייבות") >= 0.75 for w in words) and any(similarity(w, "מהווה") >= 0.8 for w in words)):
            return True
    return False


RELABEL_TARGETS = ["מספר דגימה", "מספר הדגימה", "מספר התחייבות", "מספר דרישה", "מספר בקשה"]


def garbled_label_candidates(rows, ocr, gray, views, exclude: set[int]) -> list[dict]:
    """Last resort when NO commitment label was found on the page (p19: label read 'מממה'): a number token whose
    right-hand neighbour (RTL: the label position) is a short word is re-read there by BOTH engines (Paddle with
    wider crops, Tesseract-heb@300). Only a two-word 'מספר …' label matched >= 0.8 by both engines counts.
    The number then goes through the normal stability / engines confirmation."""
    out = []
    for row in rows:
        if person_guarded(row):
            continue
        ltr = sorted(row, key=lambda t: t.cx)
        for i, t in enumerate(ltr):
            if not (is_number_token(t, 6) and not looks_like_date_or_phone(t.text) and t.id not in exclude):
                continue
            right = [u for u in ltr[i + 1:i + 2] if letters_ratio(u.text) >= 0.6 and 0 <= u.bbox[0] - t.bbox[2] <= MIN_GAP_PX]
            if not right:
                continue
            lab = right[0]
            box = [lab.bbox[0], lab.bbox[1], lab.bbox[2] + 2 * lab.h, lab.bbox[3]]   # label words may extend right
            p_reads = [x for x, _ in ocr.reread(gray, box, pads=((6, 4), (14, 6), (30, 8)))]
            t_read = tesseract_heb_label(gray, views, box)
            best = None
            for target in RELABEL_TARGETS:
                sp = max((similarity(x, target) for x in p_reads), default=0.0)
                st = similarity(t_read, target) if t_read else 0.0
                if sp >= 0.8 and st >= 0.8 and (best is None or min(sp, st) > best[0]):
                    best = (min(sp, st), target, sp, st)
            if not best:
                continue
            # a SAMPLE-number label is a commitment only where the page says so (p19: "מספר הדגימה מהווה מספר
            # התחייבות") or on a Leumit form
            if "דגימה" in best[1] and not (sample_declared_commitment(rows) or leumit_on_page(rows)):
                continue
            row_text = norm_heb("".join(u.text for u in row) + t_read)
            if any(norm_heb(w) in row_text for w in COMMITMENT_STOP):
                continue
            c = cand(number_value(t.text), t, [f"garbled label '{lab.text}' re-read as '{best[1]}' by Paddle "
                                               f"({best[2]:.2f}) and Tesseract-heb ('{t_read}', {best[3]:.2f})"],
                     t.conf, t.text)
            c.update(token_id=t.id, label_sim=best[0], rule=f"re-read label '{best[1]}'", family="generic",
                     role="strong")
            out.append(c)
    return out


def stop_near_label(row, ltoks) -> bool:
    """Stop words only count inside the label window or in the 2 tokens before it (RTL: to its right).
    v0.1 checked the whole row, which also blocked e.g. 'מספר בקשה N … תאריך …'."""
    i = row.index(ltoks[0])
    window = row[max(0, i - 2):i] + list(ltoks)
    text = norm_heb("".join(t.text for t in window))
    return any(norm_heb(w) in text for w in COMMITMENT_STOP)


def commitment_numbers(tokens, gray, ocr, rows, exclude: set[int], fam: dict, barcodes=None,
                       views=None, exclude_values: set[str] = frozenset()) -> tuple[dict, set[int]]:
    """Family rules first (role from layout, templates.py); generic label rules only for pages whose family
    does not define the commitment layout. Proposal needs: strong role evidence for every value, stable
    re-reads, no count uncertainty, and one distinct value unless the values come from a table rule."""
    cands, used = [], set()
    fcands, notes = commitment_rules(fam, rows)
    for c in fcands:
        if c["token_id"] not in exclude:
            c["label_sim"] = 1.0
            cands.append(c)
    cell_cands, notes = reread_table_cells(notes, tokens, gray, ocr, views, barcodes)
    cands += cell_cands
    generic = generic_commitment_allowed(fam)
    for s, ltoks, label in (find_labels(rows, COMMITMENT_LABELS, 0.55) if generic else []):
        row = next(r for r in rows if ltoks[0] in r)
        if stop_near_label(row, ltoks):
            continue
        label_note = ""
        if s < 0.8:                                        # weak label: re-read it before trusting it
            s2, text2 = reread_label(ocr, gray, ltoks, label)
            if s2 > s:
                s, label_note = s2, f" (label re-read '{text2}')"
        nearby = [t for t in left_of(row, ltoks, 10)[:3]
                  if is_number_token(t, 5) and not looks_like_date_or_phone(t.text) and t.id not in exclude]
        where = "same row" + label_note
        if not nearby:
            adj, why = adjacent_row_value(rows, row, ltoks, label, s, exclude, leumit_on_page(rows))
            if adj:
                nearby, where = [adj], why + label_note
        if not nearby:
            bc = barcode_below_label(rows, ltoks, label, s, barcodes, exclude_values, ocr, gray, views)
            if bc:
                for c in bc:
                    c["evidence"][0] += label_note
                cands += bc
                continue
        if not nearby and s >= 0.9 and len(label.split()) >= 2 and header_like(row, ltoks):
            for col_row in below_in_column(rows, ltoks):
                nums = [t for t in col_row if is_number_token(t, 5) and not looks_like_date_or_phone(t.text)
                        and t.id not in exclude]
                if not nums:
                    if nearby:
                        break
                    continue
                nearby += nums
            where = "column below header"
        for t in nearby if where == "column below header" else nearby[:1]:
            c = cand(number_value(t.text), t, [f"label '{label}' sim={s:.2f} {where}"], t.conf, t.text)
            c.update(token_id=t.id, label_sim=s, rule=f"generic label '{label}'", family="generic",
                     role="strong" if s >= 0.8 else "weak")
            cands.append(c)
    if generic and not cands:
        cands += garbled_label_candidates(rows, ocr, gray, views, exclude)
    occurrences = Counter(c["value"] for c in cands)
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda c: (c["role"] != "strong", -c["label_sim"])):
        if c["value"] not in seen:
            seen.add(c["value"]); uniq.append(c)
    for c in uniq:
        if c.get("source") in ("barcode", "table_cell"):  # reading already decided by the rule that made it
            c["occurrences"] = occurrences[c["value"]]
            used.update(c.get("token_ids") or [])
            continue
        ok, reads = stable(ocr, gray, next(t for t in tokens if t.id == c["token_id"]))
        c["rereads"], c["stable"] = reads, ok
        decoded = {b["text"] for b in numeric(barcodes or [])}
        if c["value"] in decoded:                       # independent reader (check character) agrees
            c["stable"], c["barcode_verified"] = True, True
            c["evidence"].append("same number decoded from a barcode on the page")
        elif not ok:
            hit = [r for r in reads if r in decoded]       # a wider re-read equals a decoded barcode
            if hit:
                c["evidence"].append(f"'{c['value']}' clipped; wider re-read = decoded barcode {hit[0]}")
                c.update(value=hit[0], raw=hit[0], stable=True, barcode_verified=True)
        c["occurrences"] = occurrences[c["value"]]
        if c["occurrences"] > 1:
            c["evidence"].append(f"same number read {c['occurrences']}x on the page")
        if not c["stable"]:                              # same-model re-reads disagree: look for independent evidence
            tok = next(t for t in tokens if t.id == c["token_id"])
            orig = c["value"]
            res = confirm_reading(tok, [orig] + list(c.get("rereads") or []), tokens, gray, ocr, views, barcodes)
            c["confirm_reads"] = res["reads"]
            if res["value"]:
                c["rereads"] = [r for r in list(c.get("rereads") or []) + [orig] if r != res["value"]]
                c.update(value=res["value"], raw=res["value"], stable=True)
                c["evidence"].append(f"conflicting re-reads resolved by independent evidence: {res['evidence']}")
        used.add(c["token_id"])
    table_only = uniq and all(c.get("family") == "insurance_table" for c in uniq)
    count_uncertain = "count_uncertain" in notes
    if (uniq and all(c["role"] == "strong" and c["stable"] for c in uniq) and not count_uncertain
            and (len(uniq) == 1 or table_only)):
        status, reason = "proposed", f"{uniq[0]['rule']}; stable re-reads"
    elif uniq:
        why = []
        if count_uncertain:
            why.append("a further table row could not be read (count uncertain)")
        if len(uniq) > 1 and not table_only:
            why.append("several different numbers")
        if any(c["role"] != "strong" for c in uniq):
            why.append("weak label")
        if any(not c["stable"] for c in uniq):
            why.append("unstable re-read")
        status, reason = "uncertain", "; ".join(why) or "not confirmed"
    else:
        status, reason = "missing", "no number found for a commitment role (may be absent — not confirmed)"
    for c in uniq:                                       # one commitment row per OCR place (token / cell / barcode)
        c["item"] = f"tok{c['token_id']}" if c.get("token_id") is not None else \
            f"{c.get('source', 'box')}{'_'.join(str(int(v)) for v in c['bbox'])}"
    candidates = uniq + [a for c in uniq for a in reread_alternatives(c, 5)]
    items = commitment_items(candidates, table_only)
    return {"status": status, "values": [c["value"] for c in uniq] if status == "proposed" else [],
            "count_uncertain": count_uncertain or status != "proposed",
            # complete_list: the legacy status/values describe the WHOLE list; items describe each row
            "complete_list": status == "proposed" and not count_uncertain,
            "families": sorted(fam), "candidates": candidates, "items": items,
            "reason": reason}, used


def commitment_items(candidates, table_only: bool) -> list[dict]:
    """One record per commitment ROW (an OCR place: token, table cell or barcode). Different readings of the same
    place are candidates of ONE item, never separate items. Item status, independent of the list:
      proposed   one value, strong role, stable / independently confirmed — still PENDING human approval
      conflict   several readings of the same place (all kept as candidates; none is chosen)
      uncertain  one reading but weak role or unstable
    Several rows from generic labels (not a table family) may be the same commitment read twice or a different
    number: those rows stay uncertain. Existence (is there a commitment here?) is separate from the number."""
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        groups.setdefault(c["item"], []).append(c)
    multi_generic = len(groups) > 1 and not table_only
    # main = the candidate the rule produced (not a re-read alternative); strong first
    for cs in groups.values():
        cs.sort(key=lambda c: (c["role"] != "strong", not c.get("stable"), "wider-crop re-read" in " ".join(c["evidence"])))
    items = []
    for key, cs in groups.items():
        main = cs[0]
        values = list(dict.fromkeys(c["value"] for c in cs))
        reasons = []
        if main["role"] == "strong" and main["stable"]:
            status = "proposed"                          # stable or independently confirmed; other readings of
            # the place (e.g. a clipped re-read the confirmation rejected) stay listed as candidates
        elif len(values) > 1:
            status = "conflict"
            reasons.append("conflicting readings of the same place")
        else:
            status = "uncertain"
            reasons.append("weak role evidence" if main["role"] != "strong" else "unstable reading")
        if multi_generic and status == "proposed":
            status = "uncertain"
            reasons.append("several different numbers from generic labels (may be one commitment)")
        # a table row with digits in the number column exists even when its number is unreadable (p25 row 3)
        existence = ("confirmed" if (main["role"] == "strong" or main.get("source") == "table_cell")
                     and not multi_generic else "uncertain")
        items.append({"item_key": key, "status": status, "value": values[0] if status == "proposed" else None,
                      "candidate_values": values, "bbox": main["bbox"], "existence": existence,
                      "rule": main.get("rule"), "evidence": main["evidence"], "uncertainty": reasons,
                      "approval": "PENDING"})
    items.sort(key=lambda it: (it["bbox"][1], -it["bbox"][0]))
    for i, it in enumerate(items):
        it["item_id"] = f"c{i + 1}"                      # order on the page (top to bottom, RTL)
    return items


def israeli_id_check(d: str) -> bool:
    """Israeli ID check digit on the zero-padded 9-digit form. AUXILIARY: a failure demotes, a pass never promotes
    on its own and nothing is ever repaired."""
    d = d.lstrip("0").zfill(9) if d else ""
    if len(d) != 9 or not d.isdigit():
        return False
    return sum(sum(divmod(int(c) * (1 + i % 2), 10)) for i, c in enumerate(d)) % 10 == 0


def id_labels(rows):
    labels = find_labels(rows, ID_LABELS_LONG, 0.75)
    for row in rows:
        for t in row:
            if norm_heb(t.text) in ID_LABELS_SHORT:
                labels.append((1.0, [t], t.text))
    return labels


def patient_regions(rows, shape, field: str) -> list[dict]:
    """Where the PATIENT's value should be, from its label: left of a Hebrew label (RTL), right of a Latin label
    ('ID / PASS:'), or below a table header with nothing to its left. Rows of other persons / institutions are
    skipped. For human review when the value cannot be read (handwriting) — a region is never a value."""
    H, W = shape[:2]
    if field == "israeli_id":
        labels, inst = id_labels(rows), False
    else:
        labels = find_labels(rows, (FIRST_NAME_LABELS if field == "first_name" else LAST_NAME_LABELS)
                             + FULL_NAME_LABELS, 0.75)
        inst = True
    out = []
    for s, ltoks, label in labels:
        row = next(r for r in rows if ltoks[0] in r)
        if person_guarded(row, institutions=inst):
            continue
        b = union([t.bbox for t in ltoks])
        lh = max(12, b[3] - b[1])
        if all(t.text.isascii() for t in ltoks):
            box, where = [b[2], b[1] - lh, min(W, b[2] + int(0.35 * W)), b[3] + lh], "right of the Latin label"
        elif header_like(row, ltoks) and len(row) >= 3:
            box, where = [max(0, b[0] - (b[2] - b[0])), b[3], min(W, b[2] + (b[2] - b[0])), min(H, b[3] + 6 * lh)], \
                "below the table header"
        else:
            box, where = [max(0, b[0] - int(0.35 * W)), b[1] - lh, b[0], b[3] + lh], "left of the label"
        out.append({"bbox": [int(v) for v in box], "label": label, "label_sim": round(s, 2),
                    "evidence": f"REVIEW HINT, not a value: label '{label}' sim={s:.2f}; value expected {where} "
                                f"(part 1: the region held the patient's value on ~40% of ID / ~20% of name pages)"})
    return out[:3]


def israeli_id(tokens, gray, ocr, rows, exclude: set[int], exclude_values: set[str] = frozenset(),
               views=None) -> dict:
    labels = id_labels(rows)
    cands = []
    for s, ltoks, label in labels:
        row = next(r for r in rows if ltoks[0] in r)
        if person_guarded(row):                    # print footer / physician / issuer / father: not the patient
            continue
        latin = all(t.text.isascii() for t in ltoks)
        lx1 = max(t.bbox[2] for t in ltoks)
        side = (sorted([t for t in row if t.cx > lx1 and t.bbox[0] - lx1 <= MIN_GAP_PX], key=lambda t: t.cx)
                if latin else left_of(row, ltoks, 12))      # Latin "ID/PASS:" is LTR: the value is to the right
        where = "same row"
        no_digits = not any(ch.isdigit() for t in row for ch in t.text)
        if (not any(is_number_token(t, 5) for t in side) and not latin and (header_like(row, ltoks) or no_digits)
                and len(row) >= 3):                          # table header: the value is in the column below
            lb = union([t.bbox for t in ltoks]); lw = lb[2] - lb[0]
            lcx = (lb[0] + lb[2]) / 2
            below = [r for r in rows if r[0].cy > lb[3]][:2]
            col = [t for r in below if not person_guarded(r) for t in r if is_number_token(t, 5)
                   and t.bbox[2] >= lb[0] - 2 * lw and t.bbox[0] <= lb[2] + 2 * lw]
            side = sorted(col, key=lambda t: abs(t.cx - lcx))[:1]
            where = "column below the table header"
        for t in side[:3]:
            d = number_value(t.text)
            # next to an explicit ID label only a date or a DASHED phone is excluded: an undashed 0X-digit ID with a
            # leading zero has the same shape as a landline number (synthetic check 22.10)
            if (is_number_token(t, 5) and not (parse_date(t.text) or re.search(r"\d-\d", t.text))
                    and t.id not in exclude
                    and d not in exclude_values):          # a value already in another role is not an ID
                c = cand(d, t, [f"label '{label}' sim={s:.2f} {where}"], t.conf, t.text)
                ok, reads = stable(ocr, gray, t)
                if not ok:                                   # conflicting same-engine re-reads: second engine
                    res = confirm_reading(t, [d] + reads, tokens, gray, ocr, views, None)
                    if res["value"]:
                        reads = [r for r in reads + [d] if r != res["value"]]
                        d, ok = res["value"], True
                        c.update(value=d, raw=d, conf=0.95)
                        c["evidence"].append(f"conflicting re-reads resolved: {res['evidence']}")
                c.update(rereads=reads, stable=ok, digits=len(d), check_digit=israeli_id_check(d))
                c["evidence"].append("ID check digit (optional note, not a condition): " +
                                     ("passes" if c["check_digit"] else "does not pass"))
                cands.append(c)
                break
    uniq = {c["value"]: c for c in cands}
    cands = list(uniq.values())
    regions = patient_regions(rows, gray.shape, "israeli_id")
    # 22.10 (challenge team): the experiment IDs are fictitious -> the check digit is an OPTIONAL note only,
    # never a condition for proposal / approval / export
    good = [c for c in cands if c["stable"] and (c["conf"] or 0) >= 0.9 and 7 <= c["digits"] <= 10]
    if len(good) == 1 and len(cands) == 1:
        return {"status": "proposed", "value": good[0]["value"], "candidates": cands, "regions": regions,
                "reason": "digits next to an ID label; confident and stable"}
    if cands:
        return {"status": "uncertain", "value": None, "regions": regions,
                "candidates": cands + [a for c in cands for a in reread_alternatives(c, 5)],
                "reason": "ID-label value is low-confidence, unstable, partial or conflicting (often handwritten)"}
    return {"status": "missing", "value": None, "candidates": [], "regions": regions,
            "reason": ("ID label found but no readable digits (handwriting?) - region kept for review" if regions
                       else "no digits next to an ID label")}


NAME_LABELS_FIELD = {"first_name": ["שם פרטי", "first name"], "last_name": ["שם משפחה", "last name", "surname"]}
NAME_LABELS_FULL = FULL_NAME_LABELS + ["שם מלא", "שם החבר", "name", "full name"]
# words that end a name value: other labels / form words (a value never contains them)
NAME_STOP = {norm_heb(w) for w in ["שם", "פרטי", "משפחה", "תז", "מז", "מספר", "זהות", "תעודת", "תאריך", "לידה", "גיל",
                                   "מין", "האב", "האם", "הרופא", "רופא", "טלפון", "כתובת", "מבוטח", "המבוטח",
                                   "הנבדק", "החולה", "המטופל", "קופה", "קופח", "מלא", "id", "pass", "name", "dob",
                                   "first", "last", "surname", "sex", "age", "tel",
                                   # values / labels of OTHER fields that sit in patient rows (dev 22.10)
                                   "זכר", "נקבה", "male", "female", "מס", "קבלה", "רשומה", "מחלקה", "סוג", "מצב",
                                   "משפחתי", "ת.ד", "מיקוד", "נייד", "פקס", "חבר", "קופת", "חולים", "כללית",
                                   "מכבי", "מאוחדת", "לאומית"]}
NAME_CHARS = re.compile(r"^[א-תA-Za-z'\"\-]+$")


def _unread_ink(gray, x0, x1, y0, y1, row) -> bool:
    """Dark ink between a label and a candidate value that no OCR token of the row explains = the real value is
    there but unread (typically handwriting); the next printed word is then NOT the value."""
    if gray is None or x1 - x0 < 4:
        return False
    m = (gray[max(0, y0):y1, max(0, x0):x1] < 128).astype("uint8")
    for t in row:
        tx0, ty0, tx1, ty1 = (int(v) for v in t.bbox)
        m[max(0, ty0 - y0):max(0, ty1 - y0), max(0, tx0 - x0):max(0, tx1 - x0)] = 0
    return int(m.sum()) >= 3 * max(8, y1 - y0)


def _name_words(row, ltoks, all_rows, gray=None):
    """Value tokens of a name label: RTL -> left of the label, Latin -> right of it; up to the next label / form word,
    digits or a wide gap, at most 3 words. Below the label when the label row is a table header with no value."""
    latin = all(t.text.isascii() for t in ltoks)
    lx0, lx1 = min(t.bbox[0] for t in ltoks), max(t.bbox[2] for t in ltoks)
    lh = max(t.h for t in ltoks)
    ids = {t.id for t in ltoks}
    side = (sorted([t for t in row if t.id not in ids and t.cx > lx1], key=lambda t: t.cx) if latin
            else [t for t in left_of(row, ltoks, 12) if t.id not in ids])
    words, edge = [], (lx1 if latin else lx0)
    ly0, ly1 = min(t.bbox[1] for t in ltoks), max(t.bbox[3] for t in ltoks)
    for t in side:
        txt = t.text.strip().strip(":").strip()
        gap = (t.bbox[0] - edge) if latin else (edge - t.bbox[2])
        # the value is ADJACENT to its label (first word <= 2 line heights, then <= 1.5): a printed word further
        # away belongs to the next field when the real value is unread handwriting (dev p06 / p12 / p13)
        limit = (2.0 if not words else 1.5) * lh
        span = (edge, t.bbox[0]) if latin else (t.bbox[2], edge)
        if (not txt or norm_heb(txt) in NAME_STOP or t.text.rstrip().endswith(":") or any(ch.isdigit() for ch in txt)
                or not NAME_CHARS.match(txt) or gap > limit or len(words) == 3
                or _unread_ink(gray, int(span[0]), int(span[1]), int(ly0), int(ly1), row)):
            break
        words.append(t)
        edge = t.bbox[2] if latin else t.bbox[0]
    where = "same row"
    if not words:                                       # table header: the value sits in the column below
        for r in below_in_column(all_rows, ltoks, 1):
            cand_w = [t for t in sorted(r, key=lambda t: -t.cx) if NAME_CHARS.match(t.text.strip().strip(":"))
                      and norm_heb(t.text) not in NAME_STOP]
            if cand_w and not person_guarded(r, institutions=True):
                words, where = cand_w[:3], "column below the table header"
    if words and any("א" <= ch <= "ת" for t in words for ch in t.text):
        words = sorted(words, key=lambda t: -t.cx)          # Hebrew value: reading order = right to left
    elif words:
        words = sorted(words, key=lambda t: t.cx)
    return words, where


def _name_stable(ocr, gray, words, views=None) -> bool:
    """Exact text layer; or per word: Paddle confident (>= 0.9) and identical wider-crop re-reads; or Paddle >= 0.7
    and a DIFFERENT engine (Tesseract heb+eng @300 dpi) reads the same word. Otherwise review."""
    from .textlayer import is_text_layer
    if all(is_text_layer(t) for t in words):
        return True
    for t in words:
        w = norm_heb(t.text.strip().strip(":"))
        same_paddle = t.conf >= 0.9 and all(norm_heb(text.strip().strip(":")) == w
                                            for text, _ in ocr.reread(gray, t.bbox))
        if same_paddle:
            continue
        if t.conf < 0.7:
            return False
        lang = "eng" if t.text.isascii() else "heb"
        t2 = tesseract_text(gray, views, t.bbox, lang, 7)
        if norm_heb(t2.strip().strip(":")) != w:
            return False
    return True


def names(tokens, gray, ocr, rows, views=None) -> tuple[dict, dict]:
    """Patient first / last name. Proposed (PENDING) only when ALL hold:
      - a FIELD-SPECIFIC label ('שם פרטי' / 'שם משפחה' / first / last name) - the first/last split is evidence;
      - the row is not another person's / an institution's (physician, father, issuer, print footer, insurer);
      - the value is letters only (Hebrew or English), up to the next label, and read reliably (text layer, or
        confident + identical local re-reads);
      - one distinct value for the field.
    A full-name label ('שם הנבדק', 'שם מלא', 'Name') gives review candidates only: no evidence for the split.
    Handwritten names are not readable locally: their regions stay for review."""
    out = {}
    hits = {"first_name": [], "last_name": [], "full": []}
    for key, labels in (("first_name", NAME_LABELS_FIELD["first_name"]), ("last_name", NAME_LABELS_FIELD["last_name"]),
                        ("full", NAME_LABELS_FULL)):
        for s, ltoks, label in find_labels(rows, labels, 0.75):
            row = next(r for r in rows if ltoks[0] in r)
            if person_guarded(row, institutions=True):
                continue
            words, where = _name_words(row, ltoks, rows, gray)
            if words:
                hits[key].append((s, label, words, where))
    for field in ("first_name", "last_name"):
        cands = []
        for s, label, words, where in hits[field]:
            text = " ".join(t.text.strip().strip(":") for t in words)
            c = cand(text, union([t.bbox for t in words]), [f"label '{label}' sim={s:.2f} {where}"],
                     min(t.conf for t in words), text)
            c["stable"] = _name_stable(ocr, gray, words, views)
            c["role"] = "strong" if s >= 0.8 else "weak"
            cands.append(c)
        for s, label, words, where in hits["full"]:
            text = " ".join(t.text.strip().strip(":") for t in words)
            c = cand(text, union([t.bbox for t in words]),
                     [f"full-name label '{label}' sim={s:.2f} {where}; first/last split not evidenced - review"],
                     min(t.conf for t in words), text)
            c.update(stable=False, role="weak")
            cands.append(c)
            if len(words) > 1:                              # one-click choice for the reviewer
                for t in words:
                    w = t.text.strip().strip(":")
                    cw = cand(w, t, [f"one word of the full name under '{label}' - review (split not evidenced)"],
                              t.conf, w)
                    cw.update(stable=False, role="weak")
                    cands.append(cw)
        good = [c for c in cands if c["role"] == "strong" and c["stable"]]
        values = {norm_heb(c["value"]) for c in cands if c["role"] == "strong"}
        if good and len(values) == 1:
            out[field] = {"status": "proposed", "value": good[0]["value"], "candidates": cands,
                          "reason": "field-specific patient label; letters only; reliable reading; one value"}
        else:
            out[field] = {"status": "uncertain" if cands else "missing", "value": None, "candidates": cands,
                          "reason": ("full-name label only (split not evidenced), weak / unstable reading or "
                                     "conflicting values" if cands else
                                     "no readable name next to a patient label (handwriting?) - region for review")}
        out[field]["regions"] = patient_regions(rows, gray.shape, field)
    return out["first_name"], out["last_name"]


MAX_STAMP_REGIONS = 4
STAMP_TEMPLATE_MIN = 0.30      # speed gate: engine-scored real stamps 0.308-0.763 on part 1; 15/26 pages below


def template_stamp_votes(gray, ocr, views: dict) -> dict | None:
    """Locate the stamp design and read it in up to 4 renderings: enhanced/raw at 200 dpi, raw/adaptive at the
    native 300 dpi of the scan (13-10 recommendation: read the stamp from the original resolution)."""
    m = match_stamp_template(gray)
    if not m or m["score"] < STAMP_TEMPLATE_MIN:     # speed gate only (never identity evidence)
        return None
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = m["bbox"]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 50 or y1 - y0 < 50:
        return None
    # 2 renderings only: raw 200 dpi + raw native 300 dpi. stamp_read_test: the true date appeared in both raw
    # variants on every stamp where any variant read it; enhanced/adaptive added time and wrong dates.
    variants = [("raw200", (views.get("raw") if views.get("raw") is not None else gray)[y0:y1, x0:x1])]
    hires = views["hires"]() if views.get("hires") else None
    if hires is not None:
        k = hires.shape[1] / W
        variants.append(("raw300", hires[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)]))
    r = read_stamp_votes(ocr, variants, m["angle"])
    r.update(score=m["score"], bbox=[x0, y0, x1, y1])
    return r


def stamp_received_date(tokens, gray, ocr, rows, views: dict | None = None) -> dict:
    """Form 1 — the 'התקבל' stamp: locate candidate regions (partial top word, fixed bottom lines, frame outline), re-read
    each region locally with small deskew angles, and accept a date only from a region proven to be the stamp:
    'התקבל' read at its top, or its lab lines at the bottom AND the stamp's dash date style (dd -mm- yyyy)."""
    cands = []
    for region in stamp_regions(tokens, gray)[:MAX_STAMP_REGIONS]:
        r = read_stamp(ocr, gray, region)
        top_sim = r["top"][1] if r["top"] else 0.0
        for d in r["dates"]:
            dash_style = "-" in d["raw"]
            if not (top_sim >= 0.5 or (r["bottom"] and dash_style)):
                continue
            ev = [f"stamp region from {region['source']}", f"deskew {d['angle']}°"]
            if r["top"]:
                ev.append(f"top text '{r['top'][0]}' ~ 'התקבל' sim={top_sim}")
            if r["bottom"]:
                ev.append(f"bottom text '{r['bottom'][0]}' sim={r['bottom'][1]}")
            year_digits = len(re.findall(r"\d+", d["raw"])[-1]) if re.findall(r"\d+", d["raw"]) else 0
            if year_digits != 4:              # the stamp prints a 4-digit year: a short year is a partial read
                ev.append(f"year read with {year_digits} digits — partial read, not proposed")
            c = cand(d["value"], region["bbox"], ev, d["conf"], d["raw"])
            c["top_sim"] = top_sim if year_digits == 4 else 0.0
            cands.append(c)
    by_value = {}
    for c in sorted(cands, key=lambda c: -c["top_sim"]):
        by_value.setdefault(c["value"], c)
    cands = list(by_value.values())
    strong = [c for c in cands if c["top_sim"] >= 0.6]
    if len(cands) == 1 and strong and 2020 <= int(strong[0]["value"][:4]) <= 2035:
        return {"status": "proposed", "value": strong[0]["value"], "candidates": cands,
                "reason": "date inside a region proven to be the 'התקבל' stamp"}
    # second pass: the stamp design located by template matching, read in several renderings (votes)
    tv = template_stamp_votes(gray, ocr, views or {})
    if tv:
        for iso, n in sorted(tv["votes"].items(), key=lambda kv: -kv[1]):
            ev = [f"stamp template region (match score {tv['score']}, not identity evidence)",
                  f"{n}/{tv['n_variants']} renderings read this date: {tv['raws'][iso][:3]}"]
            if tv["top"]:
                ev.append(f"top text '{tv['top'][0]}' ~ 'התקבל' sim={tv['top'][1]}")
            if tv["bottom"]:
                ev.append(f"bottom text '{tv['bottom'][0]}' sim={tv['bottom'][1]}")
            c = cand(iso, tv["bbox"], ev, None, tv["raws"][iso][0].split(":", 1)[1])
            c.update(votes=n, top_sim=0.0)
            if iso not in by_value:
                cands.append(c)
        identity = (tv["top"] and tv["top"][1] >= 0.6) or (tv["bottom"] and tv["bottom"][1] >= 0.6)
        dated = [(iso, n) for iso, n in tv["votes"].items()]
        four_digit = lambda iso: all(re.findall(r"\d+", r.split(":", 1)[1])[-1:] and
                                     len(re.findall(r"\d+", r.split(":", 1)[1])[-1]) == 4 for r in tv["raws"][iso])
        if (identity and len(dated) == 1 and dated[0][1] >= 2 and four_digit(dated[0][0])
                and 2020 <= int(dated[0][0][:4]) <= 2035 and len({c["value"] for c in cands}) == 1):
            return {"status": "proposed", "value": dated[0][0], "candidates": cands,
                    "reason": "stamp proven by its text; all renderings that read a date agree (>=2 votes)"}
    if cands:
        return {"status": "uncertain", "value": None, "candidates": cands,
                "reason": "stamp identified only by its bottom lines, or several different stamp dates"}
    return {"status": "missing", "value": None, "candidates": [],
            "reason": "no 'התקבל' stamp date read (stamp may be faint, rotated, absent or handwritten)"}


def below_central_regions(central: dict | None, shape) -> list[dict]:
    """Form 2 of the received date (FIELD_SPEC.md): handwritten BELOW the central number, on the sticker / lab
    annotation. Anchors = the proposed central numbers' locations (sticker / number)."""
    if not central or central.get("status") != "proposed":
        return []
    H, W = shape[:2]
    out = []
    for c in central["candidates"]:
        if c["value"] not in central["values"]:
            continue
        x0, y0, x1, y1 = c["bbox"]
        w, h = x1 - x0, max(12, y1 - y0)
        out.append({"bbox": [int(max(0, x0 - w)), int(y1), int(min(W, x1 + w)), int(min(H, y1 + 5 * h + 60))],
                    "evidence": f"below central number {c['value']} (received date form 2: handwritten under the "
                                f"central number)", "central": c["value"]})
    return out


def received_date(tokens, gray, ocr, rows, views: dict | None = None, central: dict | None = None) -> dict:
    """Received date, binding rule (FIELD_SPEC.md): (1) the 'התקבל' stamp, or (2) handwritten below the central
    number. A form-2 date is a candidate only when a date token lies in the region below a PROPOSED central number;
    the central number itself is never read as a date and vice versa. Competing dates -> no proposal. When nothing
    is readable, the below-central regions are kept for human review. A treatment / issue / print date elsewhere
    on the page is never used."""
    res = stamp_received_date(tokens, gray, ocr, rows, views)
    regions = below_central_regions(central, gray.shape)
    hand = []
    for reg in regions:
        for t in tokens:
            d = parse_date(t.text)
            if d and in_box(t, reg["bbox"]) and t.text.strip() != reg["central"]:
                c = cand(d, t, [reg["evidence"], f"date token '{t.text}' inside that region"], t.conf, t.text)
                ok, reads = stable(ocr, gray, t)
                c.update(top_sim=0.0, form=2, stable=ok, rereads=reads)
                hand.append(c)
    if not hand:
        # empty below-sticker regions are NOT exported: on part 1 they were almost always blank (the handwritten
        # dates sit under HANDWRITTEN central numbers, which are not located yet) -> noise for the reviewer
        return res
    res["regions"] = [r for r in regions if any(in_box(Token(-1, "", 0, h["bbox"]), r["bbox"]) for h in hand)]
    known = {c["value"] for c in res["candidates"]}
    res["candidates"] = res["candidates"] + [c for c in hand if c["value"] not in known]
    values = {c["value"] for c in res["candidates"]}
    if res["status"] == "proposed":
        if len(values) > 1:                              # stamp and form-2 dates disagree
            res.update(status="uncertain", value=None,
                       reason="stamp date and the date below the central number disagree")
        return res
    if (len(values) == 1 and all(c.get("form") == 2 for c in res["candidates"]) and all(c["stable"] for c in hand)
            and 2020 <= int(next(iter(values))[:4]) <= 2035):
        res.update(status="proposed", value=next(iter(values)),
                   reason="date below the proposed central number (form 2); single, stable, no competing date")
    else:
        res.update(status="uncertain", value=None,
                   reason="several candidate dates (stamp / below the central number) or unstable reading")
    return res


LETTERHEAD_BAND = 0.18      # top fraction of the page read again (letterhead experiment, 21.10: hit on p09)


def letterhead_sender(sender_field: dict, ocr, gray, views: dict | None) -> dict:
    """Only when no sender was proposed: ONE targeted read of the header band at the native 300 dpi with CLAHE,
    matched with the same closed-list matcher (ambiguous aliases and receiver side excluded, receiver-context rows
    skipped). Hits are ADDED as candidates; existing candidates are kept. A proposal (still PENDING human approval)
    only if this yields exactly one institution, there is no other non-receiver candidate, and the band has no
    addressee ('לכבוד'). Measured cost: ~2 s per page that needs it."""
    if sender_field["status"] == "proposed" or not views or not views.get("hires"):
        return sender_field
    t0 = time.perf_counter()
    hires = views["hires"]()
    if hires is None:
        return sender_field
    H, W = gray.shape[:2]
    k = hires.shape[1] / W
    band = hires[:int(LETTERHEAD_BAND * hires.shape[0]), :]
    band = enhance(cv2.cvtColor(band, cv2.COLOR_GRAY2BGR))
    band_rows = make_rows(ocr.read_crop(band))
    addressee = any("לכבוד" in norm_heb("".join(t.text for t in r)) for r in band_rows)
    found = {}
    for row in band_rows:
        rn = norm_heb("".join(t.text for t in row))
        if "לכבוד" in rn or any(norm_heb(c) in rn for c in RECEIVER_CUES):
            continue
        for h in match_row(row):
            if h["ambiguous"] or h["sim"] < 0.8:
                continue
            for rec in h["recs"]:
                name = display(rec)
                if name in RECEIVER_SIDE:
                    continue
                box = [int(v / k) for v in union([t.bbox for t in h["tokens"]])]   # back to the 200-dpi frame
                r = RECORDS[rec]
                found.setdefault(name, cand(name, box,
                                            [f"letterhead band, targeted 300-dpi CLAHE read: "
                                             f"'{' '.join(t.text for t in h['tokens'])}' ~ '{h['alias']}' sim={h['sim']}",
                                             f"list record {r['reg_no'] or 'name-only'} ({r['type'] or '-'}, {r['city'] or '-'})"],
                                            min(t.conf for t in h["tokens"])))
                found[name].update(kind="letterhead_read", reg_no=r["reg_no"])
    out = dict(sender_field)
    existing = {c["value"] for c in out["candidates"]}
    out["candidates"] = list(out["candidates"]) + [c for n, c in found.items() if n not in existing]
    others = {c["value"] for c in out["candidates"] if c["value"] not in RECEIVER_SIDE} - set(found)
    out["letterhead_read"] = {"institutions": sorted(found), "addressee_in_band": addressee,
                              "seconds": round(time.perf_counter() - t0, 2)}
    if len(found) == 1 and not others and not addressee:
        name = next(iter(found))
        out.update(status="proposed", value=name,
                   reason="letterhead band (targeted 300-dpi read): the only listed institution, no addressee")
    return out


# Known destination (21.10): LabExtract serves the central virology lab at Sheba. That lab is ALWAYS the receiver.
# A mention of Sheba itself is treated as the destination by default (in all 26 samples it was: address, 'לכבוד',
# 'נותן השירות', 'נשלח לתל השומר', 'אל מעבדת נגיפים', provider field); a Sheba unit is a sender candidate only
# with an explicit sender label in the same row.
DESTINATION = {display("4110T")}
SENDER_LABELS = ["מעבדה שולחת", "גורם שולח", "מוסד שולח", "גורם מפנה", "המוסד השולח", "מחלקה שולחת"]


def sender_label_in(row_norm: str) -> bool:
    return any(norm_heb(l) in row_norm for l in SENDER_LABELS)


def independent_sender_evidence(hits) -> bool:
    """Rule (4) evidence (21.10, approved narrow rule): a letterhead hit (top of page, no 'לכבוד' in the row or its
    neighbours), or two hits that share no token and lie in different rows. Overlapping hits of one text
    (e.g. 'לאומית' inside 'לאומית שירותי בריאות') count once."""
    if any(letterhead for _, _, letterhead in hits):
        return True
    for i, (ids_a, row_a, _) in enumerate(hits):
        for ids_b, row_b, _ in hits[i + 1:]:
            if row_a != row_b and not (ids_a & ids_b):
                return True
    return False


def sender(tokens, gray, rows, fam: dict | None = None) -> dict:
    """Closed list only. Match kinds: exact | fuzzy_long (alias >= 8 letters, sim >= 0.8) | fuzzy_short.
    fuzzy_short ('לאומי' of 'ביטוח לאומי' ~ 'לאומית') is kept as a review candidate but never competes or proposes.
    Proposal: (1) insurer column of an insurance table (role from layout), or (2) clear winner (repeated/header),
    or (3) the only decision-grade name on the page with sim >= 0.9 — receiver side never proposed."""
    fam = fam or {}
    H = gray.shape[0]
    score, where, best = defaultdict(float), defaultdict(list), defaultdict(float)
    hit_info = defaultdict(list)        # canon -> [(token ids, row index, letterhead?)] for the independence check
    sender_side_overrides = set()       # Sheba units with explicit sender-role evidence on this page
    addressee = [("לכבוד" in norm_heb("".join(t.text for t in r))) for r in rows]
    for ri, row in enumerate(rows):
        row_norm = norm_heb("".join(t.text for t in row))
        if "לכבוד" in row_norm or any(norm_heb(c) in row_norm for c in RECEIVER_CUES):
            continue                                   # receiver context, not the sender
        for h in match_row(row):
            s, n_alias = h["sim"], len(h["alias_norm"])
            kind = "exact" if s >= 0.999 else ("fuzzy_long" if n_alias >= 8 else "fuzzy_short")
            if h["ambiguous"]:
                kind = "ambiguous"                     # alias shared by several records (e.g. הדסה, רבין)
            weight = 2.0 if h["tokens"][0].cy < 0.2 * H else 1.0      # header / logo area counts double
            for rec in h["recs"]:
                canon = display(rec)
                if kind in ("exact", "fuzzy_long"):
                    score[canon] += weight * s
                    best[canon] = max(best[canon], s)
                r = RECORDS[rec]
                c = cand(canon, union([t.bbox for t in h["tokens"]]),
                         [f"'{' '.join(t.text for t in h['tokens'])}' ~ '{h['alias']}' sim={s:.2f} ({kind}, {h['kind']})",
                          f"list record {r['reg_no'] or 'name-only'} ({r['type'] or '-'}, {r['city'] or '-'})"],
                         min(t.conf for t in h["tokens"]))
                c.update(kind=kind, reg_no=r["reg_no"])
                if canon in RECEIVER_SIDE and canon not in DESTINATION and sender_label_in(row_norm):
                    # known destination = the central virology lab; another Sheba unit may send, but only with an
                    # explicit sender label in the same row (21.10 clarification, point 6)
                    sender_side_overrides.add(canon)
                    c["evidence"].append("explicit sender label in the row: Sheba unit allowed as sender")
                where[canon].append(c)
                if kind in ("exact", "fuzzy_long"):
                    letterhead = weight == 2.0 and not any(addressee[j] for j in (ri - 1, ri, ri + 1)
                                                           if 0 <= j < len(rows))
                    hit_info[canon].append((frozenset(t.id for t in h["tokens"]), ri, letterhead))
    cands = []
    for canon, items in sorted(where.items(), key=lambda kv: -score.get(kv[0], 0)):
        cands.append({**items[0], "score": round(score.get(canon, 0), 2), "occurrences": len(items)})
    ranked_all = sorted(score.items(), key=lambda kv: -kv[1])
    ranked = [kv for kv in ranked_all
              if kv[0] not in RECEIVER_SIDE or kv[0] in sender_side_overrides]          # destination does not compete
    # (1) insurance-table screenshot: insurer column of the first data row (role from layout; fuzzy >= 0.75 ok)
    for hi in fam.get("insurance_table", []):
        row = rows[hi + 1] if hi + 1 < len(rows) else []
        hits = {}
        for h in match_row(row, min_sim=0.75):
            if h["ambiguous"] or len(h["alias_norm"]) < 5:
                continue
            for rec in h["recs"]:
                if display(rec) not in RECEIVER_SIDE:
                    hits[display(rec)] = max(hits.get(display(rec), 0), h["sim"])
        if len(hits) == 1:
            canon, s = next(iter(hits.items()))
            competitors = [c for c, v in ranked if c != canon and v >= 2.0]
            if not competitors:
                ev = f"insurer column of the insurance table (sim={s:.2f})"
                for c in cands:
                    if c["value"] == canon:
                        c["evidence"].append(ev)
                if canon not in where:
                    cands.insert(0, cand(canon, union([t.bbox for t in row]), [ev]))
                return {"status": "proposed", "value": canon, "candidates": cands,
                        "reason": "insurer column of the insurance table (role from layout)"}
    if not cands:
        return {"status": "missing", "value": None, "candidates": [], "reason": "no closed-list sender name read"}
    if ranked:
        top, top_s = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if top_s >= 2.0 and top_s >= 2 * second:                                       # (2)
            return {"status": "proposed", "value": top, "candidates": cands,
                    "reason": "closed-list name, repeated or in the header, clearly ahead of others"}
        if len(ranked) == 1 and best[top] >= 0.9:                                      # (3)
            return {"status": "proposed", "value": top, "candidates": cands,
                    "reason": "the only decision-grade closed-list sender on the page (sim >= 0.9)"}
        if len(ranked) == 1 and best[top] >= 0.8 and independent_sender_evidence(hit_info[top]):   # (4)
            return {"status": "proposed", "value": top, "candidates": cands,
                    "reason": "the only listed candidate (sim >= 0.8) with independent evidence: letterhead "
                              "without addressee, or 2 non-overlapping occurrences in different rows"}
    return {"status": "uncertain", "value": None, "candidates": cands,
            "reason": "weak or ambiguous list match, receiver-side, or competing senders"}


# Tesseract patient-label probes (v0.4_pat): +2 name pages for ~+4.6 s/page -> OFF by default (22.10); enable with
# LABOCR_PATIENT_PROBE=on for targeted use
PATIENT_LABEL_PROBE = __import__("os").environ.get("LABOCR_PATIENT_PROBE", "off") == "on"


def add_handwriting_regions(tokens, gray, rows, fam, barcodes, views, pid, first, last, central, rdate,
                            commit=None):
    """Review / crop regions from the local handwriting locator (hwregions.py, 21.10). Regions only: no value, no
    status change. ID / names: label-anchored crops snapped to the written ink (+ the screenshot header template),
    replacing the coarser label boxes. Central number / received date: lab-annotation-shaped handwriting blocks,
    added only while the field is not proposed."""
    from . import hwregions as hw
    g0 = views.get("raw") if views and views.get("raw") is not None else gray
    if g0.ndim == 3:
        g0 = cv2.cvtColor(g0, cv2.COLOR_BGR2GRAY)
    if g0.shape[:2] != gray.shape[:2]:
        g0 = cv2.resize(g0, (gray.shape[1], gray.shape[0]))
    blocks = hw.handwriting_blocks(tokens, g0, barcodes)
    h_med = hw._median_text_h(tokens)
    # values ASSIGNED to another role: proposed values and strong candidates (not every weak reading)
    other_values = {v for f in (central, commit) if f for v in (f.get("values") or [])} | {
        c["value"] for f in (central, commit) if f for c in f["candidates"] if c.get("role", c.get("tier")) == "strong"}
    hdr = hw.screenshot_header_region(rows, blocks, gray.shape, "insurance_table" in fam)
    guard = lambda r: person_guarded(r, institutions=True)            # noqa: E731
    fields_ = (("israeli_id", pid), ("first_name", first), ("last_name", last))
    regs = {name: hw.patient_value_regions(rows, tokens, blocks, gray.shape, name, guard) + hdr for name, _ in fields_}
    if PATIENT_LABEL_PROBE and any(not r for r in regs.values()):
        # 22.10: patient labels Paddle did not read -> Tesseract heb+eng @300 dpi beside unanchored written blocks
        covered = [r["bbox"] for rs in regs.values() for r in rs]
        extra = hw.tesseract_patient_anchors(blocks, covered, gray.shape,
                                             lambda box: tesseract_text(gray, views, box, "heb+eng", 6), PERSON_GUARDS)
        if extra:
            regs = {name: hw.patient_value_regions(rows, tokens, blocks, gray.shape, name, guard, extra) + hdr
                    for name, _ in fields_}
    bands = []
    for name, f in fields_:
        rs = []
        for r in regs[name]:
            rs.append(r)
            blk = hw.grow_patient_block(r["bbox"], blocks, rows, guard, gray.shape, r.get("lh", 22))
            if blk != r["bbox"]:                         # the whole patient block (name + ID together)
                rs.append({"bbox": blk, "label": r.get("label"), "whole_block": True,
                           "evidence": "REVIEW / CROP REGION, not a value: whole patient block grown from "
                                       f"'{r.get('label')}' over the adjacent writing (never across another "
                                       "person's row)"})
        bands += [r["bbox"] for r in rs]
        for r in rs:                                     # may this crop be sent to a reader? (22.10)
            r["quality"] = hw.region_quality(r["bbox"], tokens, g0, h_med, other_values, name)
        if f["status"] != "proposed":
            f["regions"] = rs[:4]
    ann = hw.lab_annotation_candidates(blocks, bands, gray.shape, h_med, tokens)[:2]
    for a in ann:
        a["quality"] = hw.region_quality(a["bbox"], tokens, g0, h_med, other_values, "central_and_date")
    if central["status"] != "proposed":
        # handwritten central number unresolved: the annotation blocks hold it and the date under it
        for f in (central, rdate):
            if f["status"] != "proposed" and ann:
                f.setdefault("regions", [])
                f["regions"] += [{"bbox": a["bbox"], "quality": a["quality"],
                                  "evidence": "REVIEW / CROP REGION, not a value: " + a["evidence"]} for a in ann]
    elif rdate["status"] != "proposed":
        # central number found, received date missing: only annotation blocks directly UNDER a proposed central
        # number qualify (elsewhere on such pages these blocks were logos / notes / stamps)
        below = below_central_regions(central, gray.shape)
        for a in ann:
            if any(not (a["bbox"][2] < r["bbox"][0] or a["bbox"][0] > r["bbox"][2] or a["bbox"][3] < r["bbox"][1]
                        or a["bbox"][1] > r["bbox"][3]) for r in below):
                rdate.setdefault("regions", []).append(
                    {"bbox": a["bbox"], "quality": a["quality"], "under_proposed_central": True,
                     "evidence": "REVIEW / CROP REGION, not a value: handwriting under the proposed central number"})
    add_unresolved_regions(tokens, g0, h_med, fam, central, rdate, commit)


def add_unresolved_regions(tokens, g0, h_med, fam, central, rdate, commit):
    """22.10 (targeted fallback reader): boxes of fields the local engines LOCATED but did not resolve, each with a
    context for the reader's prompt and the same quality gate. Never a value, never a status change.
      commitment: each conflicting / uncertain item (its token or table-cell box)  context table_cell | label_value
      received date: the stamp region a stamp date was read from, while unresolved context stamp
      central number: an unconfirmed printed (sticker) candidate                   context sticker"""
    from . import hwregions as hw
    central_vals = set(central.get("values") or []) | {
        c["value"] for c in central["candidates"] if c.get("role", c.get("tier")) == "strong"}
    commit_vals = set((commit or {}).get("values") or []) | {
        c["value"] for c in (commit or {}).get("candidates", []) if c.get("role") == "strong"}
    if commit and commit.get("items"):
        ctx = "table_cell" if "insurance_table" in fam else "label_value"
        for it in commit["items"]:
            if it["status"] == "proposed":
                continue
            commit.setdefault("regions", []).append({
                "bbox": [int(v) for v in it["bbox"]], "context": ctx, "item_key": it["item_key"],
                "quality": hw.region_quality(it["bbox"], tokens, g0, h_med, central_vals, "commitment_number",
                                             side_ink=ctx != "table_cell"),
                "evidence": f"REVIEW / CROP REGION, not a value: commitment row {it['item_id']} ({it['status']}; "
                            f"{len(it['candidate_values'])} local reading(s))"})
    if rdate["status"] != "proposed":
        # only a region PROVEN to be the stamp (its 'התקבל' text or its lab lines were read) - a template / frame
        # hit alone matched header dates on screenshots (p11, p25)
        stamp = [c for c in rdate["candidates"] if c.get("form") != 2 and c.get("source") != "gemini"
                 and any("top text" in e or "bottom text" in e for e in c.get("evidence", []))]
        if stamp:
            from .layout import union
            box = union([c["bbox"] for c in stamp])
            rdate.setdefault("regions", []).append({
                "bbox": [int(v) for v in box], "context": "stamp",
                "quality": {"sendable": True, "digit_tokens": 0, "why": [], "written": True,
                            "other_field_value": False, "truncated": False},
                "evidence": "REVIEW / CROP REGION, not a value: the 'התקבל' stamp region the local reader used"})
    if central["status"] != "proposed":
        # a sticker role needs evidence beyond the visual barcode detector (it fires on screenshot UI elements):
        # the vertical 'מרכזי' label read at least partly, or the same number decoded from a barcode
        printed = [c for c in central["candidates"] if c.get("source") != "gemini" and c.get("bbox")
                   and (c.get("tier") in ("strong", "medium")
                        or any("barcode decoded" in e for e in c.get("evidence", [])))]
        for c in printed[:1]:
            central.setdefault("regions", []).append({
                "bbox": [int(v) for v in c["bbox"]], "context": "sticker",
                "quality": hw.region_quality(c["bbox"], tokens, g0, h_med, commit_vals, "central_number"),
                "evidence": "REVIEW / CROP REGION, not a value: unconfirmed printed central-number candidate"})


def extract(tokens: list[Token], gray, ocr, barcodes=None, views: dict | None = None) -> dict:
    rows = make_rows(tokens)
    fam = classify(rows)
    central, used_c = central_numbers(tokens, gray, ocr, rows, barcodes)
    central_values = set(central.get("values") or []) | {
        c["value"] for c in central["candidates"] if c.get("role", c.get("tier")) == "strong"}
    commit, used_m = commitment_numbers(tokens, gray, ocr, rows, exclude=used_c, fam=fam, barcodes=barcodes, views=views,
                                        exclude_values=central_values)
    other_roles = {c["value"] for f in (central, commit) for c in f["candidates"] if c.get("role", c.get("tier")) == "strong"}
    pid = israeli_id(tokens, gray, ocr, rows, exclude=used_c | used_m, exclude_values=other_roles, views=views)
    first, last = names(tokens, gray, ocr, rows, views)
    rdate = received_date(tokens, gray, ocr, rows, views, central)
    add_handwriting_regions(tokens, gray, rows, fam, barcodes, views, pid, first, last, central, rdate, commit)
    return {"first_name": first, "last_name": last, "israeli_id": pid,
            "received_date": rdate,
            "sender": letterhead_sender(sender(tokens, gray, rows, fam), ocr, gray, views),
            "central_numbers": central, "commitment_numbers": commit}
