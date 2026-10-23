"""Turn an input file (PDF / image) into one JPEG per page."""
import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps

MAX_SIDE = 2400  # keeps stamp digits and handwriting legible, stays well under API size limits


def _to_jpeg(img, quality=90):
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def pages(path, dpi=220, only=None):
    """Yield (page_number starting at 1, jpeg bytes)."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        doc = fitz.open(path)
        for i, page in enumerate(doc, 1):
            if only and i not in only:
                continue
            pix = page.get_pixmap(dpi=dpi)
            yield i, _to_jpeg(Image.open(io.BytesIO(pix.tobytes("png"))))
    else:
        yield 1, _to_jpeg(Image.open(path))


def rotate(jpeg, degrees):
    img = Image.open(io.BytesIO(jpeg))
    return _to_jpeg(img.rotate(degrees, expand=True))
