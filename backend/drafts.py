
from fastapi import APIRouter, Depends, HTTPException

from backend.auth import current_workspace
from backend.readiness import apply_auto_approval
from backend.storage import Repository, get_repository, validate_file_id
from extraction.engine_select import extract_document
from shared.contracts import Form, Issue, IssueReason, ProcessingStatus, SplitStatus

router = APIRouter(prefix="/documents", tags=["Document Review"])


def failed_forms(file_id: str, page_count: int) -> list[Form]:
    """A technical failure is FAILED with an issue; it is never shown as extracted, absent or unreadable data."""
    return [
        Form(
            form_id=f"{file_id}-p{page + 1}",
            file_id=file_id,
            page_regions=[{"page": page}],
            split_status=SplitStatus.UNCERTAIN,
            processing_status=ProcessingStatus.FAILED,
            issues=[Issue(issue_id=f"{file_id}-p{page + 1}-failed", form_id=f"{file_id}-p{page + 1}",
                          reason=IssueReason.TECHNICAL_FAILURE,
                          note="Text extraction failed for this document; enter values from the source")],
        )
        for page in range(max(page_count, 1))
    ]


@router.post("/{file_id}/drafts/init")
def initialize_drafts(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    validate_file_id(file_id)
    metadata = repo.get_document(workspace_id, file_id)

    if metadata is None:
        raise HTTPException(404, "Document not found")

    existing = repo.get_drafts(workspace_id, file_id)
    if existing is not None:
        return existing[0]

    original = repo.read_original(workspace_id, file_id)
    if original is None:
        raise HTTPException(404, "File not found")

    content, extension = original
    try:
        # only values the engine itself reports verified are approved automatically; the rest stay PENDING
        forms = [apply_auto_approval(form) for form in extract_document(file_id, content, extension)]
        status = ProcessingStatus.DONE
    except Exception:
        forms = failed_forms(file_id, metadata.get("page_count") or 1)
        status = ProcessingStatus.FAILED

    stored, _ = repo.init_drafts(workspace_id, file_id, [form.model_dump(mode="json") for form in forms])
    repo.set_processing_status(workspace_id, file_id, status.value)
    return stored


@router.get("/{file_id}/drafts")
def get_drafts(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    validate_file_id(file_id)
    drafts = repo.get_drafts(workspace_id, file_id)

    if drafts is None:
        raise HTTPException(404, "Drafts not initialized")

    return drafts[0]
