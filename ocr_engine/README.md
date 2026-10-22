# LabExtract OCR engine 1.0 — handoff package

Self-contained, CPU-only extraction engine for Hebrew lab commitment forms. Input: a PDF or an image. Output: seven
fields per page, with evidence, and optionally one validated `shared.contracts.Form` per page. Nothing is approved
automatically. Every value goes to human review as `approval_status=PENDING`.

Hebrew summary and full details: `OCR_HANDOFF_HE.md` / `OCR_HANDOFF_EN.md`. Test results: `TEST_RESULTS.md`.

## Contents
| path | what |
|---|---|
| `run_extract.py` | **the only entry point** (CLI) |
| `labextract_ocr/` | engine code (`engine.Engine`, `to_contract.page_to_form`) |
| `vendor/paddleocr-hebrew/` | PaddleOCR-Hebrew inference code + the 2 ONNX models used (Apache-2.0, see LICENSE/NOTICE) |
| `data/` | closed institution list + aliases; Gemini privacy allowlist (dev sample hash only) |
| `samples/input/` | 4 synthetic PDFs (fictitious people; text-layer and scan variants) |
| `samples/expected_output/` | the engine's real output on them (`*.json` engine result, `*.forms.json` contract Forms) |
| `samples/truth.json` | the values the synthetic forms contain |
| `tests/` | `smoke_test.py`, `test_guards.py`, `test_gemini_htr.py` (no network, no patient data) |
| `requirements.txt` / `requirements-gemini.txt` | pinned runtime / optional Gemini dependencies |
| `ENV_VARS.example` | every environment variable (names and defaults only, no values) |

## Install (Linux, Debian/Ubuntu)
```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv \
     tesseract-ocr tesseract-ocr-heb tesseract-ocr-osd
cd labextract_ocr_engine
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
tesseract --list-langs        # must list: heb, osd (eng is also used)
```
Ubuntu 24.04 ships Python 3.12; get 3.11 from the deadsnakes PPA or use the `python:3.11-slim` base. 3.12 has
wheels for every pin but is untested. Tested on Windows 11 with Python 3.11.9 and Tesseract 5.5.3. Linux uses the
same pinned wheels
(`opencv-python-headless` replaces `opencv-python`). On a new host, run the smoke test first.

## Run
```bash
python run_extract.py samples/input/synth_00_L1_row_text.pdf --contract --out /tmp/labocr_out
#   -> /tmp/labocr_out/synth_00_L1_row_text.json        engine result
#   -> /tmp/labocr_out/synth_00_L1_row_text.forms.json  validated shared.contracts.Form list
python run_extract.py <file.pdf> --pages 1,3            # selected pages only
```
- `--contract` needs `shared.contracts`. It is importable when the package runs inside the backend repo. Otherwise
  set `LABOCR_CONTRACTS_REPO=<folder containing shared/contracts.py>`.
- The exit code is 0 even if a single page fails. That page is `processing_status=FAILED`, which maps to Form
  `FAILED` + Issue `TECHNICAL_FAILURE`, never MISSING.

Python use (e.g. in a worker):
```python
from labextract_ocr.engine import Engine
from labextract_ocr.to_contract import page_to_form
eng = Engine()                                   # load once per process (~1.2 s), reuse for all files
res = eng.extract_file(path, pages=None)         # dict: engine_version, source_file, network_calls, pages[]
forms = [page_to_form(file_id, p, res["engine_version"]) for p in res["pages"]]
```

## Tests
```bash
python tests/smoke_test.py      # 4/4 synthetic documents: values, footer guard, contract Forms PENDING
python tests/test_guards.py     # role / footer / validation guards
python tests/test_gemini_htr.py # Gemini gates, schema validation, key failover (mock only, no network)
```

## Output rules the backend must follow
- `status`: `proposed` = engine suggestion, still PENDING human approval; `uncertain` = candidates only; `missing`.
- Commitments: use `Examination.commitments[]`, one per row. The Issue `COMMITMENT_COUNT_UNCERTAIN` means the list
  is not confirmed complete.
- Evidence with `method="review_region"` is a box for the reviewer to look at, **never a value**.
- `method="gemini_htr"` is a Gemini candidate. It is never proposed for IDs or names.
- READY: the engine never approves anything. A commitment becomes READY only through the existing contract logic
  (`derive_commitment_status`), after all its fields are approved by a human. No automatic approval is implemented.

## Resources / deployment
- **Hardware:** CPU only. RAM ~0.4 GB peak; plan 1–2 GB per worker. Package ~28 MB plus a ~370 MB venv.
- **Time per page:** 22–40 s cold, 13–21 s with the OCR cache, ~6 s for a PDF with a text layer.
- **No network** by default.
- **Vercel / serverless: blocked.**
  - The Tesseract system binary is not available there.
  - onnxruntime + OpenCV + PyMuPDF + models exceed the function bundle limits.
  - 13–40 s of CPU per page is above the function time limits.
  - The OCR cache needs private persistent storage.
- **Recommended:** a separate worker process or a small container / VM that the backend calls (queue or HTTP).
  The backend chooses. A Dockerfile is not included: it is not needed on a host with apt + Python 3.11.
- `LABOCR_CACHE` holds page text (patient data). Keep it on private storage, with retention, or point it at a temp
  directory per job.

## Gemini (optional, OFF)
It is off unless **all** of these are set: `LABOCR_GEMINI=on`, `LABOCR_GEMINI_DRYRUN=off`,
`LABOCR_GEMINI_TRANSPORT=real`, `LABOCR_GEMINI_BILLING_CHECKED=yes`, `LABOCR_GEMINI_MAX_CALLS>0`, and a key.

Even then it sends only documents whose SHA-256 is in `data/gemini_dev_allowlist.json`, which today is only the
anonymized dev sample. Real patient documents are never sent. Measured value on dev was low (see `OCR_HANDOFF_EN.md`
§7). Keep it off in production.
