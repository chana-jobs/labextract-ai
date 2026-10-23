"""Pre-processing for handwriting: separate the colored pen ink (blue / green) from the black print and the yellow
highlighter, and show it as dark strokes on white, enlarged. The reader gets the original crop on top and the
ink-only version below it.
"""
import io

import cv2
import numpy as np
from PIL import Image

# OpenCV hue is 0-179. Blue/green pens ~ 75-140; yellow highlighter ~ 20-40 is excluded.
# Absolute chroma (max-min channel) rejects the colored fringes a scanner leaves around black print: those pixels
# are dark, so their relative saturation is high but their absolute chroma is small.
HUE_LO, HUE_HI, SAT_MIN, VAL_MAX, CHROMA_MIN = 70, 145, 80, 245, 70


def ink_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    chroma = bgr.max(axis=2).astype(np.int16) - bgr.min(axis=2).astype(np.int16)
    m = ((h >= HUE_LO) & (h <= HUE_HI) & (s >= SAT_MIN) & (v <= VAL_MAX) & (chroma >= CHROMA_MIN))
    m = m.astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))   # drop speckles
    m = cv2.dilate(m, np.ones((2, 2), np.uint8))                          # close thin broken strokes
    return m


def ink_fraction(jpeg):
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return float((ink_mask(bgr) > 0).mean())


def enhance(jpeg, scale=2.0):
    """Return JPEG: [original crop] over [ink-only strokes], both upscaled. Falls back to the original if no ink."""
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    m = ink_mask(bgr)
    if (m > 0).mean() < 0.002:
        return jpeg
    ink = np.full_like(bgr, 255)
    ink[m > 0] = (40, 20, 0)
    sep = np.full((12, bgr.shape[1], 3), 160, np.uint8)
    both = np.vstack([bgr, sep, ink])
    rgb = cv2.cvtColor(both, cv2.COLOR_BGR2RGB)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=93)
    return buf.getvalue()
