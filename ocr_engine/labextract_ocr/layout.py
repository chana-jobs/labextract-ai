"""Geometry over OCR tokens: visual rows, neighbours in RTL order, regions."""
from __future__ import annotations

from .ocr import Token


def rows(tokens: list[Token], tol: float = 0.6) -> list[list[Token]]:
    """Group tokens into visual rows by vertical centre; each row sorted right-to-left (Hebrew reading order)."""
    out, band = [], []
    for t in sorted(tokens, key=lambda t: t.cy):
        if band and abs(t.cy - band[-1].cy) > tol * max(t.h, band[-1].h, 10):
            out.append(sorted(band, key=lambda x: -x.cx)); band = []
        band.append(t)
    if band:
        out.append(sorted(band, key=lambda x: -x.cx))
    return out


def row_text_ltr(row: list[Token]) -> str:
    """Row text left-to-right without spaces — for digit/date runs split across tokens."""
    return "".join(t.text for t in sorted(row, key=lambda x: x.cx))


def in_box(t: Token, box) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= t.cx <= x1 and y0 <= t.cy <= y1


def union(boxes) -> list[int]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def h_overlap(a, b) -> float:
    """Horizontal overlap as a fraction of the narrower box."""
    inter = min(a[2], b[2]) - max(a[0], b[0])
    return max(0.0, inter) / max(1, min(a[2] - a[0], b[2] - b[0]))


def v_overlap(a, b) -> float:
    inter = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, inter) / max(1, min(a[3] - a[1], b[3] - b[1]))
