"""Extraction engine selection behind the existing drafts flow. No API, contract or DB change.

LABEXTRACT_ENGINE:
  pdf_text (default)  rule-based text-layer extraction only (extraction/pdf_text.py)
  hybrid              pdf_text for pages with a usable text layer; the OCR engine (lab-ocr-1.0) for scanned pages,
                      reversed-text pages and images
  ocr                 the OCR engine for every page

The OCR engine is the frozen handoff package, loaded from LABEXTRACT_OCR_DIR (else <project>/ocr_engine). It is
never modified here. Its token cache holds page text (patient data): it goes to a private temporary folder that is
emptied after every document. The Gemini stage is not approved for the product and is forced off.

If the OCR engine cannot run, the affected pages are FAILED with a TECHNICAL_FAILURE issue (never MISSING, never
read as absent); pages that pdf_text already read are kept.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

from extraction import labx_overlay, pdf_text
from shared.contracts import (Commitment, ExistenceStatus, ExtractionStatus, FieldName, FieldValue, Form, Issue,
                              IssueReason, ProcessingStatus, SplitStatus, UncertaintyReason)

ENGINES = ("pdf_text", "hybrid", "ocr")
NO_TEXT_FLAGS = {"no_text_layer", "text_order_unsupported"}
ROOT = Path(__file__).resolve().parents[1]


class OcrUnavailable(Exception):
    """The OCR engine or one of its requirements (package, Tesseract) is missing on this host."""


def engine_mode() -> str:
    mode = os.environ.get("LABEXTRACT_ENGINE", "pdf_text")
    if mode not in ENGINES:
        raise RuntimeError(f"Unknown LABEXTRACT_ENGINE: {mode}; allowed: {list(ENGINES)}")
    return mode


# ------------------------------------------------------------------ OCR engine (loaded once per process)
_lock = threading.Lock()            # one document at a time: the engine and its cache folder are shared


@lru_cache
def _ocr():
    folder = Path(os.environ.get("LABEXTRACT_OCR_DIR") or ROOT / "ocr_engine")
    if not (folder / "labextract_ocr" / "engine.py").is_file():
        raise OcrUnavailable(f"OCR engine package not found in {folder}")
    cache = Path(tempfile.mkdtemp(prefix="labextract_ocr_cache_"))
    # read by the engine at import time; must be set before the first import
    os.environ["LABOCR_CACHE"] = str(cache)
    os.environ["LABOCR_GEMINI"] = "off"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    try:
        from labextract_ocr.config import TESSERACT
        from labextract_ocr.engine import Engine
        from labextract_ocr.to_contract import page_to_form
    except ImportError as exc:
        raise OcrUnavailable(f"OCR engine dependencies missing: {exc.name}") from None
    if not (shutil.which(TESSERACT) or Path(TESSERACT).is_file()):
        raise OcrUnavailable("Tesseract binary not found")
    return Engine(), page_to_form, cache


def _clear(cache: Path) -> None:
    for item in cache.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def _failed_form(file_id: str, page: int, note: str) -> Form:
    form_id = f"{file_id}-p{page + 1}"
    return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page}], split_status=SplitStatus.UNCERTAIN,
                processing_status=ProcessingStatus.FAILED,
                issues=[Issue(issue_id=f"{form_id}-failed", form_id=form_id, reason=IssueReason.TECHNICAL_FAILURE,
                              note=note)])


DATE_LINE = re.compile(r"[-–~]\s?\d{1,2}\s?[-–~]")      # a dater prints the month as "-08-"
YEAR = re.compile(r"20\d\d")
TALL_YEAR = 1.4     # the dater's year is larger than print; measured on part 1: 1.3 already adds a false hit


def date_line_boxes(tokens: list[dict]) -> list[list[int]]:
    """Oriented-frame boxes of received-date lines found in OCR tokens: a "-mm-" token or a standalone year printed
    larger than the page's text, grouped per line and widened to hold the day, "התקבל" above and the frame.
    Located only - never read. Part 1: stamps located 10/13 (was 7), 0 false hits on the 13 pages without one."""
    if not tokens:
        return []
    med = statistics.median(t["bbox"][3] - t["bbox"][1] for t in tokens)
    anchors = [t["bbox"] for t in tokens
               if DATE_LINE.fullmatch(t["text"].strip())
               or (YEAR.fullmatch(t["text"].strip()) and t["bbox"][3] - t["bbox"][1] >= TALL_YEAR * med)]
    lines: list[list[int]] = []
    for b in sorted(anchors):
        h = b[3] - b[1]
        line = next((ln for ln in lines if b[1] < ln[3] and b[3] > ln[1] and b[0] - ln[2] < 6 * h), None)
        if line:
            line[:] = [min(line[0], b[0]), min(line[1], b[1]), max(line[2], b[2]), max(line[3], b[3])]
        else:
            lines.append(list(b))
    out = []
    for x0, y0, x1, y1 in lines:
        h = y1 - y0
        out.append([x0 - 5 * h, y0 - 4 * h, x1 + 3 * h, y1 + 3 * h])
    return out


def _date_lines(page: dict, cache: Path) -> list[dict]:
    """[{bbox, rotation, dpi, source_size}] for one OCR result page: bbox in the pixels of the page as the engine
    loaded it (a PDF at `dpi`; an image possibly upscaled to `source_size`); [] when nothing was located."""
    key = (page.get("ocr") or {}).get("cache_key")
    frame = page.get("frame")
    if page.get("processing_status") != "DONE" or not key or not frame:
        return []
    try:
        tokens = json.loads((cache / f"{key}.json").read_text(encoding="utf-8"))["tokens"]
    except (OSError, ValueError, KeyError):
        return []
    from labextract_ocr.orient import to_source_bbox
    W, H = frame["oriented_size"]
    return [{"bbox": to_source_bbox([max(0, b[0]), max(0, b[1]), min(W - 1, b[2]), min(H - 1, b[3])], frame),
             "rotation": frame["rotation_applied"], "dpi": frame.get("dpi"), "source_size": frame["source_size"]}
            for b in date_line_boxes(tokens)]


def ocr_pages(file_id: str, content: bytes, extension: str, pages: list[int]) -> dict[int, Form]:
    """{0-based page: Form} from the OCR engine for the given 0-based pages."""
    try:
        engine, page_to_form, cache = _ocr()
    except OcrUnavailable:
        return {p: _failed_form(file_id, p, "OCR is not available on this server; enter values from the source")
                for p in pages}
    t_wait = time.monotonic()
    with _lock, tempfile.TemporaryDirectory(prefix="labextract_ocr_job_") as job:
        t_ocr = time.monotonic()
        path = Path(job) / f"document{extension}"
        path.write_bytes(content)
        try:
            result = engine.extract_file(path, [p + 1 for p in pages] if extension == ".pdf" else None)
            for page in result["pages"]:        # the token cache is emptied below: locate the date line now
                labx_overlay.remember_date_lines(file_id, page["page"] - 1, _date_lines(page, cache))
        finally:
            _clear(cache)
        print(f"[ocr] pages={[p + 1 for p in pages]} waited_for_lock={t_ocr - t_wait:.1f}s "
              f"ocr={time.monotonic() - t_ocr:.1f}s", flush=True)
    forms = {}
    for page in result["pages"]:
        form = page_to_form(file_id, page, result["engine_version"])
        forms[page["page"] - 1] = Form.model_validate(form.model_dump(mode="json"))
    for p in pages:     # a page the engine did not return is a technical failure, not an empty page
        forms.setdefault(p, _failed_form(file_id, p, "OCR returned no result for this page"))
    return forms


# ------------------------------------------------------------------ entry point used by backend/drafts.py
def _needs_ocr(form: Form) -> bool:
    exam = form.examinations[0] if form.examinations else None
    return exam is not None and bool(NO_TEXT_FLAGS & set(exam.first_name.flags))


def _read_something(exam) -> bool:
    """Any value or candidate on the form; a page with nothing read at all (no text, blank) gets no row."""
    fields = [exam.first_name, exam.last_name, exam.israeli_id, exam.sample_received_date, exam.sender,
              *exam.central_numbers]
    return any(f.normalized or f.raw or f.candidates for f in fields)


def _ensure_commitment_row(form: Form) -> Form:
    """A processed form with no commitment number still gets ONE commitment row with an empty number, so the
    fields that were read (name, ID, date, sender, central numbers) reach the review grid and the export instead
    of the whole form disappearing. The COMMITMENT_COUNT_UNCERTAIN issue stays: there may be more rows."""
    if form.processing_status != ProcessingStatus.DONE or all(e.commitments for e in form.examinations):
        return form
    exams = [e if e.commitments or not _read_something(e) else e.model_copy(update={"commitments": [Commitment(
        commitment_id=f"{form.form_id}-c1", examination_id=e.examination_id,
        existence_status=ExistenceStatus.UNCERTAIN, internal_index=0,
        number=FieldValue(field_name=FieldName.COMMITMENT_NUMBER, extraction_status=ExtractionStatus.MISSING,
                          uncertainty_reason=[UncertaintyReason.UNGROUNDED],
                          flags=["no commitment number found"]))]}) for e in form.examinations]
    return form.model_copy(update={"examinations": exams})


def page_count(content: bytes, extension: str) -> int:
    if extension != ".pdf":
        return 1
    import pymupdf
    with pymupdf.open(stream=content, filetype="pdf") as document:
        return document.page_count


def extract_page(file_id: str, content: bytes, extension: str, page: int) -> Form:
    """The form of ONE 0-based page, with the same engine rules as extract_document. One page of OCR fits one
    request, whatever the length of the document. With LABEXTRACT_LABX=on the multi-model consensus layer then
    reads the four patient fields (extraction/labx_overlay.py); on a scanned page it runs in parallel with the
    local OCR, so the page takes about as long as the slower of the two instead of both added up."""
    if not 0 <= page < page_count(content, extension):
        raise IndexError(page)
    mode = engine_mode()
    if mode == "ocr":
        pending = labx_overlay.start(content, extension, page)
        return _ensure_commitment_row(labx_overlay.apply(
            content, extension, page, ocr_pages(file_id, content, extension, [page])[page], pending))
    if extension == ".pdf":
        import pymupdf
        with pymupdf.open(stream=content, filetype="pdf") as document:
            form = pdf_text.extract_page(file_id, page, document[page])
    else:
        [form] = pdf_text.extract_document(file_id, content, extension)
    if mode == "hybrid" and _needs_ocr(form):
        pending = labx_overlay.start(content, extension, page)
        return _ensure_commitment_row(labx_overlay.apply(
            content, extension, page, ocr_pages(file_id, content, extension, [page])[page], pending))
    return _ensure_commitment_row(labx_overlay.apply(content, extension, page, form))


def extract_document(file_id: str, content: bytes, extension: str) -> list[Form]:
    """One form per page, as pdf_text.extract_document; the engine is chosen by LABEXTRACT_ENGINE."""
    forms = _extract_document(file_id, content, extension)
    return [_ensure_commitment_row(labx_overlay.apply(content, extension, p, form)) for p, form in enumerate(forms)]


def _extract_document(file_id: str, content: bytes, extension: str) -> list[Form]:
    mode = engine_mode()
    if mode == "pdf_text":
        return pdf_text.extract_document(file_id, content, extension)
    if mode == "ocr":
        if extension == ".pdf":
            import pymupdf
            with pymupdf.open(stream=content, filetype="pdf") as document:
                count = document.page_count
        else:
            count = 1
        forms = ocr_pages(file_id, content, extension, list(range(count)))
        return [forms[p] for p in range(count)]
    forms = pdf_text.extract_document(file_id, content, extension)
    pending = [p for p, form in enumerate(forms) if _needs_ocr(form)]
    if pending:
        read = ocr_pages(file_id, content, extension, pending)
        forms = [read.get(p, form) for p, form in enumerate(forms)]
    return forms
