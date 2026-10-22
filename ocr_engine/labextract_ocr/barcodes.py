"""1-D barcode decoding (zxing-cpp) — an INDEPENDENT reader of digits. Only some symbologies carry a MANDATORY
check character (MANDATORY_CHECK); Code 39 / ITF / Codabar do not, so their decode is not a verified reading alone.

A successful decode verifies the digits of that barcode; it says nothing about the ROLE of the number (page
barcodes, tube barcodes and commitment barcodes exist too). Role still comes from labels / stickers / families.
Measured on the 26 part-1 pages (tools/probe_barcodes.py): 22/31 central numbers decoded, ~0.46 s/page.
"""
from __future__ import annotations

import zxingcpp

from .layout import union

# symbologies whose decode includes a mandatory check character (zxing-cpp format names)
MANDATORY_CHECK = {"Code 128", "EAN-13", "EAN-8", "UPC-A", "UPC-E", "Code 93", "DataBar", "DataBarExpanded"}


def decode(images) -> list[dict]:
    """Decode on several renderings of the SAME oriented page (e.g. raw gray + enhanced); dedupe by text+place."""
    out = []
    for img in images:
        for r in zxingcpp.read_barcodes(img, try_rotate=True, try_downscale=True):
            text = r.text.strip()
            if not text:
                continue
            p = r.position
            xs = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
            ys = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
            box = [min(xs), min(ys), max(xs), max(ys)]
            fmt = str(r.format).split(".")[-1]
            dup = next((b for b in out if b["text"] == text and _near(b["bbox"], box)), None)
            if dup:
                dup["bbox"] = union([dup["bbox"], box])
            else:
                out.append({"text": text, "format": fmt, "bbox": box})
    return out


def _near(a, b, tol=40) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def numeric(barcodes) -> list[dict]:
    return [b for b in barcodes if b["text"].isdigit()]
