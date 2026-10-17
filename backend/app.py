"""
Minimal backend skeleton (no DB, no uploads yet): serves the MOCK engine result and the official Excel.

Run from the project root:
    python -m uvicorn backend.app:app --reload
Then open http://127.0.0.1:8000/docs
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from backend.excel_export import ExportSummary, build_export
from extraction.mock_engine import make_mock_result
from shared.contracts import EngineMode, EngineResult

app = FastAPI(title="LabExtract AI (skeleton)", version="0.1")

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


@app.get("/mock/result", response_model=EngineResult)
def mock_result(scenario: str = "default", mode: EngineMode = EngineMode.FULL):
    if scenario not in SCENARIOS:
        raise HTTPException(422, f"allowed: {list(SCENARIOS)}")
    return make_mock_result(scenario=scenario, mode=mode)


@app.get("/mock/export/summary", response_model=ExportSummary)
def export_summary(scenarios: str = "default"):
    """Shown to the user BEFORE a partial download (P1 2.2)."""
    return build_export(_forms(scenarios))[1]


@app.get("/mock/export.xlsx")
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
