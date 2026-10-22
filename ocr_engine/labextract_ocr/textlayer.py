"""Digital PDFs: words straight from the PDF text layer (PyMuPDF), as Tokens in the engine's 200-dpi frame.

Used instead of OCR only when the page is really digital:
  - at least MIN_WORDS words in the text layer;
  - images cover < MAX_IMAGE_COVER of the page (a scan with a scanner-made text layer keeps OCR);
  - no page rotation.
Pieces of one word that PyMuPDF splits at punctuation ('ת' '.' 'ז' '.:') are merged in LOGICAL order when they
touch, so labels look like OCR tokens ('ת.ז.:'). Text-layer tokens get conf 1.0 and ids from TEXT_LAYER_ID0 - an
exact reading, so same-engine re-reads are not needed (fields.stable)."""
from __future__ import annotations

from pathlib import Path

import pymupdf

from .ocr import Token

TEXT_LAYER_ID0 = 1_000_000
MIN_WORDS = 15
MAX_IMAGE_COVER = 0.30
TOUCH_PT = 1.5


def page_tokens(path, number: int, dpi: int) -> list[Token] | None:
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return None
    with pymupdf.open(path) as doc:
        page = doc[number - 1]
        if page.rotation:
            return None
        area = page.rect.width * page.rect.height
        covered = 0.0
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                covered += abs(r & page.rect)
        if covered / area >= MAX_IMAGE_COVER:
            return None
        words = page.get_text("words")
    if len(words) < MIN_WORDS:
        return None
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)
    k = dpi / 72.0
    out = []
    for key in sorted(lines):
        group = sorted(lines[key], key=lambda w: w[7])           # logical (reading) order
        merged = [list(group[0])]
        for w in group[1:]:
            m = merged[-1]
            gap = max(w[0] - m[2], m[0] - w[2])                 # touching on either side (RTL or LTR)
            if gap <= TOUCH_PT:
                m[0], m[1], m[2], m[3] = min(m[0], w[0]), min(m[1], w[1]), max(m[2], w[2]), max(m[3], w[3])
                m[4] = m[4] + w[4]
            else:
                merged.append(list(w))
        for m in merged:
            out.append(Token(TEXT_LAYER_ID0 + len(out), m[4], 1.0,
                             [int(m[0] * k), int(m[1] * k), int(m[2] * k), int(m[3] * k)]))
    return out


def is_text_layer(token) -> bool:
    return token.id >= TEXT_LAYER_ID0
