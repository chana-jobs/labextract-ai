
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

from backend.auth import current_workspace
from backend.storage import CONTENT_TYPES, Repository, content_type_for, get_repository, validate_file_id

router = APIRouter(prefix="/documents", tags=["Documents"])

# Demo limit (synthetic documents only): a Vercel function accepts request bodies of about 4.5 MB.
MAX_UPLOAD_MB = 4
MAX_FILE_SIZE = MAX_UPLOAD_MB * 1024 * 1024

ALLOWED_EXTENSIONS = set(CONTENT_TYPES)

# The format found by reading the content, and the extensions it may carry.
VERIFIED_EXTENSIONS = {"PDF": {".pdf"}, "PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}}


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    repo: Repository = Depends(get_repository),
    workspace_id: str = Depends(current_workspace),
):
    filename = Path(file.filename or "").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Only PDF, JPG and PNG files are supported",
        )

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

    if not content:
        raise HTTPException(422, "Empty file")

    try:
        if extension == ".pdf":
            with pymupdf.open(
                stream=bytes(content),
                filetype="pdf",
            ) as document:
                # PyMuPDF opens images too, whatever filetype says: require a real PDF
                if not document.is_pdf or b"%PDF-" not in bytes(content[:1024]):
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

    file_id = uuid4().hex

    metadata = {
        "file_id": file_id,
        "filename": filename,
        "page_count": page_count,
        "processing_status": "QUEUED",
        "split_status": "UNCERTAIN",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    repo.create_document(
        workspace_id,
        metadata,
        bytes(content),
        extension=extension,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type=content_type_for(extension),
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
