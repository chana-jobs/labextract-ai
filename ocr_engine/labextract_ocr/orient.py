"""Orientation (Tesseract OSD) + conservative enhancement, always on a copy.

Measured in the 2026-10-20 experiment: OSD fixed both rotated dev pages (p03, p25); Paddle tokens 24->109, 47->134.
Bounding boxes are produced in the ORIENTED frame; to_source_bbox maps them back to the source page.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .config import TESSERACT

ROTATE = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
MIN_OSD_CONF = 2.0


def osd(bgr: np.ndarray) -> tuple[int, float]:
    """(degrees clockwise that make the page upright, OSD confidence). (0, 0.0) if OSD gives nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "page.png"
        cv2.imencode(".png", bgr)[1].tofile(str(p))
        out = subprocess.run([TESSERACT, str(p), "stdout", "--psm", "0"], capture_output=True, text=True,
                             encoding="utf-8").stdout
    rot = re.search(r"Rotate: (\d+)", out)
    conf = re.search(r"Orientation confidence: ([\d.]+)", out)
    return (int(rot.group(1)), float(conf.group(1))) if rot and conf else (0, 0.0)


def enhance(bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(clahe, (0, 0), 1.0)
    return cv2.addWeighted(clahe, 1.5, blur, -0.5, 0)


def orient_and_enhance(bgr: np.ndarray) -> tuple[np.ndarray, dict]:
    rotation, conf = osd(bgr)
    applied = rotation if rotation in ROTATE and conf >= MIN_OSD_CONF else 0
    img = cv2.rotate(bgr, ROTATE[applied]) if applied else bgr
    return enhance(img), {"osd_rotate": rotation, "osd_conf": conf, "rotation_applied": applied,
                          "source_size": [bgr.shape[1], bgr.shape[0]]}


def to_source_bbox(bbox, frame: dict) -> list[int]:
    """Map an oriented-frame bbox [x0,y0,x1,y1] back to the source page pixel frame."""
    x0, y0, x1, y1 = bbox
    W, H = frame["source_size"]                       # source page size (before rotation)
    r = frame["rotation_applied"]
    if r == 0:
        pts = [(x0, y0), (x1, y1)]
    elif r == 90:          # rotated clockwise: src (x, y) -> (H-1-y, x); inverse: (xr, yr) -> (yr, H-1-xr)
        pts = [(y0, H - 1 - x0), (y1, H - 1 - x1)]
    elif r == 180:
        pts = [(W - 1 - x0, H - 1 - y0), (W - 1 - x1, H - 1 - y1)]
    else:                  # 270: src (x, y) -> (y, W-1-x); inverse: (xr, yr) -> (W-1-yr, xr)
        pts = [(W - 1 - y0, x0), (W - 1 - y1, x1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
