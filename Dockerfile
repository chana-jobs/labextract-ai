# LabExtract AI in one container: frontend build (v2 review UI) + API + OCR engine lab-ocr-1.0 with Tesseract.
#   docker build -t labextract .
# ocr_engine/ is the frozen handoff package (runtime parts only; see ocr_engine/MANIFEST.sha256).
# Runtime configuration (Supabase, cookies) comes from environment variables / host secrets, never from the image.

FROM node:20-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN VITE_API_BASE_URL=/api VITE_USE_MOCK=false npm run build

FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-heb tesseract-ocr-osd \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
COPY ocr_engine/requirements.txt /tmp/ocr-requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/ocr-requirements.txt uvicorn==0.54.0 \
    google-genai==2.30.1 truststore==0.10.5 python-dotenv==1.2.4

COPY ocr_engine/labextract_ocr /app/ocr_engine/labextract_ocr
COPY ocr_engine/vendor /app/ocr_engine/vendor
COPY ocr_engine/data /app/ocr_engine/data
COPY shared /app/shared
COPY extraction /app/extraction
COPY backend /app/backend
COPY labx /app/labx
COPY --from=frontend /src/frontend/dist /app/public

RUN useradd --create-home --uid 1000 app
USER app
ENV PYTHONUNBUFFERED=1 \
    LABEXTRACT_ENV=production \
    LABEXTRACT_ENGINE=hybrid \
    LABEXTRACT_OCR_DIR=/app/ocr_engine \
    LABOCR_TESSERACT=/usr/bin/tesseract \
    LABEXTRACT_LABX=on \
    LABX_DIR=/app \
    LABX_TESSERACT=/usr/bin/tesseract \
    LABX_CACHE_DIR=/tmp/labx_cache \
    LABX_EXTRA_ENV_FILE=/nonexistent \
    LABX_GEMINI_TIMEOUT_MS=30000 \
    LABX_SLOT_BUDGET_S=45 \
    LABX_PAGE_BUDGET_S=150 \
    PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.container:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
