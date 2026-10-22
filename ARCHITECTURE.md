# LabExtract AI — architecture (hackathon prototype, synthetic data only)

## Flow
Upload a PDF (digital or scanned) or an image → one form per page → extraction of the seven fields and one row per
commitment → server classification READY / NEEDS_REVIEW → human correction and approval → official seven-column Excel.

## Components
| Layer | What | Where |
|---|---|---|
| Review UI | React + Vite, V2 review workspace (`frontend/v2.html`) served at `/` | `frontend/` |
| API | FastAPI under `/api`: sign-in, upload, drafts, field save / approve, export | `backend/` |
| Extraction | `extraction/engine_select.py`: text-layer pages → `pdf_text` (rule-based, exact text); scanned pages and images → OCR engine | `extraction/` |
| OCR engine | lab-ocr-1.0 (frozen): Tesseract OSD + PaddleOCR-Hebrew ONNX, CPU only, no network; output mapped to the shared contract | `ocr_engine/` |
| Contract | One Pydantic contract shared by extraction, API and export (`Form`, `Examination`, `Commitment`, `FieldValue`) | `shared/contracts.py` |
| Storage + auth | Supabase: Postgres (`lx_*` tables), private Storage bucket for originals, Supabase Auth; server-side sessions in an HttpOnly cookie | `backend/supabase_repo.py`, `migrations/` |
| Runtime | One Docker image (UI build + API + OCR + Tesseract), run as a Vercel container function | `Dockerfile`, `vercel.docker.json` |

## Decisions that protect the data
- **Nothing is approved by a proposal.** Every extracted value is PENDING. `AUTO_APPROVED` exists only for values an
  engine reports independently verified (rule id + policy version); today's engines report none, so a person approves.
- **READY is decided by the server** (`backend/readiness.py`): all seven fields approved, no open issue, commitment
  existence confirmed. The export counts READY the same way and warns when filled rows are still not READY.
- **Unapproved cells are empty and orange** in the Excel; a partial file needs explicit confirmation before download.
- **A technical failure is FAILED, never "missing".** If OCR is unavailable, only the affected pages fail.
- **Privacy:** the OCR token cache is a private temporary folder emptied after every document; the Gemini stage is
  forced off; production refuses to start with local storage, local accounts or an insecure cookie.

## Known limits
- Scanned page OCR: about 15–60 s per page on CPU; uploads up to 4 MB (about 1–2 scanned pages at 200 dpi).
- Handwritten names / IDs / dates are not read reliably; they go to human review with the region to look at.
- Form split (one page = one form) is not confirmed automatically, so every export is marked partial.
