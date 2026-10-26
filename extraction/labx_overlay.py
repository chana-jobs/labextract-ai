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
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from shared.contracts import (
    Attribution, Candidate, Dimensions, Evidence, ExtractionStatus, FieldValue, Form, ProcessingStatus, Readability,
    UncertaintyReason, Verification,
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
    # ID / date not accepted: show what each model read, for the reviewer
    cands = [Candidate(raw=c, role_basis=f"labx reading (not accepted: {entry['reason']})"[:500],
                       evidence=[_evidence(page0, c)]) for c in entry["candidates"]]
    reasons = list(old.uncertainty_reason) or [UncertaintyReason.RECOGNITION]
    status = old.extraction_status if old.extraction_status != ExtractionStatus.MISSING else ExtractionStatus.UNCERTAIN
    return old.model_copy(update={"extraction_status": status, "uncertainty_reason": reasons,
                                  "candidates": list(old.candidates) + cands})


# one labx page at a time per server: it already runs next to the OCR, and two of each exhausted the memory
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("LABX_WORKERS", "1")), thread_name_prefix="labx")


def _report(page0: int, seconds: float, res: dict | None, error: str | None = None) -> None:
    """One log line per page: timing, field statuses and reader errors. Never field values (patient data)."""
    if res is None:
        print(f"[labx] page={page0 + 1} FAILED after {seconds:.1f}s: {error}", flush=True)
        return
    statuses = {f: e["status"] for f, e in res["fields"].items()}
    reads = res.get("log", {}).get("page_reads", {})
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
    """Start the labx reading in the background (it runs while the local OCR reads the same page)."""
    return _pool.submit(_run, content, extension, page0) if enabled() else None


def apply(content: bytes, extension: str, page0: int, form: Form, pending: Future | None = None) -> Form:
    """Merge the labx result into the page form. `pending` = a reading already started with start()."""
    if not enabled() or form.processing_status != ProcessingStatus.DONE or not form.examinations:
        return form
    exam = form.examinations[0]
    if pending is None and all(getattr(exam, f).extraction_status == ExtractionStatus.COMPLETE
                               for f in FIELDS.values()):
        return form                       # text-layer page already read all four exactly: save model quota
    res = pending.result() if pending is not None else _run(content, extension, page0)
    if res is None:
        return form
    auto = os.environ.get("LABX_AUTO_APPROVE") == "1"
    form = form.model_copy(deep=True)
    exam = form.examinations[0]
    for field, name in FIELDS.items():
        setattr(exam, name, _merge(getattr(exam, name), field, res["fields"][field], page0, auto))
    return Form.model_validate(form.model_dump())
