"""
LabExtract AI backend. Every route except /health and /auth/login requires a signed-in session;
the workspace of a request comes from that session.

Run from the project root:
    python -m uvicorn backend.app:app --reload
Then open http://127.0.0.1:8000/docs (disabled in production)
"""
from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response

from backend.auth import check_deployment_config, require_principal
from backend.auth_routes import router as auth_router
from backend.excel_export import ExportSummary, build_export
from backend.security import SecurityMiddleware
from extraction.mock_engine import make_mock_result
from shared.contracts import EngineMode, EngineResult
from backend.uploads import router as documents_router
from backend.drafts import router as drafts_router
from backend.review import router as review_router
from backend.fields import router as fields_router
from backend.saved_export import router as saved_export_router

check_deployment_config()

_production = bool(os.environ.get("VERCEL")) or os.environ.get("LABEXTRACT_ENV") == "production"
app = FastAPI(
    title="LabExtract AI",
    version="0.2",
    docs_url=None if _production else "/docs",
    redoc_url=None if _production else "/redoc",
    openapi_url=None if _production else "/openapi.json",
)
app.add_middleware(SecurityMiddleware)

protected = [Depends(require_principal)]
app.include_router(auth_router)
app.include_router(documents_router, dependencies=protected)
app.include_router(drafts_router, dependencies=protected)
app.include_router(review_router, dependencies=protected)
app.include_router(fields_router, dependencies=protected)
app.include_router(saved_export_router, dependencies=protected)

SCENARIOS = ("default", "count_unsure", "failed")


def _forms(scenarios: str):
    names = [x.strip() for x in scenarios.split(",") if x.strip()]
    bad = [n for n in names if n not in SCENARIOS]
    if bad:
        raise HTTPException(422, f"unknown scenario(s): {bad}; allowed: {list(SCENARIOS)}")
    return [make_mock_result(form_id=f"f{i + 1}", file_id=f"file{i + 1}", scenario=n).form for i, n in enumerate(names)]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/mock/result", response_model=EngineResult, dependencies=protected)
def mock_result(scenario: str = "default", mode: EngineMode = EngineMode.FULL):
    if scenario not in SCENARIOS:
        raise HTTPException(422, f"allowed: {list(SCENARIOS)}")
    return make_mock_result(scenario=scenario, mode=mode)


@app.get("/mock/export/summary", response_model=ExportSummary, dependencies=protected)
def export_summary(scenarios: str = "default"):
    """Shown to the user BEFORE a partial download (P1 2.2)."""
    return build_export(_forms(scenarios))[1]


@app.get("/mock/export.xlsx", dependencies=protected)
def export_xlsx(scenarios: str = "default", confirm_partial: bool = Query(False)):
    data, summary = build_export(_forms(scenarios))
    if summary.is_partial and not confirm_partial:
        # explicit confirmation required before a partial file is handed out
        raise HTTPException(409, detail={"needs_confirmation": True, "summary": summary.model_dump()})
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=labextract.xlsx; filename*=UTF-8''{quote(summary.filename)}"},
    )
