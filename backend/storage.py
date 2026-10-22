"""Storage for documents, original files, drafts, sessions and workspace membership.

Routes reach storage only through a Repository. File ids from request paths are validated before
use, and every read is scoped to a workspace: a record of another workspace is "not found".
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

FILE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

# Owner of LOCAL files written before workspaces existed. Never used for requests: the workspace
# of a request comes from its verified session (backend.auth.current_workspace).
DEFAULT_WORKSPACE = "default"

ROOT = Path(__file__).resolve().parents[1]


# The only formats stored, keyed by extension. A stored original's content type always comes from
# here (after the upload route has verified the content), never from the client. The private
# bucket accepts exactly these types (migrations/001_labextract_init.sql: allowed_mime_types).
CONTENT_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def content_type_for(extension: str) -> str:
    try:
        return CONTENT_TYPES[extension]
    except KeyError:
        raise ValueError(f"unsupported file type: {extension!r}") from None


def check_content_type(extension: str, content_type: str) -> None:
    """Refuse before any write when the content type is not the one of the verified format."""
    if content_type_for(extension) != content_type:
        raise ValueError(f"content type {content_type!r} does not match {extension!r}")


def validate_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not FILE_ID_PATTERN.fullmatch(file_id):
        raise HTTPException(400, "Invalid file ID")
    return file_id


class VersionConflict(Exception):
    """The drafts changed since they were read; nothing was written."""

    def __init__(self, server_version: int):
        super().__init__(f"server version is {server_version}")
        self.server_version = server_version


class StorageError(Exception):
    """The storage backend failed; the operation did not complete."""


@dataclass(frozen=True)
class Session:
    user_id: str
    workspace_id: str
    expires_at: datetime
    revoked: bool = False


class Repository(Protocol):
    def create_document(self, workspace_id: str, metadata: dict, original: bytes, *,
                        extension: str, sha256: str, content_type: str) -> None: ...

    def get_document(self, workspace_id: str, file_id: str) -> dict | None: ...

    def list_documents(self, workspace_id: str) -> list[dict]: ...

    def read_original(self, workspace_id: str, file_id: str) -> tuple[bytes, str] | None: ...

    def set_processing_status(self, workspace_id: str, file_id: str, status: str) -> None: ...

    def init_drafts(self, workspace_id: str, file_id: str, forms: list[dict]) -> tuple[list[dict], int]: ...

    def get_drafts(self, workspace_id: str, file_id: str) -> tuple[list[dict], int] | None: ...

    def save_drafts(self, workspace_id: str, file_id: str, forms: list[dict], expected_version: int) -> int: ...

    # -- direct upload: the browser sends the original to storage, never through a function body
    def create_upload_target(self, workspace_id: str, file_id: str, extension: str, content_type: str) -> dict: ...

    def read_staged(self, workspace_id: str, file_id: str, extension: str) -> bytes | None: ...

    def discard_staged(self, workspace_id: str, file_id: str, extension: str) -> None: ...

    def register_document(self, workspace_id: str, metadata: dict, *, extension: str, sha256: str,
                          content_type: str, size_bytes: int) -> None: ...

    def original_link(self, workspace_id: str, file_id: str, min_bytes: int) -> str | None: ...

    def create_session(self, token_hash: str, session: Session) -> None: ...

    def get_session(self, token_hash: str) -> Session | None: ...

    def revoke_session(self, token_hash: str) -> None: ...

    def revoke_user_sessions(self, user_id: str) -> None: ...

    def get_membership(self, user_id: str) -> str | None: ...


# ------------------------------------------------------------------ local adapter
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _write_atomic(path: Path, data: bytes) -> None:
    """Write to a unique temporary file, then replace: readers never see a half-written file."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class LocalRepository:
    """Files in one folder. For local development and tests; not persistent on Vercel.

    Layout per document: <id><ext> (original), <id>.json (metadata as returned by the API),
    <id>.owner.json (workspace and checksum), <id>.drafts.json ({"version", "forms"}).
    Files written before this layout (no owner file, drafts as a bare list) belong to the default
    workspace and are at version 1.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    # -- paths
    def _path(self, file_id: str, suffix: str) -> Path:
        validate_file_id(file_id)
        return self.folder / f"{file_id}{suffix}"

    def _owner(self, file_id: str) -> dict | None:
        path = self._path(file_id, ".owner.json")
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _belongs(self, workspace_id: str, file_id: str) -> bool:
        owner = self._owner(file_id)
        return (owner["workspace_id"] if owner else DEFAULT_WORKSPACE) == workspace_id

    # -- documents
    def create_document(self, workspace_id, metadata, original, *, extension, sha256, content_type):
        check_content_type(extension, content_type)
        file_id = metadata["file_id"]
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_atomic(self._path(file_id, extension), original)
        _write_atomic(self._path(file_id, ".owner.json"), _json_bytes({
            "workspace_id": workspace_id, "extension": extension, "content_type": content_type,
            "sha256": sha256, "size_bytes": len(original),
        }))
        # metadata last: a document is visible only once its original is stored
        _write_atomic(self._path(file_id, ".json"), _json_bytes(metadata))

    def get_document(self, workspace_id, file_id):
        path = self._path(file_id, ".json")
        if not path.is_file() or not self._belongs(workspace_id, file_id):
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_documents(self, workspace_id):
        if not self.folder.is_dir():
            return []
        result = []
        for path in self.folder.glob("*.json"):
            file_id = path.name[:-len(".json")]
            if FILE_ID_PATTERN.fullmatch(file_id):
                metadata = self.get_document(workspace_id, file_id)
                if metadata is not None:
                    result.append(metadata)
        return sorted(result, key=lambda m: m.get("uploaded_at", ""), reverse=True)

    def read_original(self, workspace_id, file_id):
        metadata = self.get_document(workspace_id, file_id)
        if metadata is None:
            return None
        owner = self._owner(file_id)
        extension = owner["extension"] if owner else Path(metadata["filename"]).suffix.lower()
        path = self._path(file_id, extension)
        if not path.is_file():
            return None
        return path.read_bytes(), extension

    def set_processing_status(self, workspace_id, file_id, status):
        with _lock_for(str(self._path(file_id, ".json"))):
            metadata = self.get_document(workspace_id, file_id)
            if metadata is None:
                raise KeyError(file_id)
            _write_atomic(self._path(file_id, ".json"), _json_bytes({**metadata, "processing_status": status}))

    # -- drafts
    def _read_drafts(self, file_id) -> tuple[list[dict], int] | None:
        path = self._path(file_id, ".drafts.json")
        if not path.is_file():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(stored, list):
            return stored, 1
        return stored["forms"], stored["version"]

    def init_drafts(self, workspace_id, file_id, forms):
        if not self._belongs(workspace_id, file_id):
            raise KeyError(file_id)
        with _lock_for(str(self._path(file_id, ".drafts.json"))):
            existing = self._read_drafts(file_id)
            if existing is not None:
                return existing
            self.folder.mkdir(parents=True, exist_ok=True)
            _write_atomic(self._path(file_id, ".drafts.json"), _json_bytes({"version": 1, "forms": forms}))
            return forms, 1

    def get_drafts(self, workspace_id, file_id):
        if not self._belongs(workspace_id, file_id):
            return None
        return self._read_drafts(file_id)

    def save_drafts(self, workspace_id, file_id, forms, expected_version):
        if not self._belongs(workspace_id, file_id):
            raise KeyError(file_id)
        with _lock_for(str(self._path(file_id, ".drafts.json"))):
            current = self._read_drafts(file_id)
            if current is None:
                raise KeyError(file_id)
            if current[1] != expected_version:
                raise VersionConflict(current[1])
            version = expected_version + 1
            _write_atomic(self._path(file_id, ".drafts.json"), _json_bytes({"version": version, "forms": forms}))
            return version

    # -- direct upload (local: the "storage" target is the API's own PUT route)
    def _staged(self, workspace_id, file_id) -> dict | None:
        path = self._path(file_id, ".staged.json")
        if not path.is_file():
            return None
        staged = json.loads(path.read_text(encoding="utf-8"))
        return staged if staged["workspace_id"] == workspace_id else None

    def create_upload_target(self, workspace_id, file_id, extension, content_type):
        check_content_type(extension, content_type)
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_atomic(self._path(file_id, ".staged.json"), _json_bytes({
            "workspace_id": workspace_id, "extension": extension, "content_type": content_type,
        }))
        return {"method": "PUT", "api_path": f"/documents/uploads/{file_id}/content",
                "headers": {"Content-Type": content_type}}

    def store_staged(self, workspace_id: str, file_id: str, content: bytes) -> None:
        """Local only: the body of the PUT returned by create_upload_target."""
        staged = self._staged(workspace_id, file_id)
        if staged is None or self._path(file_id, ".json").is_file():
            raise KeyError(file_id)
        _write_atomic(self._path(file_id, staged["extension"]), content)

    def read_staged(self, workspace_id, file_id, extension):
        staged = self._staged(workspace_id, file_id)
        if staged is None or staged["extension"] != extension:
            return None
        path = self._path(file_id, extension)
        return path.read_bytes() if path.is_file() else None

    def discard_staged(self, workspace_id, file_id, extension):
        if self._staged(workspace_id, file_id) is None:
            return
        self._path(file_id, extension).unlink(missing_ok=True)
        self._path(file_id, ".staged.json").unlink(missing_ok=True)

    def register_document(self, workspace_id, metadata, *, extension, sha256, content_type, size_bytes):
        check_content_type(extension, content_type)
        file_id = metadata["file_id"]
        if self._staged(workspace_id, file_id) is None:
            raise KeyError(file_id)
        _write_atomic(self._path(file_id, ".owner.json"), _json_bytes({
            "workspace_id": workspace_id, "extension": extension, "content_type": content_type,
            "sha256": sha256, "size_bytes": size_bytes,
        }))
        _write_atomic(self._path(file_id, ".json"), _json_bytes(metadata))
        self._path(file_id, ".staged.json").unlink(missing_ok=True)

    def original_link(self, workspace_id, file_id, min_bytes):
        return None         # local files are always served by the API itself

    # -- sessions and membership
    def _read_table(self, name: str) -> dict:
        path = self.folder / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _write_table(self, name: str, table: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.folder / name, _json_bytes(table))

    def create_session(self, token_hash, session):
        with _lock_for(str(self.folder / "_sessions.json")):
            table = self._read_table("_sessions.json")
            table[token_hash] = {
                "user_id": session.user_id, "workspace_id": session.workspace_id,
                "expires_at": session.expires_at.isoformat(), "revoked": session.revoked,
            }
            self._write_table("_sessions.json", table)

    def get_session(self, token_hash):
        row = self._read_table("_sessions.json").get(token_hash)
        if row is None:
            return None
        return Session(row["user_id"], row["workspace_id"], datetime.fromisoformat(row["expires_at"]), row["revoked"])

    def _revoke_where(self, predicate) -> None:
        with _lock_for(str(self.folder / "_sessions.json")):
            table = self._read_table("_sessions.json")
            for key, row in table.items():
                if predicate(key, row):
                    row["revoked"] = True
            self._write_table("_sessions.json", table)

    def revoke_session(self, token_hash):
        self._revoke_where(lambda key, row: key == token_hash)

    def revoke_user_sessions(self, user_id):
        self._revoke_where(lambda key, row: row["user_id"] == user_id)

    def get_membership(self, user_id):
        return self._read_table("_members.json").get(user_id)

    def set_membership(self, user_id: str, workspace_id: str) -> None:
        """Local setup only (tests, development). Membership in Supabase is managed by an administrator."""
        with _lock_for(str(self.folder / "_members.json")):
            table = self._read_table("_members.json")
            table[user_id] = workspace_id
            self._write_table("_members.json", table)


# ------------------------------------------------------------------ dependencies
@lru_cache
def get_repository() -> Repository:
    backend = os.environ.get("LABEXTRACT_STORAGE", "local")
    if backend == "supabase":
        from backend.supabase_repo import SupabaseRepository
        return SupabaseRepository.from_env()
    if backend != "local":
        raise RuntimeError(f"Unknown LABEXTRACT_STORAGE: {backend}")
    if os.environ.get("VERCEL"):
        raise RuntimeError("Local storage is not persistent on Vercel; set LABEXTRACT_STORAGE=supabase")
    return LocalRepository(Path(os.environ.get("LABEXTRACT_LOCAL_DIR", ROOT / "local_uploads")))
