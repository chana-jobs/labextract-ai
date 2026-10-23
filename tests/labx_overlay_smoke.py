"""Smoke test of the labx overlay on one page of a sample PDF (uses the labx response cache when present).

python tests/labx_overlay_smoke.py <pdf> <1-based page>
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("LABEXTRACT_LABX", "on")

from extraction.engine_select import extract_page

pdf, page = sys.argv[1], int(sys.argv[2])
form = extract_page("smoke", Path(pdf).read_bytes(), ".pdf", page - 1)
print("processing:", form.processing_status.value)
if not form.examinations:
    print("issues:", [(i.reason.value, i.note) for i in form.issues])
    sys.exit(1)
exam = form.examinations[0]
for name in ("first_name", "last_name", "israeli_id", "sample_received_date", "sender"):
    fv = getattr(exam, name)
    cands = [c.raw for c in fv.candidates][:4]
    print(f"{name:22s} {fv.extraction_status.value:10s} {fv.approval_status.value:8s} {fv.normalized!s:12s} {cands}")
for c in exam.commitments:
    print(f"{'commitment':22s} {c.number.extraction_status.value:10s} existence={c.existence_status.value} {c.number.normalized}")
for fv in exam.central_numbers:
    print(f"{'central_number':22s} {fv.extraction_status.value:10s} {fv.normalized}")
print("issues:", [i.reason.value for i in form.issues])
