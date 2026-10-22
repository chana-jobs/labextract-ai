# LabExtract OCR engine — handoff (EN)

Version **`lab-ocr-1.0` (frozen 22.10.2026)**. Lab: `C:\accthon_chani\ocr_engine_lab`.

**Handoff package:** `C:\accthon_chani\ocr_engine_lab\handoff\labextract_ocr_engine\`. It is self-contained (~28 MB):
code, the vendored PaddleOCR-Hebrew code and 2 ONNX models, data, pinned `requirements.txt`, `ENV_VARS.example`,
synthetic samples with their real output, tests, `README.md` (Linux install/run) and `TEST_RESULTS.md`. The package
is rebuilt by `tools/build_handoff.py`.

**Status:**
- **Local engine:** READY FOR HANDOFF for extraction + human review. Automatic approval is NOT implemented (§6b).
- **Gemini fallback reader:** built, tested with mocks and measured with real calls on the dev sample (§7). The
  value is low, so it is **OFF by default**. Keep it off in production.
- **Not changed:** backend, frontend, API, DB and the contracts. Nothing was committed or deployed.
- **After this handoff the OCR work is frozen.**

## 0. Quick start (Linux)
```bash
sudo apt-get install -y tesseract-ocr tesseract-ocr-heb tesseract-ocr-osd     # + Python 3.11
cd labextract_ocr_engine && python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tests/smoke_test.py                                                     # expect 4/4
python run_extract.py samples/input/synth_00_L1_row_text.pdf --contract --out /tmp/labocr_out
```
Compare the output with `samples/expected_output/`. Inside the backend repo `shared.contracts` is importable as-is;
elsewhere set `LABOCR_CONTRACTS_REPO`.

## 1. Entry points
- **CLI (the one entry point):**

  ```
  python run_extract.py <file.pdf|image> [--pages 1,3] [--out DIR] [--overlay] [--contract]
  ```

  - Output `DIR/<stem>.json` is the engine result.
  - With `--contract`, `DIR/<stem>.forms.json` holds one validated `shared.contracts.Form` per page.
- **Python:**
  - `labextract_ocr.engine.Engine().extract_file(path, pages)` returns a dict.
  - `labextract_ocr.to_contract.page_to_form(file_id, page, engine_version)` returns a `Form`.
- **Seven fields per page, always present:** `first_name`, `last_name`, `israeli_id`, `received_date`, `sender`,
  `central_numbers`, `commitment_numbers`.
  - Each field has `status` (proposed | uncertain | missing), candidates with evidence and boxes (oriented px,
    source px, PDF points), and review `regions`.
- **Never auto-approved:** every value is `approval_status=PENDING`.
- **A failed page** is `processing_status=FAILED` (Form FAILED + Issue TECHNICAL_FAILURE), never MISSING. The other
  pages continue.

## 2. Must-read rules for the backend
- **Commitments:** consume `Examination.commitments[]`, one record per row, from the engine's `items`. Do **not**
  rely on list-level `values`: a partial list has `status=uncertain` and empty `values` (p25: 2 of 3 rows proposed).
  - `existence_status` (is there a row?), `number.extraction_status` and `approval_status` are separate states.
  - The form issue `COMMITMENT_COUNT_UNCERTAIN` means the list is not confirmed complete.
- **Review regions** (`Evidence.method="review_region"`) are places to show the reviewer, **never values**.
- **Gemini candidates** carry `Evidence.method="gemini_htr"`. Patient IDs and names from Gemini are review-only,
  always.

## 3. Pipeline
1. Render the PDF at 200 dpi (300 dpi on demand for crops / stamps).
2. Tesseract OSD orientation, then CLAHE.
3. PaddleOCR-Hebrew ONNX (word-det + mobile-word) gives tokens, cached by image hash. Print-footer lines are masked
   before any cache write.
4. zxing-cpp barcodes.
5. Field assignment: templates, labels, spatial rules, role guards (physician / father / issuer / print footer),
   the closed institution list (`data/Institutions.xlsx`, 855 rows), the stamp reader, a second engine (Tesseract)
   for number confirmation, and per-row commitment items.
6. The handwriting-region locator (`hwregions.py`), plus a quality gate per crop: written content, not another
   field's value, not truncated.
7. **Optional:** the Gemini fallback reader (`gemini_htr.py`), only for located but unresolved fields. Never a page;
   a fixed prompt per (field × crop context); no local reading is put in the prompt.
   - Structured output (`response_schema`) and strict local JSON validation.
   - Candidates only. Promotion on two-engine agreement exists behind `LABOCR_GEMINI_PROMOTE`, off; it is never
     applied to IDs or names, and dates need a stamp read.
   - Any Gemini failure keeps the local results (snapshot restore).

## 4. Configuration (environment)

| variable | default | meaning |
|---|---|---|
| `LABOCR_TESSERACT` | `tesseract` on PATH, else `C:\Program Files\Tesseract-OCR\tesseract.exe` | Tesseract 5 binary (needs `osd`, `heb`, `eng`) |
| `LABOCR_PADDLE_REPO` | `<package>/vendor/paddleocr-hebrew` (dev: `<repo root>/python/paddleocr-hebrew`) | Paddle code + ONNX models (`models/word-det`, `models/mobile-word`, `charset_v2f.txt`) |
| `LABOCR_CACHE` | `<package>/out/cache` | OCR token cache. **It holds page text: treat as patient data.** |
| `LABOCR_CONTRACTS_REPO` | `<repo root>/labextract_contracts` (only if `shared.contracts` is not importable) | where `shared/contracts.py` is |
| `LABOCR_CONFIRM` | `engines` | confirmation policy for unstable number reads: `engines` (two engines agree, approved 21.10) or `strict`; leave the default |
| `LABOCR_PATIENT_PROBE` | off | Tesseract patient-label probes (+2 name pages for ~+4.6 s/page; off) |
| `LABOCR_GEMINI` | off | Gemini component |
| `LABOCR_GEMINI_DRYRUN` | on | build the plan, send nothing |
| `LABOCR_GEMINI_TRANSPORT` | (unset) | `real` creates the network transport |
| `LABOCR_GEMINI_MAX_CALLS` | 0 | hard cap of real attempts per run (retries and backup included) |
| `LABOCR_GEMINI_BILLING_CHECKED` | no | `yes` only after the account owner confirms no billing account is linked |
| `LABOCR_GEMINI_MODEL` / `_BACKUP_MODEL` | `gemini-3.1-flash-lite` / `gemini-3.5-flash-lite` | backup only after a 503; verified with a real call on 22.10 (gemini-2.5-flash returns 404 - never use) |
| `LABOCR_GEMINI_PROMOTE` | off | agreement-based promotion (project manager's decision) |
| `GEMINI_API_KEY` or `LABOCR_GEMINI_ENV_FILE` | — | key; never logged or written |

**Privacy gate:** a real Gemini send happens only for documents whose SHA-256 is in
`data/gemini_dev_allowlist.json`; today that is only the anonymized dev sample (part 1). Real patient documents need
a separate privacy approval.

## 5. Dependencies and resources (measured on the dev machine, Windows, CPU only)
- **Python 3.11.9.**
  - `onnxruntime 1.31.0`, `opencv-python 5.0.0.93`, `numpy 2.4.6`, `pymupdf 1.28.2`, `zxing-cpp 3.1.1`;
  - contract conversion: `pydantic 2.14.1`;
  - optional Gemini: `google-genai 2.30.1`, `truststore 0.10.5`, `python-dotenv 1.2.4`.
  - Environment size: ~370 MB.
- **Tesseract 5.5.3** with `osd`, `heb`, `eng`.
- **Models:** Paddle `word-det` 4.7 MB + `mobile-word` 7.4 MB (Apache-2.0).
- **RAM:** ~0.11 GB after model load; peak ~0.4 GB while processing (warm cache). Plan 1–2 GB per worker.
- **Time per page** (this machine, load varied run to run):

  | stage | time |
  |---|---|
  | engine start | 1.2 s |
  | cold page (render + OCR + assignment) | 22–40 s |
  | warm page (OCR cached) | 13–21 s |
  | Gemini stage, mock (crops + plan) | 0.1–0.4 s |
  | one real Gemini call | ~4 s (flash-lite); up to 68 s (3.5-flash) |

  | text-layer PDF page (no OCR needed) | ~6 s |

- **Linux:** `apt-get install tesseract-ocr tesseract-ocr-heb tesseract-ocr-osd`; `pip install -r requirements.txt`
  (same pins; `opencv-python-headless` instead of `opencv-python`). Linux was not run on this machine: run
  `tests/smoke_test.py` first on the target host.
- **Network:** none by default.
- **Deployment — Vercel / serverless is BLOCKED:**
  1. the Tesseract system binary (apt) is not available in a serverless function;
  2. onnxruntime + OpenCV + PyMuPDF + models exceed the function bundle size limits;
  3. 13–40 s CPU per page is above the function duration limits;
  4. the OCR cache needs private persistent storage.

  Use a separate worker process or a small VM / container that the Vercel backend calls (queue or HTTP). **The
  backend decides; no cloud service was chosen.** No Dockerfile is included: one is not required on a host with apt
  + Python 3.11.

## 6. Measured quality (26 dev pages, part 1; the 22 test pages were never run)
- **Proposals:** 58/58 correct, 0 wrong; 0 proposals on 36 documented counter-cases.
- **Fields correct:** 65/182 (incl. correct-empty).
- **Readable coverage:** 65/128 = 51%. Names are anonymized scribbles and are excluded
  (`eval/readability_categories.json`).

| field | proposed correct / wrong | notes |
|---|---|---|
| sender | 23 / 0 | closed list |
| central numbers | 17 pages (21/21 numbers; recall 21/30) / 0 | handwritten ones: Gemini region + candidate |
| commitment numbers | 18/18 numbers (recall 18/21); 16 complete lists + 1 partial / 0 | per-row records |
| received date | 2 / 0 | stamps faint; handwritten dates via Gemini (candidate) |
| ID, first / last name | 0 / 0 on dev (handwritten placeholders / scribbles) | READABLE values (synthetic, 15 forms): text-layer PDFs 6/6 names (field-specific labels), 15/15 IDs; scans 5/6 names, 12/15 IDs; 0 wrong (`eval/patient_fields_synth.md`) |

- **Located regions:**
  - handwritten central number + date: 7/8 and 6/7, 0 false regions;
  - patient ID crops: 13/26; names: 10/26.
- **Gemini, earlier measurements:**
  - central numbers 7/8 correct;
  - IDs 8/8 (one placeholder pattern);
  - dates: 2 confident wrong reads in 2 experiments, so dates are never taken from Gemini alone;
  - names unreadable / partial on the anonymized scribbles;
  - the live trial ignored the JSON envelope, now enforced by `response_schema`.

## 6b. Automatic verification / approval: NOT implemented (product decision of 22.10)
- **Target (product):** full automatic verification of commitments under a PROVEN policy, names and IDs included.
- **This version:**
  - nothing is ever approved automatically; every value is `approval_status=PENDING`;
  - agreement-based promotion (`LABOCR_GEMINI_PROMOTE`) only creates *proposals*, never approvals, and it is off;
  - IDs and names are never promoted.
- **Still to build, after the Gemini measurement and the project manager's approval:**
  1. a per-field policy with measured precision (rule id + policy version, as the contract requires for
     `AUTO_APPROVED`);
  2. the evidence each rule needs:
     - IDs: two independent readers + the patient role;
     - names: a reading on real, non-anonymized names (the dev set cannot measure this);
     - dates: a stamp read;
  3. measurement on data where the truth is readable (not the placeholders / scribbles of the dev set);
  4. the `AUTO_APPROVED` mapping in the adapter.
- **Do not present the verification mechanism as finished.**
- **READY under the existing policy:** the engine emits `PENDING` only. A commitment reaches READY through the
  contract's own `derive_commitment_status` once a human approves its fields and no open issue remains. That logic
  is unchanged in the contracts; the engine never sets READY.

## 7. Open items / limits
1. **Gemini measurement on dev (run 22.10, `eval/gemini_measurement.md`):** 10/10 attempts, 1 valid response.
   - The primary model returned 503 on 4 of 6 attempts; the backup returned 404 (4/4).
   - The one reading was a wrong stamp date (2023 for 2026), kept as a candidate only.
   - Quality is not established. **Gemini stays OFF by default**; the local engine is the production path.
   - The backup model is now `gemini-3.5-flash-lite`, verified with a real call.
   - Key failover (`GEMINI_API_KEY`, `_2`, `_3`) switches only on 503 / 504 / timeout / invalid key, **never on
     429**.
   - Later experiments on the dev sample (`eval/gemini_fullpage_patient.md`, `eval/gemini_names_*.md`):
     - full-page IDs 24/26;
     - **names were invented** with high confidence (e.g. p23, p15, conf 0.95–0.98);
     - a strict prompt abstained.

     That is why Gemini names / IDs are never proposed.
   - Mishkefet (local Hebrew HTR) was tested: 0/6 whole names. Not integrated; its licence is CC-BY-NC-SA.
2. **Handwritten fields:** without Gemini, names, IDs and handwritten dates cannot be read locally; they go to
   human review with their regions.
3. **Not built:** whole-page "locate" mode (needs a privacy approval and a measured trial).
4. **Truth questions:** the dev truth has open questions (p03 / p13 / p12 / p15 received dates). Nothing was changed.

## 8. Acceptance tests
**In the handoff package** (no dev data needed): `python tests/smoke_test.py`, `tests/test_guards.py`,
`tests/test_gemini_htr.py`. Final results: `TEST_RESULTS.md`.

**In the lab** (OCR venv, dev sample part 1 only):

```
python tests/test_guards.py
python tests/test_items.py
python tests/test_gemini_htr.py
python tests/test_gemini_engine.py
python tests/test_gemini_contract.py
python tests/test_engine_failures.py
python eval/evaluate.py dev --name accept --no-overlay      # expect 58/58 correct, 0 counter-case violations
python tools/check_adapter.py accept                        # expect 26/26 Forms valid
python run_extract.py "<part 1 PDF>" --pages 10 --contract  # one page end to end
```

## 9. Needed from the backend team (0 ₪ budget)
1. Copy `handoff/labextract_ocr_engine/` to the worker host. Install it per `README.md` and run
   `tests/smoke_test.py`.
2. Decide where the worker runs: a local machine or a free small host, **not a Vercel function** (§5). It needs CPU,
   Tesseract and ~1–2 GB RAM.
3. In the worker:
   - load `Engine()` once;
   - per uploaded file call `extract_file(path)`, then `page_to_form(file_id, page, engine_version)` for each page;
   - store the Forms through the existing API, unchanged contract.
4. Set `LABOCR_CACHE` to private storage (or a per-job temp dir). Leave every `LABOCR_GEMINI*` variable unset.
- UI work:
  - show per-row commitments and review regions;
  - show Gemini candidates as candidates, PENDING;
  - check how a CONFIRMED commitment whose number is CONFLICT is displayed.
- Keep `LABOCR_CACHE` on private storage, with a retention policy, because it holds patient text.
- Gemini in production only after: the measurement, the project manager's decision, and a privacy approval for real
  data.
