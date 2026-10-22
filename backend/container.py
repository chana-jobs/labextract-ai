"""Single-container entry (Dockerfile): the API under /api, as on Vercel, and the frontend's static build at /, on
the same origin so the session cookie and the Origin check work unchanged. Same API app as backend.asgi.

The review UI is v2 (v2.html): it is served for / and for /index.html, so the legacy page, whose readiness check
ignores open issues, is never shown."""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app import app as api

PUBLIC = Path(os.environ.get("LABEXTRACT_PUBLIC_DIR") or Path(__file__).resolve().parents[1] / "public")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/api", api)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def review_ui():
    return FileResponse(PUBLIC / "v2.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=PUBLIC), name="frontend")
