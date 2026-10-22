"""Load a PDF or image into page arrays (BGR). Unicode-safe on Windows (Hebrew paths)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pymupdf

from .config import RENDER_DPI

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
TARGET_WIDTH = 1650


def imread(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(path.suffix, img)[1].tofile(str(path))


def render_gray(path: Path, page: int, dpi: int) -> np.ndarray | None:
    """One PDF page as gray at the given dpi (e.g. 300 = the samples' native scan resolution); None for images."""
    path = Path(path)
    if path.suffix.lower() in IMAGE_EXT:
        return None
    with pymupdf.open(path) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2GRAY if pix.n == 4 else cv2.COLOR_RGB2GRAY)


def load_pages(path: Path, pages: list[int] | None = None) -> list[tuple[int, np.ndarray, dict]]:
    """[(page_number_1based, bgr, meta)]; meta = {dpi, pdf_size_pt or None}."""
    path = Path(path)
    if path.suffix.lower() in IMAGE_EXT:
        img = imread(path)
        # v0.1.1 (after the holdout freeze, images only): size thresholds are calibrated for an A4 page at
        # 200 dpi (~1650 px wide); smaller images are upscaled to that width. Never downscaled.
        scale = TARGET_WIDTH / img.shape[1] if img.shape[1] < TARGET_WIDTH * 0.9 else 1.0
        if scale != 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        return [(1, img, {"dpi": None, "pdf_size_pt": None, "image_upscale": round(scale, 3)})]
    out = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            if pages and i not in pages:
                continue
            pix = page.get_pixmap(dpi=RENDER_DPI)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
            out.append((i, bgr, {"dpi": RENDER_DPI, "pdf_size_pt": [page.rect.width, page.rect.height]}))
    return out
