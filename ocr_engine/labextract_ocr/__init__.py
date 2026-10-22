"""LabExtract OCR engine 1.0 (frozen for handoff 22.10.2026) — local by default (Tesseract OSD + PaddleOCR-Hebrew ONNX,
PDF text layer when present). The optional Gemini fallback is off unless enabled by environment variables.

Entry points: run_extract.py (CLI), labextract_ocr.engine.Engine, labextract_ocr.to_contract.page_to_form.
"""
ENGINE_VERSION = "lab-ocr-1.0"
