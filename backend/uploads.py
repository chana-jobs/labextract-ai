
import hashlib
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from PIL import Image
from pydantic import BaseModel, Field

from backend.auth import current_workspace
from backend.storage import CONTENT_TYPES, Repository, content_type_for, get_repository, validate_file_id

router = APIRouter(prefix="/documents", tags=["Documents"])

# Demo limit (synthetic documents only): a Vercel function accepts request bodies of about 4.5 MB.
MAX_UPLOAD_MB = 4
MAX_FILE_SIZE = MAX_UPLOAD_MB * 1024 * 1024

# Direct upload (browser -> Storage) is not bound by the function body limit; the bucket enforces the same
# limit (migrations/002_direct_upload_size.sql).
MAX_DIRECT_UPLOAD_MB = int(os.environ.get("LABEXTRACT_MAX_DIRECT_UPLOAD_MB", "20"))
MAX_DIRECT_FILE_SIZE = MAX_DIRECT_UPLOAD_MB * 1024 * 1024

ALLOWED_EXTENSIONS = set(CONTENT_TYPES)

# The format found by reading the content, and the extensions it may carry.
VERIFIED_EXTENSIONS = {"PDF": {".pdf"}, "PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}}


def _filename_and_extension(raw_name: str | None) -> tuple[str, str]:
    filename = Path(raw_name or "").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Only PDF, JPG and PNG files are supported")
    return filename, extension


def _inspect(content: bytes, extension: str) -> int:
    """Page count of a verified PDF / PNG / JPEG whose content matches its extension; 422 otherwise."""
    if not content:
        raise HTTPException(422, "Empty file")
    try:
        if extension == ".pdf":
            with pymupdf.open(stream=content, filetype="pdf") as document:
                # PyMuPDF opens images too, whatever filetype says: require a real PDF
                if not document.is_pdf or b"%PDF-" not in content[:1024]:
                    raise ValueError("Not a PDF")
                page_count = len(document)
            detected = "PDF"
        else:
            with Image.open(BytesIO(content)) as image:
                detected = image.format
                if detected not in ("PNG", "JPEG"):
                    raise ValueError("Invalid image format")
                image.verify()
            page_count = 1

        if page_count < 1:
            raise ValueError("Document has no pages")

    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Cannot read the uploaded document",
        )

    if extension not in VERIFIED_EXTENSIONS[detected]:
        # e.g. a PNG named .jpg: stored and served with the wrong type otherwise
        raise HTTPException(422, "File content does not match its extension")
    return page_count


def _metadata(file_id: str, filename: str, page_count: int) -> dict:
    return {
        "file_id": file_id,
        "filename": filename,
        "page_count": page_count,
        "processing_status": "QUEUED",
        "split_status": "UNCERTAIN",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    filename, extension = _filename_and_extension(file.filename)

    content = bytearray()

    try:
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)

            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds {MAX_UPLOAD_MB} MB (demo limit; synthetic documents only)",
                )
    finally:
        await file.close()

    page_count = _inspect(bytes(content), extension)
    metadata = _metadata(uuid4().hex, filename, page_count)

    repo.create_document(
        workspace_id,
        metadata,
        bytes(content),
        extension=extension,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type=content_type_for(extension),
    )

    return metadata


# ------------------------------------------------------------------ direct upload (large files)
class DirectUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


class DirectUploadComplete(BaseModel):
    filename: str = Field(min_length=1, max_length=255)


@router.post("/uploads")
def start_direct_upload(
    data: DirectUploadRequest,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    """Step 1: a one-object upload target in this workspace. The client never chooses the storage path."""
    _, extension = _filename_and_extension(data.filename)
    if data.size > MAX_DIRECT_FILE_SIZE:
        raise HTTPException(413, f"File exceeds {MAX_DIRECT_UPLOAD_MB} MB (demo limit; synthetic documents only)")
    file_id = uuid4().hex
    target = repo.create_upload_target(workspace_id, file_id, extension, content_type_for(extension))
    return {"file_id": file_id, "upload": target, "max_bytes": MAX_DIRECT_FILE_SIZE}


@router.put("/uploads/{file_id}/content", status_code=204)
async def put_direct_upload_content(
    file_id: str,
    request: Request,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    """Step 2, local development only: the cloud target is a signed Storage URL, not this route."""
    validate_file_id(file_id)
    store = getattr(repo, "store_staged", None)
    if store is None:
        raise HTTPException(404, "Not found")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_DIRECT_FILE_SIZE:
            raise HTTPException(413, f"File exceeds {MAX_DIRECT_UPLOAD_MB} MB")
    try:
        store(workspace_id, file_id, bytes(content))
    except KeyError:
        raise HTTPException(404, "Upload not found") from None
    return Response(status_code=204)


@router.post("/uploads/{file_id}/complete")
def complete_direct_upload(
    file_id: str,
    data: DirectUploadComplete,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    """Step 3: the stored bytes are verified exactly like /documents/upload, then the document is registered.
    Same response as /documents/upload."""
    validate_file_id(file_id)
    filename, extension = _filename_and_extension(data.filename)
    content = repo.read_staged(workspace_id, file_id, extension)
    if content is None:
        raise HTTPException(404, "Upload not found")
    try:
        if len(content) > MAX_DIRECT_FILE_SIZE:
            raise HTTPException(413, f"File exceeds {MAX_DIRECT_UPLOAD_MB} MB")
        page_count = _inspect(content, extension)
    except HTTPException:
        repo.discard_staged(workspace_id, file_id, extension)     # an unverified file is never kept
        raise
    metadata = _metadata(file_id, filename, page_count)
    repo.register_document(
        workspace_id, metadata, extension=extension, sha256=hashlib.sha256(content).hexdigest(),
        content_type=content_type_for(extension), size_bytes=len(content),
    )
    return metadata


@router.get("")
def list_documents(
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    """Temporary list for the 22.10 demo (newest first); not the Batch model of Spec v3.2."""
    return repo.list_documents(workspace_id)


@router.get("/{file_id}")
def get_document(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    validate_file_id(file_id)
    metadata = repo.get_document(workspace_id, file_id)

    if metadata is None:
        raise HTTPException(404, "Document not found")

    return metadata


@router.get("/{file_id}/file")
def get_document_file(
    file_id: str,
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    validate_file_id(file_id)

    if repo.get_document(workspace_id, file_id) is None:
        raise HTTPException(404, "Document not found")

    # an original larger than a function response is served by a short-lived signed Storage link
    link = repo.original_link(workspace_id, file_id, MAX_FILE_SIZE)
    if link is not None:
        return RedirectResponse(link, status_code=307, headers={"Cache-Control": "private, no-store"})

    original = repo.read_original(workspace_id, file_id)

    if original is None:
        raise HTTPException(404, "File not found")

    content, extension = original
    return Response(
        content=content,
        media_type=CONTENT_TYPES.get(extension, "application/octet-stream"),
        headers={
            # shown in the viewer; the name is the opaque id, never the uploaded file name
            "Content-Disposition": f'inline; filename="{file_id}{extension}"',
            "Cache-Control": "private, no-store",
        },
    )
