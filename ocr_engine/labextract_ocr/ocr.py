"""PaddleOCR-Hebrew (word-det + mobile-word CTC rec, ONNX, CPU) with a content-addressed token cache.

Cache key = sha1 of the enhanced oriented image, so identical input never re-runs OCR.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, asdict

import cv2
import numpy as np
import onnxruntime as ort

from .config import CACHE_DIR, PADDLE_MODELS, PADDLE_REPO

sys.path.insert(0, str(PADDLE_REPO))
from paddleocr_hebrew.detect import detect_words, crop_bbox                          # noqa: E402
from paddleocr_hebrew.rec_decode import load_dict, prep_dynamic_target_w, quantize_width  # noqa: E402

OCR_VERSION = "paddle-hebrew word-det+mobile-word, rec h48, det thresh0.40 unclip1.3 max_side2560"


@dataclass
class Token:
    id: int
    text: str
    conf: float
    bbox: list[int]             # [x0, y0, x1, y1] in the oriented frame

    @property
    def cx(self): return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self): return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def h(self): return self.bbox[3] - self.bbox[1]

    @property
    def w(self): return self.bbox[2] - self.bbox[0]


class PaddleOCR:
    def __init__(self):
        prov = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(PADDLE_MODELS / "word-det" / "det.onnx"), providers=prov)
        self.rec = ort.InferenceSession(str(PADDLE_MODELS / "mobile-word" / "rec.onnx"), providers=prov)
        self.vocab = load_dict(str(PADDLE_MODELS / "charset_v2f.txt"))   # vocab[0] = CTC blank
        self.rec_in = self.rec.get_inputs()[0].name
        self.calls = 0

    def recognize(self, crop: np.ndarray) -> tuple[str, float]:
        """One text line crop -> (text, mean char confidence)."""
        if crop is None or crop.size == 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
            return "", 0.0
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        arr = prep_dynamic_target_w(crop, h=48, target_w=quantize_width(crop, h=48, round_to=16))[None]
        probs = self.rec.run(None, {self.rec_in: arr.astype(np.float32)})[0][0]
        self.calls += 1
        ids, maxp = probs.argmax(-1), probs.max(-1)
        chars, confs, prev = [], [], -1
        for i, p in zip(ids, maxp):
            if i != prev and i != 0 and i < len(self.vocab):
                chars.append(self.vocab[i]); confs.append(float(p))
            prev = i
        return "".join(chars), (float(np.mean(confs)) if confs else 0.0)

    def read_page(self, gray: np.ndarray) -> tuple[list[Token], dict]:
        key = hashlib.sha1(gray.tobytes() + str(gray.shape).encode() + OCR_VERSION.encode()).hexdigest()
        cache = CACHE_DIR / f"{key}.json"
        from .privacy import mask_print_footer
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            tokens, masked = mask_print_footer([Token(**t) for t in data["tokens"]])
            return tokens, {"cache": "hit", "seconds": data["seconds"], "key": key, "footer_masked": masked}
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        t0 = time.perf_counter()
        boxes = detect_words(bgr, self.det, self.det.get_inputs()[0].name, thresh=0.40, unclip_ratio=1.3,
                             max_side=2560)
        tokens = []
        for box in boxes:
            text, conf = self.recognize(crop_bbox(bgr, box, pad=2))
            if text.strip():
                tokens.append(Token(len(tokens), text, round(conf, 3), [int(v) for v in box]))
        seconds = round(time.perf_counter() - t0, 2)
        tokens, masked = mask_print_footer(tokens)          # never cache a print footer
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ocr_version": OCR_VERSION, "seconds": seconds,
                                     "tokens": [asdict(t) for t in tokens]}, ensure_ascii=False), encoding="utf-8")
        return tokens, {"cache": "miss", "seconds": seconds, "key": key, "footer_masked": masked}

    def read_crop(self, gray_crop: np.ndarray) -> list[Token]:
        """Detection + recognition on a small region (not cached; used for targeted re-reads)."""
        bgr = cv2.cvtColor(gray_crop, cv2.COLOR_GRAY2BGR) if gray_crop.ndim == 2 else gray_crop
        boxes = detect_words(bgr, self.det, self.det.get_inputs()[0].name, thresh=0.35, unclip_ratio=1.4,
                             max_side=1280)
        out = []
        for box in boxes:
            text, conf = self.recognize(crop_bbox(bgr, box, pad=2))
            if text.strip():
                out.append(Token(len(out), text, round(conf, 3), [int(v) for v in box]))
        return out

    def reread(self, gray: np.ndarray, bbox, pads=((6, 4), (14, 6))) -> list[tuple[str, float]]:
        """Independent local re-reads of one token with wider horizontal/vertical padding."""
        H, W = gray.shape[:2]
        x0, y0, x1, y1 = bbox
        out = []
        for px, py in pads:
            crop = gray[max(0, y0 - py):min(H, y1 + py), max(0, x0 - px):min(W, x1 + px)]
            out.append(self.recognize(crop))
        return out
