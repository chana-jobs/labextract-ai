"""Multi-model consensus layer (labx) for the four patient fields: first name, last name, Israeli ID, received date.

Enabled with LABEXTRACT_LABX=on. Runs after the page engine (pdf_text / OCR) and changes only those four fields:
  * labx ACCEPTED a value (two different vision models read it "clear" and agree, code validators pass):
      - the page engine has no complete value, or the same value  -> COMPLETE with the labx value
      - the page engine has a DIFFERENT complete value             -> CONFLICT with both candidates (a human decides)
  * labx did not accept: the page engine's field is kept; for ID / date the model readings are added as candidates.
    Names are never accepted (the models invent names on unclear handwriting): when both models agree, the name is
    pre-filled as UNCERTAIN for a person to approve or correct; when they disagree, each reading is a candidate.
Approval stays PENDING (verification NONE) unless LABX_AUTO_APPROVE=1, which reports verification AGREEMENT so the
server's auto-approval policy (backend/readiness.py) may approve it.
If labx fails (no key, quota, network), the form is returned unchanged.
The labx package is loaded from LABX_DIR (default C:\\accthon_chani\\AI).
"""
from __future__ import annotations

import gc
import logging
import os
import re
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from shared.contracts import (
    Attribution, Candidate, Dimensions, Evidence, ExtractionStatus, FieldName, FieldValue, Form, ProcessingStatus,
    Readability, UncertaintyReason, Verification,
)

log = logging.getLogger("labx_overlay")
ENGINE_VERSION = "labx-1.0"
METHOD = "labx_multimodel_consensus"
FIELDS = {"first_name": "first_name", "last_name": "last_name", "id": "israeli_id", "received_date": "sample_received_date"}


def enabled() -> bool:
    return os.environ.get("LABEXTRACT_LABX", "off") == "on"


@lru_cache
def _labx():
    folder = Path(os.environ.get("LABX_DIR") or r"C:\accthon_chani\AI")
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    from labx import engine
    return engine


def _norm(field: str, value: str) -> str:
    return value.zfill(9) if field == "id" else value


def _raw_text(candidate: str) -> str:
    """'page:gemini-3.5-flash=16/6/26(c)' -> '16/6/26'."""
    return re.sub(r"\([a-z]\)$", "", candidate.split("=", 1)[-1]).strip()


def _evidence(page0: int, note: str) -> Evidence:
    return Evidence(page=page0, bbox=None, method=METHOD, engine_version=ENGINE_VERSION, role_basis=note[:500],
                    tier=Attribution.STRONG)


def _accepted(name, field, entry, page0, auto) -> FieldValue:
    value = _norm(field, entry["value"])
    agreeing = [c for c in entry["candidates"]]
    note = f"labx: {entry['reason']} ({'; '.join(agreeing)})"
    return FieldValue(field_name=name, raw=entry["value"], normalized=value,
                      extraction_status=ExtractionStatus.COMPLETE,
                      dimensions=Dimensions(readability=Readability.OK, attribution=Attribution.STRONG,
                                            verification=Verification.AGREEMENT if auto else Verification.NONE),
                      candidates=[Candidate(raw=entry["value"], normalized=value, tier=Attribution.STRONG,
                                            role_basis=note[:500], evidence=[_evidence(page0, note)])],
                      evidence=[_evidence(page0, note)])


def _name_proposal(old: FieldValue, entry: dict, page0: int) -> FieldValue:
    """A name labx did not accept. Both models agreed: the value is pre-filled as UNCERTAIN (never auto-approved, a
    person approves or corrects it). They disagreed: each valid reading is offered as a candidate, no value."""
    proposed, readings = entry.get("proposed"), entry.get("readings") or []
    if not proposed and not readings:
        return old
    note = f"labx: {entry['reason']} ({'; '.join(entry['candidates'])})"
    cands = [Candidate(raw=v, normalized=v, role_basis=note[:500], evidence=[_evidence(page0, note)])
             for v in ([proposed] if proposed else readings)]
    return FieldValue(field_name=old.field_name, raw=proposed, normalized=proposed,
                      extraction_status=ExtractionStatus.UNCERTAIN, uncertainty_reason=[UncertaintyReason.RECOGNITION],
                      flags=["labx_name_proposal" if proposed else "labx_name_readings_disagree"],
                      candidates=list(old.candidates) + cands, evidence=list(old.evidence) + [_evidence(page0, note)])


def _merge(old: FieldValue, field: str, entry: dict, page0: int, auto: bool) -> FieldValue:
    name = old.field_name
    if entry["status"] == "accepted":
        new = _accepted(name, field, entry, page0, auto)
        if old.extraction_status == ExtractionStatus.COMPLETE and old.normalized and old.normalized != new.normalized:
            return FieldValue(field_name=name, extraction_status=ExtractionStatus.CONFLICT,
                              uncertainty_reason=[UncertaintyReason.CANDIDATE_CONFLICT],
                              candidates=list(old.candidates) + list(new.candidates),
                              evidence=list(old.evidence) + list(new.evidence),
                              flags=["labx_disagrees_with_page_engine"])
        return new
    if old.extraction_status == ExtractionStatus.COMPLETE:
        return old
    if field in ("first_name", "last_name"):
        return _name_proposal(old, entry, page0)
    if not entry["candidates"]:
        return old
    # ID / date not accepted: one clean candidate per distinct reading, for the reviewer; which model read what
    # (the engine's "source:model=raw(legibility)" strings) stays in role_basis / evidence
    note = f"labx reading (not accepted: {entry['reason']}): {'; '.join(entry['candidates'])}"
    readings = entry.get("readings") or []
    if readings:
        values = [(v, _norm(field, v)) for v in readings]
    else:   # no valid reading: show the raw text the models wrote, never as a normalized value
        values = list(dict.fromkeys((_raw_text(c), None) for c in entry["candidates"] if _raw_text(c)))
    if not values:
        return old
    cands = [Candidate(raw=raw, normalized=norm, role_basis=note[:500], evidence=[_evidence(page0, note)])
             for raw, norm in values]
    reasons = list(old.uncertainty_reason) or [UncertaintyReason.RECOGNITION]
    status = old.extraction_status if old.extraction_status != ExtractionStatus.MISSING else ExtractionStatus.UNCERTAIN
    return old.model_copy(update={"extraction_status": status, "uncertainty_reason": reasons,
                                  "candidates": list(old.candidates) + cands})


# one labx page at a time per server: it already runs next to the OCR, and two of each exhausted the memory
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("LABX_WORKERS", "1")), thread_name_prefix="labx")


def crops_first() -> bool:
    """LABX_MODE=crops_first: send only locally located crops; the full page only when a field was not located."""
    return os.environ.get("LABX_MODE", "full_page") == "crops_first"


# regions located locally after the OCR (engine_select._local_regions), kept until the page is merged:
# {(file_id, page0): {"regions": [...], "form_dates": [...]}}
_REGIONS: dict[tuple[str, int], dict] = {}
NO_REGIONS = {"regions": [], "form_dates": []}


def remember_regions(file_id: str, page0: int, local: dict | None) -> None:
    if local:
        _REGIONS[(file_id, page0)] = local
    else:
        _REGIONS.pop((file_id, page0), None)


@lru_cache
def _crops():
    _labx()                               # puts LABX_DIR on sys.path
    from labx import crops
    return crops


_ROTATE = {90: "ROTATE_90_CLOCKWISE", 180: "ROTATE_180", 270: "ROTATE_90_COUNTERCLOCKWISE"}


def _region_crops(content: bytes, extension: str, page0: int, regions: list[dict]) -> list[tuple[str, bytes, dict]]:
    """Cut each located region from the page as the OCR engine loaded it, turned upright, enlarged:
    [(kind, jpeg, region)]."""
    import cv2
    import numpy as np
    if extension == ".pdf":
        import pymupdf
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            png = doc[page0].get_pixmap(dpi=regions[0]["dpi"]).tobytes("png")
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    else:
        img = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    W, H = regions[0]["source_size"]
    if (img.shape[1], img.shape[0]) != (W, H):       # an image the engine upscaled
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_CUBIC)
    out = []
    for r in regions:
        x0, y0, x1, y1 = r["bbox"]
        if r["kind"] == "central_annotation":       # a tight ink block: keep a margin so no digit is cut
            mx, my = (x1 - x0) // 8 + 10, (y1 - y0) // 8 + 10
            x0, y0, x1, y1 = x0 - mx, y0 - my, x1 + mx, y1 + my
        elif r["kind"] == "commitment_cell":        # one token / cell box: room for the whole number, not the row
            mx, my = (x1 - x0) // 6 + 10, (y1 - y0) + 10
            x0, y0, x1, y1 = x0 - mx, y0 - my, x1 + mx, y1 + my
        c = img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        if c.size == 0:
            continue
        if r["rotation"] in _ROTATE:
            c = cv2.rotate(c, getattr(cv2, _ROTATE[r["rotation"]]))
        f = max(1.0, 900 / max(c.shape[:2]))
        c = cv2.resize(c, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        out.append((r["kind"], cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes(), r))
    return out


def _run_crops(content: bytes, extension: str, page0: int, local: dict):
    """("sent", result or None on failure) or ("escalate", reason) when a field could not be located locally.
    The received date is located by a stamp date line or by the lab's handwritten annotation (the date is written
    under the central number). A technical failure never escalates to the full page: the form is kept as it is."""
    t0 = time.monotonic()
    try:
        hand = _crops().handwriting_crops(content, extension, page0)
        if not hand:
            return "escalate", "no handwriting located"
        regions = _region_crops(content, extension, page0, local["regions"]) if local["regions"] else []
        if not any(k in ("date_line", "central_annotation") for k, _, _ in regions):
            return "escalate", "no received-date region located"
        res = _crops().extract(hand + [(k, j) for k, j, _ in regions], form_dates=local["form_dates"],
                               commitments=[{"item_id": r["item_id"], "candidates": r["candidates"]}
                                            for k, _, r in regions if k == "commitment_cell"])
    except Exception as exc:
        _report(page0, time.monotonic() - t0, None, f"{type(exc).__name__}: {str(exc)[:300]}")
        return "sent", None
    finally:
        gc.collect()
    res = {"fields": res["fields"], "commitments": res.get("commitments", []),
           "log": {"crop_reads": res["log"]["crop_reads"], "crop_kinds": res["log"]["crop_kinds"]}}
    _report(page0, time.monotonic() - t0, res)
    return "sent", res


def _report(page0: int, seconds: float, res: dict | None, error: str | None = None) -> None:
    """One log line per page: timing, field statuses and reader errors. Never field values (patient data)."""
    if res is None:
        print(f"[labx] page={page0 + 1} FAILED after {seconds:.1f}s: {error}", flush=True)
        return
    statuses = {f: e["status"] for f, e in res["fields"].items()}
    log = res.get("log", {})
    reads = log.get("page_reads") or log.get("crop_reads") or {}
    if "crop_kinds" in log:
        print(f"[labx] page={page0 + 1} sent=crops {log['crop_kinds']}", flush=True)
    errors = {m.split(":")[-1]: r["_error"][:160] for m, r in reads.items() if isinstance(r, dict) and "_error" in r}
    print(f"[labx] page={page0 + 1} {seconds:.1f}s rotated={res.get('rotated')} fields={statuses} "
          f"readers={[m.split(':')[-1] for m in reads]} errors={errors}", flush=True)


def _run(content: bytes, extension: str, page0: int):
    t0 = time.monotonic()
    try:
        res = _labx().extract_bytes(content, extension, page0)
    except Exception as exc:              # never break the upload because of the extra layer
        _report(page0, time.monotonic() - t0, None, f"{type(exc).__name__}: {str(exc)[:300]}")
        return None
    finally:
        gc.collect()                      # page images and OpenCV buffers: give the memory back before the next page
    res = {"fields": res["fields"], "rotated": res.get("rotated"),
           "log": {"page_reads": res.get("log", {}).get("page_reads", {})}}   # keep only what the merge / log need
    _report(page0, time.monotonic() - t0, res)
    return res


def start(content: bytes, extension: str, page0: int) -> Future | None:
    """Start the labx reading in the background (it runs while the local OCR reads the same page). Not in
    crops-first mode: the crops need the OCR result first, and the full page must not be sent before we know."""
    return _pool.submit(_run, content, extension, page0) if enabled() and not crops_first() else None


def apply(content: bytes, extension: str, page0: int, form: Form, pending: Future | None = None) -> Form:
    """Merge the labx result into the page form. `pending` = a reading already started with start()."""
    local = _REGIONS.pop((form.file_id, page0), None) or NO_REGIONS
    if not enabled() or form.processing_status != ProcessingStatus.DONE or not form.examinations:
        return form
    exam = form.examinations[0]
    if pending is None and all(getattr(exam, f).extraction_status == ExtractionStatus.COMPLETE
                               for f in FIELDS.values()):
        return form                       # text-layer page already read all four exactly: save model quota
    res = None
    if crops_first() and pending is None:
        outcome, res = _run_crops(content, extension, page0, local)
        if outcome == "escalate":
            print(f"[labx] page={page0 + 1} sent=full_page ({res})", flush=True)
            res = _run(content, extension, page0)
    else:
        res = pending.result() if pending is not None else _run(content, extension, page0)
    if res is None:
        return form
    auto = os.environ.get("LABX_AUTO_APPROVE") == "1"
    form = form.model_copy(deep=True)
    exam = form.examinations[0]
    for field, name in FIELDS.items():
        setattr(exam, name, _merge(getattr(exam, name), field, res["fields"][field], page0, auto))
    if "central_number" in res["fields"]:           # crops-first only: the lab's handwritten central number
        exam.central_numbers = _merge_central(exam.central_numbers, res["fields"]["central_number"], page0, auto)
    by_id = {c.commitment_id: c for c in exam.commitments}
    for entry in res.get("commitments", []):        # crops-first only: commitment cells the OCR could not decide
        c = by_id.get(f"{form.form_id}-{entry['item_id']}")
        if c is not None:
            c.number = _merge(c.number, "commitment_number", entry, page0, auto)
    return Form.model_validate(form.model_dump())


def _merge_central(old: list[FieldValue], entry: dict, page0: int, auto: bool) -> list[FieldValue]:
    """A central number the OCR already read (COMPLETE) is never touched. Otherwise the labx entry is merged into the
    first central FieldValue with the ID / date rules: accepted -> COMPLETE (PENDING), readings -> candidates."""
    if any(fv.extraction_status == ExtractionStatus.COMPLETE for fv in old):
        return old
    base = old[0] if old else FieldValue(field_name=FieldName.CENTRAL_NUMBERS, extraction_status=ExtractionStatus.MISSING)
    merged = _merge(base, "central_number", entry, page0, auto)
    return old if merged is base else [merged] + list(old[1:])
