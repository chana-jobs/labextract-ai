"""Handwriting region location (local, no reading). Generic, no page-specific coordinates, no colour cues.

handwriting_blocks: ink that the printed OCR does not explain (dark pixels outside confidently-read, normal-height
printed tokens), with table rules and decoded barcodes removed, grouped at the scale of the page's text height.
Paddle's detector also finds handwriting as TALL, low-confidence boxes; these are added as seeds.
Each block is described by size (in text-height units), number of stacked lines and ink density.

lab_annotation_candidates: blocks shaped like the lab's handwritten annotation (central number with the received
date under it): 2-3 stacked lines of handwriting larger than print, compact, not inside a patient-label row.
The role is NOT proven by the shape: a later reader (Gemini, approved separately) must read 'number + date' and the
usual rules decide. Regions are for crops / review only, never values.

patient_value_crop: tightens a label-based patient region (fields.patient_regions) to the handwriting / tokens
actually found there, so a crop holds the whole value and not the neighbouring fields."""
from __future__ import annotations

import numpy as np
import cv2

from .layout import union


def _median_text_h(tokens) -> float:
    hs = sorted(t.h for t in tokens if t.conf >= 0.85 and len(t.text.strip()) >= 2)
    return float(hs[len(hs) // 2]) if hs else 22.0


def _lines(mask) -> int:
    """Stacked text lines in a block = runs of rows with ink (horizontal projection), ignoring thin gaps."""
    prof = mask.sum(axis=1) > max(2, 0.02 * mask.shape[1])
    runs, inside, start = [], False, 0
    for y, v in enumerate(prof):
        if v and not inside:
            inside, start = True, y
        elif not v and inside:
            inside = False
            runs.append(y - start)
    if inside:
        runs.append(len(prof) - start)
    return sum(1 for r in runs if r >= 6)


def _stroke_width(mask) -> float:
    """Median stroke width (px) = 2 x distance-to-background on the stroke centre lines. Pen handwriting is thin;
    logos, stamps and bold print are thick."""
    if mask.sum() == 0:
        return 0.0
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    ridge = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (mask > 0)
    vals = dist[ridge]
    return round(float(2 * np.median(vals)), 2) if vals.size else 0.0


def handwriting_blocks(tokens, gray, barcodes=None) -> list[dict]:
    H, W = gray.shape[:2]
    h_med = _median_text_h(tokens)
    ink = (gray < 150).astype(np.uint8)
    printed = [t for t in tokens if t.h <= 1.5 * h_med]        # normal-height text = print, whatever its conf
    for t in printed:
        x0, y0, x1, y1 = (int(v) for v in t.bbox)
        ink[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3] = 0
    for b in barcodes or []:
        x0, y0, x1, y1 = (int(v) for v in b["bbox"])
        ink[max(0, y0 - 25):y1 + 45, max(0, x0 - 10):x1 + 10] = 0          # bars + printed digits under them
    L = int(3 * h_med)
    rules = (cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1))) |
             cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, L))))
    ink = ink & ~rules
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    seeds = np.zeros_like(ink)
    for t in tokens:                                      # tall, low-confidence detections = handwriting seeds
        if t.h >= 1.6 * h_med and t.conf < 0.9:
            x0, y0, x1, y1 = (int(v) for v in t.bbox)
            seeds[y0:y1, x0:x1] = 1
    grp = cv2.dilate(ink | (seeds & (gray < 170).astype(np.uint8)),
                     cv2.getStructuringElement(cv2.MORPH_RECT, (int(1.6 * h_med), int(1.4 * h_med))))
    n, lab, st, _ = cv2.connectedComponentsWithStats(grp)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = st[i]
        sub = ink[y:y + h, x:x + w]
        px = int(sub.sum())
        if w < 2.5 * h_med or h < 1.2 * h_med or w > 0.45 * W or h > 0.3 * H or px < 25 * h_med:
            continue
        out.append({"bbox": [int(x), int(y), int(x + w), int(y + h)], "w_u": round(w / h_med, 1),
                    "h_u": round(h / h_med, 1), "lines": _lines(sub), "density": round(px / float(w * h), 3),
                    "stroke": _stroke_width(sub)})
    return out


PRINTED_MAX = 0.09    # share of the block area covered by normal-height OCR text (targets <= 0.05)
BAND = (0.10, 0.40)   # ranking prior on y0 / page height
STROKE_MAX = 4.0      # px at 200 dpi; pen strokes ~2-4, bold print / logos / stamp letters thicker


def lab_annotation_candidates(blocks, patient_rows: list[list[int]], shape, h_med: float = 22.0,
                              tokens=()) -> list[dict]:
    """Blocks shaped like 'central number over date': 5.5-13 text heights tall, 6-17 wide, sparse thin pen strokes
    (density 0.03-0.09, stroke <= 4 px), not overlapping a patient-label row band. Line counting is not used (it
    fails on rotated handwriting)."""
    H, W = shape[:2]
    out = []
    for b in blocks:
        x0, y0, x1, y1 = b["bbox"]
        if not (5.5 <= b["h_u"] <= 13 and 6 <= b["w_u"] <= 17 and 0.03 <= b["density"] <= 0.09
                and b["stroke"] <= STROKE_MAX):
            continue
        if any(not (y1 < r[1] or y0 > r[3]) and not (x1 < r[0] or x0 > r[2]) for r in patient_rows):
            continue
        # printed text crossing the block (stamps, logos, a signature with a printed name): not an annotation
        pr_area = sum((t.bbox[2] - t.bbox[0]) * t.h for t in tokens
                      if x0 <= t.cx <= x1 and y0 <= t.cy <= y1 and t.h <= 1.5 * h_med)
        if pr_area > PRINTED_MAX * (x1 - x0) * (y1 - y0):
            continue
        b = {**b, "band": bool(BAND[0] <= y0 / H <= BAND[1])}
        out.append({**b, "evidence": f"handwriting block, {b['lines']} stacked lines, {b['h_u']}x{b['w_u']} text "
                                     f"heights - shaped like a lab annotation (central number + date); role unproven"})
    # ranking prior (layout assumption, NOT a filter): lab annotations sat in the upper free space on part 1
    out.sort(key=lambda b: (not b["band"], b["bbox"][1]))
    return out


ID_LABELS = ["תעודת זהות", "מספר זהות", "מס זהות", "מס זהוי", "תז", "מז", "id", "idpass", "pass", "תעודת זהות:"]
NAME_LABELS = {"first_name": ["שם פרטי", "שם הנבדק", "שם המבוטח", "שם החולה", "שם המטופל", "אצל החולה", "שם"],
               "last_name": ["שם משפחה", "שם הנבדק", "שם המבוטח", "שם החולה", "שם המטופל", "אצל החולה", "שם"]}


def _label_hits(rows, labels, exact_short=True):
    from .textmatch import best_window, norm_heb
    out = []
    for row in rows:
        texts = [t.text for t in row]
        for lab in labels:
            n = norm_heb(lab)
            if len(n) <= 4:                                # short labels: exact token match only (ת.ז, שם, ID)
                hit = [t for t in row if norm_heb(t.text).rstrip(":") == n or norm_heb(t.text) == n]
                if hit:
                    out.append((1.0, [hit[0]], lab, row)); break
            else:
                s, i, j = best_window(texts, lab)
                if s >= 0.75:
                    out.append((s, row[i:j], lab, row)); break
    return out


NON_PERSON = ["מבטח", "מוסד", "המוסד", "מחלקה", "ספק", "קופה", "בית", "בדיקה", "הבדיקה", "השירות", "מעבדה"]


def _names_a_non_person(row, ltoks) -> bool:
    """A bare 'שם' followed (RTL: to its left) by a non-person noun is an institution / test name label."""
    from .textmatch import norm_heb
    lx0, lh = min(t.bbox[0] for t in ltoks), max(t.h for t in ltoks)
    nxt = [t for t in row if t not in ltoks and 0 <= lx0 - t.bbox[2] <= 1.5 * lh]
    return any(norm_heb(t.text).strip(":") in {norm_heb(w) for w in NON_PERSON} for t in nxt)


def _extend_label(row, ltoks, labels, latin):
    """Absorb the label's continuation in reading direction: tokens that are themselves patient-label words or
    separators ('/', ':'), within ~1.5 label heights. Generic for compound labels such as 'ID / PASS:'."""
    from .textmatch import norm_heb
    short = {norm_heb(l) for l in labels if len(norm_heb(l)) <= 6}
    lh = max(t.h for t in ltoks)
    changed = True
    while changed:
        changed = False
        lx0, lx1 = min(t.bbox[0] for t in ltoks), max(t.bbox[2] for t in ltoks)
        for t in row:
            if t in ltoks:
                continue
            gap = (t.bbox[0] - lx1) if latin else (lx0 - t.bbox[2])
            n = norm_heb(t.text).strip(":/.")
            if -2 <= gap <= 1.5 * lh and (n in short or t.text.strip() in {"/", ":", "/:"}):
                ltoks.append(t)
                changed = True
    return ltoks


def patient_value_regions(rows, tokens, blocks, shape, field: str, person_guarded, extra_hits=()) -> list[dict]:
    """Label-anchored, ink-snapped value crops for the PATIENT's ID / names.
    - Hebrew label: the value lies LEFT of it (RTL), up to the next printed word that is itself a label-like word.
    - Latin label ('ID / PASS:'): the value lies RIGHT of it.
    - Table header with nothing beside it: the value lies BELOW it.
    The crop is the union of handwriting blocks / tokens inside that span and the label's row band (+-1 line).
    Rows of other persons (physician, father, issuer, print footer) are skipped by `person_guarded`."""
    from .textmatch import letters_ratio
    H, W = shape[:2]
    labels = ID_LABELS if field == "israeli_id" else NAME_LABELS[field]
    out = []
    hits = _label_hits(rows, labels) + [h for h in extra_hits if h[2] in labels]
    from .textmatch import norm_heb
    for s, ltoks, lab, row in hits:
        if person_guarded(row):
            continue
        if norm_heb(lab) == "שמ" and _names_a_non_person(row, ltoks):
            continue                                       # 'שם מבטח' / 'שם המוסד' ...: not the patient
        if any(norm_heb(t.text).strip(":") in {norm_heb(w) for w in NON_PERSON} for t in ltoks):
            continue                                       # fuzzy 'שם המבוטח' matched 'שם מבטח' (the insurer)
        latin = all(t.text.isascii() for t in ltoks)
        ltoks = _extend_label(row, list(ltoks), labels, latin)   # 'ID' + '/' + 'PASS:' read as separate tokens
        lb = union([t.bbox for t in ltoks])
        lh = max(14, lb[3] - lb[1])
        band = (lb[1] - int(1.2 * lh), lb[3] + int(1.2 * lh))
        words = [t for t in row if t not in ltoks and t.h <= 1.5 * lh and letters_ratio(t.text) >= 0.6 and t.conf >= 0.8]
        if latin:
            nxt = [t.bbox[0] for t in words if t.bbox[0] > lb[2]]
            span = (lb[2], min(nxt + [lb[2] + int(0.4 * W)]))
        else:
            nxt = [t.bbox[2] for t in words if t.bbox[2] < lb[0]]
            span = (max(nxt + [lb[0] - int(0.4 * W)]), lb[0])
        def inside(b):
            cy = (b[1] + b[3]) / 2
            overlap = min(b[2], span[1] + 5) - max(b[0], span[0] - 5)
            return overlap > 0.3 * (b[2] - b[0]) and band[0] <= cy <= band[1]
        # nearest written block on the label's side (handwriting) - or digits printed there (a printed ID)
        cands = [b["bbox"] for b in blocks if inside(b["bbox"])]
        cands += [t.bbox for t in tokens if t not in ltoks and inside(t.bbox) and t not in words
                  and (sum(ch.isdigit() for ch in t.text) >= 4 or t.h > 1.5 * lh)]
        gap = (lambda b: b[0] - lb[2]) if latin else (lambda b: lb[0] - b[2])
        cands.sort(key=lambda b: abs(gap(b)))
        hits = []
        for b in cands:                                    # take the nearest and blocks continuing it on the line
            if not hits or abs(gap(b) - max(gap(h) for h in hits) - (hits[-1][2] - hits[-1][0])) < 3 * lh:
                hits.append(b)
        where = "right of the Latin label" if latin else "left of the label"
        if not hits:                                       # table header: look below, same column
            col = (lb[0] - (lb[2] - lb[0]), lb[2] + (lb[2] - lb[0]))
            below = [b["bbox"] for b in blocks if col[0] <= (b["bbox"][0] + b["bbox"][2]) / 2 <= col[1]
                     and lb[3] < b["bbox"][1] < lb[3] + 4 * lh]
            hits, where = below, "below the label (table header)"
        if not hits:
            continue
        u = union(hits)
        u = [max(0, u[0] - 8), max(0, u[1] - 6), min(W, u[2] + 8), min(H, u[3] + 6)]
        # never reach into another person's row (father / physician / issuer / print footer): cut at that row
        for r in rows:
            if r is row or not person_guarded(r):
                continue
            ry0, ry1 = min(t.bbox[1] for t in r), max(t.bbox[3] for t in r)
            rx0, rx1 = min(t.bbox[0] for t in r), max(t.bbox[2] for t in r)
            if ry1 < u[1] or ry0 > u[3] or rx1 < u[0] or rx0 > u[2]:
                continue
            if ry0 >= lb[1]:
                u[3] = min(u[3], ry0 - 1)
            else:
                u[1] = max(u[1], ry1 + 1)
        if u[3] - u[1] < 0.6 * lh:
            continue
        src = "Tesseract-heb re-read" if ltoks[0].id <= -300 else "OCR"
        out.append({"bbox": u, "label": lab, "label_bbox": lb, "label_sim": round(s, 2), "lh": lh,
                    "evidence": f"REVIEW / CROP REGION, not a value: patient label '{lab}' ({src}) sim={s:.2f}; "
                                f"written value {where}, snapped to the ink found there"})
    return out


TRUNC_INK = 5.0   # written ink (in text heights) within 3 text heights beyond an edge = truncated; dev: good <= 4.2,
                  # truncated >= 6.5 (narrow margin, conservative: an error only sends the crop to human review)


def region_quality(bbox, tokens, gray, h_med: float, other_values=frozenset(), kind: str = "israeli_id",
                   side_ink: bool = True) -> dict:
    """Is a crop worth sending to a reader? Generic checks, no page knowledge:
    - written content: handwriting ink (dark pixels outside normal-height printed tokens) or a >= 4-digit token;
    - not another field's value: a digit token inside equals a value already assigned to another role
      (commitment / central number);
    - not truncated: written ink continues just outside the left / right edge (a strip of one text height)."""
    import re
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in bbox)
    inside = [t for t in tokens if x0 <= t.cx <= x1 and y0 <= t.cy <= y1]
    digit_vals = [re.sub(r"\D", "", t.text) for t in inside if sum(ch.isdigit() for ch in t.text) >= 4]

    def hw_ink(a0, a1):
        if a1 - a0 < 3:
            return 0
        m = (gray[y0:y1, a0:a1] < 150).astype("uint8")
        for t in tokens:                                  # mask normal-height printed WORDS (digits stay: a
            if t.h <= 1.5 * h_med and sum(ch.isdigit() for ch in t.text) < 2:   # handwritten value read as a token)
                tx0, ty0, tx1, ty1 = (int(v) for v in t.bbox)
                m[max(0, ty0 - y0):max(0, ty1 - y0), max(0, tx0 - a0):max(0, tx1 - a0)] = 0
        if m.size:
            m[m.mean(axis=1) > 0.5] = 0                   # ruling lines crossing the strip are not writing
        return int(m.sum())
    ink = hw_ink(x0, x1)
    strip = int(3 * h_med)
    left, right = hw_ink(max(0, x0 - strip), x0), hw_ink(x1, min(W, x1 + strip))
    why = []
    written = ink >= 15 * h_med or bool(digit_vals)
    if not written:
        why.append("no handwriting ink and no digits inside")
    other = any(v in other_values for v in digit_vals)
    if other:
        why.append("contains a value assigned to another field")
    # a digit-bearing token cut by any crop edge = the value (or another number) is truncated
    straddle = [t for t in tokens if sum(ch.isdigit() for ch in t.text) >= 2
                and t.bbox[2] > x0 and t.bbox[0] < x1 and t.bbox[3] > y0 and t.bbox[1] < y1
                and not (t.bbox[0] >= x0 - 2 and t.bbox[2] <= x1 + 2 and t.bbox[1] >= y0 - 2 and t.bbox[3] <= y1 + 2)]
    # side ink: off for a table cell (the neighbouring columns hold other numbers, not a continuation)
    truncated = (side_ink and max(left, right) >= TRUNC_INK * h_med) or bool(straddle)
    if truncated:
        why.append("writing continues beyond the crop edge (possibly truncated)")
    return {"written": written, "digit_tokens": len(digit_vals), "other_field_value": other,
            "truncated": truncated, "sendable": written and not other and not truncated, "why": why}


MAX_LABEL_PROBES = 8


def tesseract_patient_anchors(blocks, covered, shape, read, guard_words, max_probes: int = MAX_LABEL_PROBES):
    """Patient labels Paddle did not read: for written blocks that no located patient region covers, read the
    printed text where a label would stand (RTL: right of the block; Latin 'ID / PASS': left of it) with Tesseract
    heb+eng at 300 dpi. A line matching a patient label becomes a synthetic label hit next to that block.
    Lines naming another person (guard words: father, physician, issuer, print footer...) are skipped.
    Returns [(sim, [Token], label, row)] in the format of _label_hits."""
    from .ocr import Token
    from .textmatch import best_window, norm_heb
    H, W = shape[:2]
    labels = sorted(set(ID_LABELS + NAME_LABELS["first_name"] + NAME_LABELS["last_name"]), key=len, reverse=True)
    free = [b for b in blocks if 1.2 <= b["h_u"] <= 6 and 3 <= b["w_u"] <= 25
            and not any(not (b["bbox"][2] < c[0] or b["bbox"][0] > c[2] or b["bbox"][3] < c[1] or b["bbox"][1] > c[3])
                        for c in covered)]
    free.sort(key=lambda b: b["bbox"][1])
    out = []
    for k, b in enumerate(free[:max_probes]):
        x0, y0, x1, y1 = b["bbox"]
        for side, box in (("right", [x1, y0, min(W, x1 + int(0.22 * W)), y1]),
                          ("left", [max(0, x0 - int(0.16 * W)), y0, x0, y1])):
            if box[2] - box[0] < 20:
                continue
            text = read(box)
            for line in (l for l in text.splitlines() if l.strip()):
                words = line.split()
                if any(norm_heb(w).startswith(norm_heb(g)) for w in words for g in guard_words if norm_heb(g)):
                    continue
                best = None
                for lab in labels:
                    n = norm_heb(lab)
                    if len(n) <= 4:
                        s = 1.0 if any(norm_heb(w).strip(":./") == n for w in words) else 0.0
                    else:
                        s = best_window(words, lab)[0]
                    if s >= 0.8 and (best is None or s > best[0]):
                        best = (s, lab)
                if best and ((side == "left") == all(ch.isascii() for ch in best[1])):
                    lb = [x1 + 2, y0, min(W, x1 + 60), y1] if side == "right" else [max(0, x0 - 60), y0, x0 - 2, y1]
                    tok = Token(-300 - 2 * k - (side == "left"), best[1], 0.99, lb)
                    out.append((best[0], [tok], best[1], [tok]))
                    break
    return out


def grow_patient_block(crop, blocks, rows, person_guarded, shape, lh: float):
    """The whole patient block: the crop plus written blocks touching it (within ~1.5 lines), repeatedly; never
    across a row of another person. For a reader that separates name / ID itself."""
    H, W = shape[:2]
    u = list(crop)
    guarded = [(min(t.bbox[1] for t in r), max(t.bbox[3] for t in r)) for r in rows if person_guarded(r)]
    gap = 1.5 * lh
    changed = True
    while changed:
        changed = False
        for b in blocks:
            bx = b["bbox"]
            if bx[0] >= u[0] and bx[1] >= u[1] and bx[2] <= u[2] and bx[3] <= u[3]:
                continue
            near = not (bx[2] < u[0] - gap or bx[0] > u[2] + gap or bx[3] < u[1] - gap or bx[1] > u[3] + gap)
            if not near or b["h_u"] > 8 or b["w_u"] > 30:
                continue
            nu = [min(u[0], bx[0]), min(u[1], bx[1]), max(u[2], bx[2]), max(u[3], bx[3])]
            if any(not (g1 < nu[1] or g0 > nu[3]) and (g1 < u[1] or g0 > u[3]) for g0, g1 in guarded):
                continue                                   # growing would reach another person's row
            if (nu[2] - nu[0]) > 0.5 * W or (nu[3] - nu[1]) > 0.18 * H:
                continue
            u, changed = nu, True
    return [max(0, u[0]), max(0, u[1]), min(W, u[2]), min(H, u[3])]


def screenshot_header_region(rows, blocks, shape, is_screenshot: bool) -> list[dict]:
    """Template rule for HMO-system screenshots (family insurance_table): the patient line is the top header row
    holding the birth date; the patient's name / ID are written beside it (RTL: to the right of the date)."""
    from .textmatch import parse_date
    if not is_screenshot:
        return []
    H, W = shape[:2]
    for row in rows:
        if row[0].cy > 0.15 * H:
            break
        dates = [t for t in row if parse_date(t.text) and len(t.text) >= 10]
        if not dates:
            continue
        d = max(dates, key=lambda t: t.cx)
        lh = max(14, d.h)
        hits = [b["bbox"] for b in blocks if b["bbox"][0] >= d.bbox[0] and abs((b["bbox"][1] + b["bbox"][3]) / 2 - d.cy) <= 1.6 * lh]
        if hits:
            u = union(hits)
            return [{"bbox": [max(0, u[0] - 8), max(0, u[1] - 6), min(W, u[2] + 8), min(H, u[3] + 6)],
                     "label": "screenshot header", "label_bbox": d.bbox, "label_sim": 1.0,
                     "evidence": "REVIEW / CROP REGION, not a value: HMO screenshot template - patient header row "
                                 "(birth date), written name / ID beside it"}]
    return []


def patient_value_crop(region_bbox, label_bbox, tokens, blocks, shape) -> list[int] | None:
    """Tighten a patient region to what is actually written there: union of handwriting blocks and non-label
    tokens intersecting the region's row band, plus a margin. None when nothing is there."""
    H, W = shape[:2]
    rx0, ry0, rx1, ry1 = region_bbox
    hits = [b["bbox"] for b in blocks if not (b["bbox"][2] < rx0 or b["bbox"][0] > rx1 or b["bbox"][3] < ry0
                                              or b["bbox"][1] > ry1)]
    hits += [t.bbox for t in tokens if rx0 <= t.cx <= rx1 and ry0 <= t.cy <= ry1
             and not (label_bbox[0] <= t.cx <= label_bbox[2] and label_bbox[1] <= t.cy <= label_bbox[3])]
    if not hits:
        return None
    u = union(hits)
    u = [max(rx0, u[0]), max(ry0 - 10, u[1]), min(rx1, u[2]), min(ry1 + 10, u[3])]
    m = 8
    return [max(0, u[0] - m), max(0, u[1] - m), min(W, u[2] + m), min(H, u[3] + m)]
