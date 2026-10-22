"""Engine: file -> pages -> orientation/enhancement -> OCR tokens -> field assignment -> structured result.

Result is an internal experiment structure (not an API contract). Every candidate carries:
  bbox         oriented-frame pixels (the frame the engine worked in)
  bbox_source  source-page pixels at render dpi (rotation undone)
  bbox_pt      source-page PDF points (only for PDF input)
"""
from __future__ import annotations

import copy
import os
import time
from pathlib import Path

import cv2

from . import ENGINE_VERSION
from .fields import extract
from .imageio import imwrite, load_pages, render_gray

HIRES_DPI = 300                      # native resolution of the sample scans (embedded JPEG)
from .ocr import OCR_VERSION, PaddleOCR
from .barcodes import decode as decode_barcodes
from .orient import ROTATE, orient_and_enhance, to_source_bbox

COLORS = {"first_name": (200, 0, 200), "last_name": (200, 0, 200), "israeli_id": (0, 140, 255),
          "received_date": (0, 0, 220), "sender": (0, 160, 0), "central_numbers": (220, 120, 0),
          "commitment_numbers": (160, 0, 0)}


class Engine:
    def __init__(self, gemini_transport=None):
        """gemini_transport: optional callable(request, timeout_s) -> response text for the Gemini fallback reader
        (tests inject a mock). Without it, a REAL transport is created only when LABOCR_GEMINI_TRANSPORT=real and
        the component is enabled and not in dry run; every other gemini_htr gate still applies."""
        self.ocr = PaddleOCR()
        self.gemini_transport = gemini_transport

    def _gemini_stage(self, fields, gray, views, path):
        """Local extraction -> unresolved located fields -> Gemini -> validation / merge. Any failure of this stage
        returns the LOCAL fields untouched (snapshot) - Gemini can never cost a local result."""
        from . import gemini_htr as G
        if not G.ENABLED:
            return fields, {"enabled": False}, 0
        transport = self.gemini_transport
        if transport is None and os.environ.get("LABOCR_GEMINI_TRANSPORT") == "real" and not G.DRY_RUN:
            try:
                transport = self.gemini_transport = G.real_transport()
            except Exception as exc:                       # missing package / key: stay local
                return fields, {"enabled": True, "sent": False, "error": f"transport unavailable: {type(exc).__name__}"}, 0
        snapshot = copy.deepcopy(fields)
        before = G.RUN_LEDGER.real_attempts()
        try:
            summ = G.run(fields, gray, views, transport=transport, source_path=path)
        except Exception as exc:                           # never lose local results
            err = {"enabled": True, "error": f"gemini stage failed ({type(exc).__name__}); local results kept"}
            return snapshot, err, G.RUN_LEDGER.real_attempts() - before
        return fields, summ, G.RUN_LEDGER.real_attempts() - before

    def extract_file(self, path, pages: list[int] | None = None, overlay_dir: Path | None = None) -> dict:
        path = Path(path)
        result = {"engine_version": ENGINE_VERSION, "ocr_version": OCR_VERSION, "source_file": path.name,
                  "network_calls": 0, "pages": []}
        for number, bgr, meta in load_pages(path, pages):
            try:
                self._page(result, path, number, bgr, meta, overlay_dir)
            except Exception as exc:                       # one bad page never stops the file
                result["pages"].append({"page": number, "processing_status": "FAILED",
                                        "error": type(exc).__name__, "fields": None})
        return result

    def _page(self, result, path, number, bgr, meta, overlay_dir):
        t0 = time.perf_counter()
        gray, frame = orient_and_enhance(bgr)
        t1 = time.perf_counter()
        tl = None
        if meta.get("dpi") and not frame["rotation_applied"]:
            from .textlayer import page_tokens
            tl = page_tokens(path, number, meta["dpi"])        # digital PDF: exact words, no OCR needed
        if tl:
            from .privacy import mask_print_footer
            tokens, masked = mask_print_footer(tl)
            ocr_info = {"cache": "text_layer", "seconds": 0.0, "key": None, "footer_masked": masked}
        else:
            tokens, ocr_info = self.ocr.read_page(gray)
        t2 = time.perf_counter()
        rec_before = self.ocr.calls
        raw = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)            # same orientation, no enhancement
        if frame["rotation_applied"]:
            raw = cv2.rotate(raw, ROTATE[frame["rotation_applied"]])
        tb = time.perf_counter()
        codes = decode_barcodes([raw, gray])
        barcode_s = time.perf_counter() - tb
        hires_cache = {}

        def hires(path=path, number=number, rot=frame["rotation_applied"]):
            if "img" not in hires_cache:            # rendered only if a stamp region needs it
                g = render_gray(path, number, HIRES_DPI)
                hires_cache["img"] = cv2.rotate(g, ROTATE[rot]) if (g is not None and rot) else g
            return hires_cache["img"]
        fields = extract(tokens, gray, self.ocr, codes, views={"raw": raw, "hires": hires})
        t3 = time.perf_counter()
        # optional Gemini fallback (OFF by default; dry run unless every gate passes)
        fields, gemini, api_calls = self._gemini_stage(fields, gray, {"raw": raw, "hires": hires}, path)
        result["network_calls"] += api_calls
        t4 = time.perf_counter()
        self._add_source_boxes(fields, frame, meta)
        page = {"page": number, "processing_status": "DONE", "frame": {**frame, "dpi": meta["dpi"], "oriented_size": [gray.shape[1], gray.shape[0]]},
                "ocr": {"tokens": len(tokens), "cache": ocr_info["cache"], "cache_key": ocr_info["key"]},
                "timing_s": {"orient_enhance": round(t1 - t0, 2),
                             "ocr": round(t2 - t1, 2) if ocr_info["cache"] == "miss" else 0.0,
                             "ocr_original_run": ocr_info["seconds"],
                             "barcodes": round(barcode_s, 2),
                             "assign_and_probes": round(t3 - t2, 2),
                             "gemini": round(t4 - t3, 2)},
                "barcodes": [{"text": b["text"], "format": b["format"], "bbox": b["bbox"]} for b in codes],
                "local_rec_calls_for_probes": self.ocr.calls - rec_before,
                "fields": fields,
                "review_required": [f for f, v in fields.items() if v["status"] != "proposed"]}
        if gemini.get("enabled"):
            page["gemini"] = gemini                    # plan / ledger only: no images, no texts, no key
        result["pages"].append(page)
        if overlay_dir:
            page["overlay"] = str(self._overlay(gray, fields, overlay_dir / f"{path.stem}_p{number:02d}.png"))

    @staticmethod
    def _add_source_boxes(fields, frame, meta):
        for f in fields.values():
            for c in f["candidates"] + f.get("regions", []):
                c["bbox_source"] = to_source_bbox(c["bbox"], frame)
                if meta["dpi"]:
                    c["bbox_pt"] = [round(v * 72 / meta["dpi"], 1) for v in c["bbox_source"]]

    @staticmethod
    def _overlay(gray, fields, out: Path) -> Path:
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for name, f in fields.items():
            for c in f["candidates"]:
                x0, y0, x1, y1 = c["bbox"]
                chosen = f["status"] == "proposed" and (c["value"] == f.get("value") or c["value"] in f.get("values", []))
                cv2.rectangle(img, (x0 - 3, y0 - 3), (x1 + 3, y1 + 3), COLORS[name], 4 if chosen else 1)
                cv2.putText(img, name.split("_")[0][:6] + ("" if chosen else "?"), (x0, max(12, y0 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLORS[name], 2)
        imwrite(out, img)
        return out
