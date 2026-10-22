"""Local paths. Override with environment variables; nothing here is secret."""
import os
import shutil
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
# Tesseract 5 (osd + heb + eng): LABOCR_TESSERACT, else `tesseract` on PATH (Linux: apt), else the Windows default
TESSERACT = (os.environ.get("LABOCR_TESSERACT") or shutil.which("tesseract")
             or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
# PaddleOCR-Hebrew (code + ONNX models): LABOCR_PADDLE_REPO, else <package>/vendor/paddleocr-hebrew (handoff layout),
# else <repo root>/python/paddleocr-hebrew (dev layout). No user-specific path.
_VENDOR = LAB / "vendor" / "paddleocr-hebrew"
PADDLE_REPO = Path(os.environ.get("LABOCR_PADDLE_REPO") or
                   (_VENDOR if _VENDOR.exists() else LAB.parent / "python" / "paddleocr-hebrew"))
PADDLE_MODELS = PADDLE_REPO / "models"
CACHE_DIR = Path(os.environ.get("LABOCR_CACHE", LAB / "out" / "cache"))
RENDER_DPI = 200
