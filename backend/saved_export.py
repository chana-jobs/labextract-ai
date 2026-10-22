
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from backend.auth import current_workspace
from backend.excel_export import build_export, ExportSummary
from backend.storage import Repository, get_repository, validate_file_id
from shared.contracts import Form, SplitStatus

router = APIRouter(
    prefix="/documents",
    tags=["Excel Export"],
)


def prepare_export(file_id: str, repo: Repository, workspace_id: str):
    validate_file_id(file_id)

    drafts = repo.get_drafts(workspace_id, file_id)

    if drafts is None:
        raise HTTPException(404, "Document drafts not found")

    forms = [Form.model_validate(item) for item in drafts[0]]

    excel_bytes, summary = build_export(forms)

    if summary.rows == 0:
        summary.is_partial = True
        summary.warnings.append(
            "No commitment rows were created. "
            "Review the commitment count before export."
        )

    if any(
        form.split_status == SplitStatus.UNCERTAIN
        for form in forms
    ):
        summary.is_partial = True
        summary.warnings.append(
            "Form splitting has not been confirmed."
        )

    return excel_bytes, summary


@router.get(
    "/{file_id}/export/summary",
    response_model=ExportSummary,
)
def saved_export_summary(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    _, summary = prepare_export(file_id, repo, workspace_id)
    return summary


@router.get("/{file_id}/export.xlsx")
def saved_export_xlsx(
    file_id: str,
    confirm_partial: bool = Query(False),
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    excel_bytes, summary = prepare_export(file_id, repo, workspace_id)

    if summary.rows == 0:
        raise HTTPException(
            409,
            detail="No commitment rows available for export",
        )

    if summary.is_partial and not confirm_partial:
        raise HTTPException(
            409,
            detail={
                "needs_confirmation": True,
                "summary": summary.model_dump(),
            },
        )

    return Response(
        content=excel_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                "attachment; filename=labextract.xlsx; "
                f"filename*=UTF-8''{quote(summary.filename)}"
            ),
            "Cache-Control": "private, no-store",
        },
    )
