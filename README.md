---
title: LabExtract AI
emoji: 🧪
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# LabExtract AI

Extraction of laboratory commitment forms (digital and scanned PDF) into the official seven-column Excel file, with
human review. Every value the engine proposes stays PENDING until a person approves it; a row is READY only when all
seven fields are approved and no issue is open.

- `Dockerfile`: one container with the review UI (v2), the API under `/api` and the OCR engine lab-ocr-1.0
  (Tesseract + PaddleOCR-Hebrew ONNX, CPU only, no network).
- Configuration comes only from environment variables / host secrets: `LABEXTRACT_STORAGE=supabase`,
  `LABEXTRACT_AUTH=supabase`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`. In production the
  server refuses to start with local storage, local accounts or an insecure cookie.
- Synthetic data only in this demo.
