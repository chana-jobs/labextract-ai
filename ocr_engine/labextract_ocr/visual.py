"""Visual evidence that OCR text alone misses.

1. Barcodes (gradient + morphology) — the central-number sticker is barcode + number + a VERTICAL "מרכזי".
2. Vertical-label probe — crop the strips beside a sticker, rotate +-90 degrees, run the Paddle recognizer, and
   fuzzy-match "מרכזי". Paddle's detector is tuned for horizontal text, so this word never appears as a token.
"""
from __future__ import annotations

import cv2
import numpy as np

from .textmatch import similarity

CENTRAL_LABEL = "מרכזי"


def find_barcodes(gray: np.ndarray, min_w: int = 60, min_h: int = 25) -> list[list[int]]:
    """Boxes [x0,y0,x1,y1] of 1-D barcodes with vertical bars (horizontal and vertical stickers both)."""
    boxes = []
    for vertical_bars in (True, False):
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=-1)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=-1)
        grad = cv2.convertScaleAbs(cv2.subtract(np.abs(gx), np.abs(gy)) if vertical_bars
                                   else cv2.subtract(np.abs(gy), np.abs(gx)))
        grad = cv2.blur(grad, (9, 9))
        _, th = cv2.threshold(grad, 150, 255, cv2.THRESH_BINARY)
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7) if vertical_bars else (7, 21))
        closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
        closed = cv2.dilate(cv2.erode(closed, None, iterations=4), None, iterations=4)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            long_side, short_side = (w, h) if vertical_bars else (h, w)
            if long_side >= min_w and short_side >= min_h and long_side / max(1, short_side) <= 8:
                fill = cv2.contourArea(c) / max(1, w * h)
                if fill > 0.6:
                    boxes.append([x, y, x + w, y + h, "h" if vertical_bars else "v"])
    return boxes


STAMP_TOP = "התקבל"
STAMP_BOTTOM = ["לנגיפים", "המעבדה המרכזית", "המשולב", "המרכז הרפואי"]


def find_stamp_frames(gray: np.ndarray, dpi: int = 200) -> list[dict]:
    """Rounded-rectangle frames of 'received' stamp size (~2.2 x 1.45 in at the sample scale; wide tolerance)."""
    scale = dpi / 200
    thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    thr = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    contours, _ = cv2.findContours(thr, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    frames = []
    for c in contours:
        (cx, cy), (w, h), angle = cv2.minAreaRect(c)
        if w < h:
            w, h, angle = h, w, angle + 90
        if angle > 45:
            angle -= 90
        if not (300 * scale <= w <= 650 * scale and 180 * scale <= h <= 420 * scale and 1.2 <= w / h <= 2.3):
            continue
        if abs(angle) > 25:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        frames.append({"bbox": [x, y, x + bw, y + bh], "angle": round(float(angle), 1),
                       "size": [round(w), round(h)]})
    frames.sort(key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
    kept = []                                          # drop near-duplicates (inner/outer edge of the same frame)
    for f in frames:
        if not any(_iou(f["bbox"], k["bbox"]) > 0.5 for k in kept):
            kept.append(f)
    return kept


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / max(1, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def read_stamp(ocr, gray: np.ndarray, frame: dict) -> dict:
    """OCR inside a stamp frame, deskewed by the frame angle and a few small extra angles.
    Returns the identity evidence (top/bottom stamp texts) and every date read in the middle band."""
    from .textmatch import parse_date, similarity as sim
    from .layout import rows as make_rows, row_text_ltr
    x0, y0, x1, y1 = frame["bbox"]
    pad = 10
    crop = gray[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    h, w = crop.shape[:2]
    result = {"frame": frame, "top": None, "bottom": None, "dates": []}
    for extra, scale in ((0, 1.0), (-3, 1.0), (3, 1.0), (0, 1.6)):
        angle = frame["angle"] + extra
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        rot = cv2.warpAffine(crop, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
        if scale != 1.0:
            rot = cv2.resize(rot, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        rh = rot.shape[0]
        tokens = ocr.read_crop(rot)
        for t in tokens:
            rel = t.cy / rh
            s_top = sim(t.text, STAMP_TOP)
            if rel < 0.45 and s_top >= 0.5 and (not result["top"] or s_top > result["top"][1]):
                result["top"] = (t.text, round(s_top, 2))
            s_bot = max(sim(t.text, b) for b in STAMP_BOTTOM)
            if rel > 0.55 and s_bot >= 0.6 and (not result["bottom"] or s_bot > result["bottom"][1]):
                result["bottom"] = (t.text, round(s_bot, 2))
        for row in make_rows([t for t in tokens if 0.2 < t.cy / rh < 0.8]):
            iso = parse_date(row_text_ltr(row))
            if iso:
                result["dates"].append({"value": iso, "raw": row_text_ltr(row), "angle": angle, "scale": scale,
                                        "conf": min(t.conf for t in row)})
        if result["dates"] and (result["top"] or result["bottom"]):
            break                                       # identity + date found: no more angles needed
    return result


_TEMPLATE = None


def match_stamp_template(gray, scales=(0.9, 1.0, 1.1), angles=(-4, 0, 4), ds=0.5) -> dict | None:
    """Best match of the lab's own 'התקבל' stamp design (assets/stamp_template.png: dev-page stamp, date band
    blanked). Measured: top-1 is a real stamp on 6/26 pages, but non-stamps score as high -> the score is NOT
    identity evidence; the region still has to prove itself by its text."""
    global _TEMPLATE
    if _TEMPLATE is None:
        from pathlib import Path
        p = Path(__file__).resolve().parent / "assets" / "stamp_template.png"
        _TEMPLATE = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_GRAYSCALE) if p.exists() else False
    if _TEMPLATE is False:
        return None
    small = cv2.resize(gray, None, fx=ds, fy=ds, interpolation=cv2.INTER_AREA)
    best = None
    for s in scales:
        t = cv2.resize(_TEMPLATE, None, fx=s * ds, fy=s * ds, interpolation=cv2.INTER_AREA)
        th, tw = t.shape
        if th >= small.shape[0] or tw >= small.shape[1]:
            continue
        for a in angles:
            M = cv2.getRotationMatrix2D((tw / 2, th / 2), a, 1.0)
            tr = cv2.warpAffine(t, M, (tw, th), borderValue=int(t.mean()))
            _, mx, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(small, tr, cv2.TM_CCOEFF_NORMED))
            if best is None or mx > best["score"]:
                h = (th / ds)
                best = {"score": round(float(mx), 3), "angle": float(-a),
                        "bbox": [int(x / ds) - 20, int(y / ds - 0.35 * h), int((x + tw) / ds) + 20,
                                 int((y + th) / ds + 0.15 * h)]}
    return best


def read_stamp_votes(ocr, variants: list[tuple[str, np.ndarray]], angle: float) -> dict:
    """Read one stamp region in several renderings; every rendering is one vote per date.
    Returns identity evidence (top word / bottom lines, best over variants) and date votes."""
    from collections import Counter
    from .textmatch import parse_date, similarity as sim
    from .layout import rows as make_rows, row_text_ltr
    votes, raws, top, bottom = Counter(), {}, None, None
    for name, img in variants:
        h, w = img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
        toks = ocr.read_crop(img)
        for t in toks:
            rel = t.cy / h
            s_top = sim(t.text, STAMP_TOP)
            if rel < 0.5 and s_top >= 0.5 and (not top or s_top > top[1]):
                top = (t.text, round(s_top, 2))
            s_bot = max(sim(t.text, b) for b in STAMP_BOTTOM)
            if rel > 0.5 and s_bot >= 0.6 and (not bottom or s_bot > bottom[1]):
                bottom = (t.text, round(s_bot, 2))
        seen = set()
        for row in make_rows(toks):
            raw = row_text_ltr(row)
            iso = parse_date(raw)
            if iso and iso not in seen:
                seen.add(iso)
                votes[iso] += 1
                raws.setdefault(iso, []).append(f"{name}:{raw}")
    return {"votes": dict(votes), "raws": raws, "top": top, "bottom": bottom, "n_variants": len(variants)}


def stamp_regions(tokens, gray, dpi: int = 200) -> list[dict]:
    """Where a 'received' stamp may be: from a 'התקבל'-like token (stamp below it), from the stamp's fixed
    bottom lines (stamp above them), and from frame outlines. Each region still has to prove it is a stamp."""
    from .textmatch import similarity as sim, norm_heb
    s = dpi / 200
    W, H = gray.shape[1], gray.shape[0]
    half_w, height = int(260 * s), int(330 * s)
    regions = []
    for t in tokens:
        if sim(t.text, STAMP_TOP) >= 0.5 and len(norm_heb(t.text)) >= 3 and t.h >= 40 * s:   # stamp word is large
            regions.append({"bbox": [int(t.cx - half_w), int(t.bbox[1] - 30 * s), int(t.cx + half_w),
                                     int(t.bbox[1] + height)], "angle": 0.0,
                            "source": f"top-text '{t.text}' sim={sim(t.text, STAMP_TOP):.2f}"})
    for t in tokens:
        best = max(sim(t.text, b) for b in STAMP_BOTTOM)
        if best >= 0.6:
            regions.append({"bbox": [int(t.cx - half_w), int(t.bbox[3] - height), int(t.cx + half_w),
                                     int(t.bbox[3] + 30 * s)], "angle": 0.0,
                            "source": f"bottom-text '{t.text}' sim={best:.2f}"})
    for f in find_stamp_frames(gray, dpi):
        regions.append({**f, "source": "frame outline"})
    out = []
    for r in regions:
        b = r["bbox"]
        r["bbox"] = [max(0, b[0]), max(0, b[1]), min(W, b[2]), min(H, b[3])]
        if not any(_iou(r["bbox"], k["bbox"]) > 0.4 for k in out):
            out.append(r)
    return out


def probe_vertical_label(ocr, gray: np.ndarray, sticker_box, label: str = CENTRAL_LABEL) -> dict | None:
    """Look for a vertical `label` in the strips right and left of a sticker. Returns best hit or None."""
    H, W = gray.shape[:2]
    x0, y0, x1, y1 = sticker_box
    sh = y1 - y0
    strip_w = max(30, int(0.55 * sh))
    best = None
    for side, sx0, sx1 in (("right", x1, x1 + strip_w), ("left", x0 - strip_w, x0)):
        sx0, sx1 = max(0, sx0), min(W, sx1)
        if sx1 - sx0 < 10:
            continue
        crop = gray[max(0, y0 - 5):min(H, y1 + 5), sx0:sx1]
        for rot in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
            text, conf = ocr.recognize(cv2.rotate(crop, rot))
            s = similarity(text, label)
            if best is None or s > best["similarity"]:
                best = {"side": side, "text": text, "conf": round(conf, 3), "similarity": round(s, 2),
                        "bbox": [sx0, max(0, y0 - 5), sx1, min(H, y1 + 5)]}
    return best
