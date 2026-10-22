
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import current_workspace
from backend.readiness import apply_auto_approval
from backend.storage import Repository, VersionConflict, get_repository, validate_file_id
from extraction.engine_select import extract_document, extract_page
from shared.contracts import Form, Issue, IssueReason, ProcessingStatus, SplitStatus

router = APIRouter(prefix="/documents", tags=["Document Review"])

QUEUED = ProcessingStatus.QUEUED.value
FAILED = ProcessingStatus.FAILED.value
SAVE_ATTEMPTS = 5


def failed_form(file_id: str, page: int, note: str) -> Form:
    """A technical failure is FAILED with an issue; it is never shown as extracted, absent or unreadable data."""
    form_id = f"{file_id}-p{page + 1}"
    return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page}], split_status=SplitStatus.UNCERTAIN,
                processing_status=ProcessingStatus.FAILED,
                issues=[Issue(issue_id=f"{form_id}-failed", form_id=form_id, reason=IssueReason.TECHNICAL_FAILURE,
                              note=note)])


def failed_forms(file_id: str, page_count: int) -> list[Form]:
    return [failed_form(file_id, page, "Text extraction failed for this document; enter values from the source")
            for page in range(max(page_count, 1))]


def queued_form(file_id: str, page: int) -> Form:
    """A page that is not processed yet: no values, nothing to review or export."""
    return Form(form_id=f"{file_id}-p{page + 1}", file_id=file_id, page_regions=[{"page": page}],
                split_status=SplitStatus.UNCERTAIN, processing_status=ProcessingStatus.QUEUED)


def _document(repo: Repository, workspace_id: str, file_id: str) -> dict:
    validate_file_id(file_id)
    metadata = repo.get_document(workspace_id, file_id)
    if metadata is None:
        raise HTTPException(404, "Document not found")
    return metadata


def _original(repo: Repository, workspace_id: str, file_id: str) -> tuple[bytes, str]:
    original = repo.read_original(workspace_id, file_id)
    if original is None:
        raise HTTPException(404, "File not found")
    return original


def _extract_page(file_id: str, content: bytes, extension: str, page: int) -> dict:
    try:
        # only values the engine itself reports verified are approved automatically; the rest stay PENDING
        form = apply_auto_approval(extract_page(file_id, content, extension, page))
    except Exception:
        form = failed_form(file_id, page, "Text extraction failed for this page; enter values from the source")
    return form.model_dump(mode="json")


def _store_page(repo: Repository, workspace_id: str, file_id: str, page: int, form: dict, retry: bool):
    """Writes one page into the drafts without touching the others (re-read and retry on a concurrent write).
    A page another request already processed is kept: the first result wins, nothing is extracted twice."""
    for _ in range(SAVE_ATTEMPTS):
        forms, version = repo.get_drafts(workspace_id, file_id)
        current = forms[page]
        if not _processable(current, retry):
            return current, forms
        updated = list(forms)
        updated[page] = form
        try:
            repo.save_drafts(workspace_id, file_id, updated, version)
            return form, updated
        except VersionConflict:
            continue
    raise HTTPException(409, detail={"error": "DRAFTS_BUSY"})


def _processable(form: dict, retry: bool) -> bool:
    status = form.get("processing_status")
    return status == QUEUED or (retry and status == FAILED)


def _finish(repo: Repository, workspace_id: str, file_id: str, forms: list[dict]) -> int:
    remaining = sum(f.get("processing_status") == QUEUED for f in forms)
    repo.set_processing_status(workspace_id, file_id,
                               (ProcessingStatus.RUNNING if remaining else ProcessingStatus.DONE).value)
    return remaining


@router.post("/{file_id}/drafts/pages/{page}")
def process_page(
    file_id: str,
    page: int,
    retry: bool = Query(False, description="re-process a page that FAILED"),
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    """Extracts ONE page (1-based) and stores its form. Long documents are processed page by page, each request
    well inside the function time limit. Calling it again for a processed page returns the stored form."""
    metadata = _document(repo, workspace_id, file_id)
    count = metadata.get("page_count") or 1
    if not 1 <= page <= count:
        raise HTTPException(404, "Page not found")

    drafts = repo.get_drafts(workspace_id, file_id)
    if drafts is None:
        drafts = repo.init_drafts(workspace_id, file_id,
                                  [queued_form(file_id, p).model_dump(mode="json") for p in range(count)])
    forms, _ = drafts
    if len(forms) != count:
        raise HTTPException(409, detail={"error": "PAGE_LAYOUT_MISMATCH"})

    stored = forms[page - 1]
    if _processable(stored, retry):
        content, extension = _original(repo, workspace_id, file_id)
        stored, forms = _store_page(repo, workspace_id, file_id, page - 1,
                                    _extract_page(file_id, content, extension, page - 1), retry)
    remaining = _finish(repo, workspace_id, file_id, forms)
    return {"page": page, "page_count": count, "remaining": remaining, "form": stored}


@router.post("/{file_id}/drafts/init")
def initialize_drafts(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    metadata = _document(repo, workspace_id, file_id)

    existing = repo.get_drafts(workspace_id, file_id)
    if existing is not None:
        forms = existing[0]
        pending = [p for p, form in enumerate(forms) if form.get("processing_status") == QUEUED]
        if not pending:
            return forms
        # drafts started page by page: finish the remaining pages (same result as calling each page)
        content, extension = _original(repo, workspace_id, file_id)
        for page in pending:
            _, forms = _store_page(repo, workspace_id, file_id, page,
                                   _extract_page(file_id, content, extension, page), retry=False)
        _finish(repo, workspace_id, file_id, forms)
        return forms

    content, extension = _original(repo, workspace_id, file_id)
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
