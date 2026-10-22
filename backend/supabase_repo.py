"""Repository on Supabase: Postgres through PostgREST, originals in a private Storage bucket.

Uses only the standard library (no new dependency). The service-role key is read from the
environment at runtime and is never logged. Tables and policies: migrations/001_labextract_init.sql.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from backend.storage import Session, StorageError, VersionConflict, check_content_type, validate_file_id

# Product tables carry the lx_ prefix: they share the project's public schema with other work.
T_DOCUMENTS = "lx_documents"
T_DRAFTS = "lx_drafts"
T_SESSIONS = "lx_sessions"
T_WORKSPACE_MEMBERS = "lx_workspace_members"

# (method, url, headers, body) -> (status, body)
Http = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]

TIMEOUT_SECONDS = 20


def urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        raise StorageError(f"storage unreachable: {error.reason}") from None


def _eq(value: str) -> str:
    return f"eq.{value}"


class SupabaseRepository:
    def __init__(self, url: str, service_key: str, bucket: str, http: Http = urllib_http):
        self.url = url.rstrip("/")
        self._key = service_key
        self.bucket = bucket
        self._http = http

    @classmethod
    def from_env(cls) -> "SupabaseRepository":
        try:
            return cls(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                os.environ.get("LABEXTRACT_BUCKET", "labextract-originals"),
            )
        except KeyError as missing:
            raise RuntimeError(f"Missing environment variable {missing}") from None

    # -- transport
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        headers.update(extra or {})
        return headers

    def _rest(self, method: str, table: str, *, params: dict | None = None, payload=None,
              prefer: str | None = None) -> list | None:
        url = f"{self.url}/rest/v1/{table}"
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=".,*()")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, raw = self._http(method, url, self._headers(headers), body)
        if status >= 300:
            raise StorageError(f"{method} {table} failed with HTTP {status}")
        return json.loads(raw) if raw else None

    def _object_url(self, path: str) -> str:
        return f"{self.url}/storage/v1/object/{self.bucket}/{urllib.parse.quote(path)}"

    # -- documents
    def create_document(self, workspace_id, metadata, original, *, extension, sha256, content_type):
        check_content_type(extension, content_type)          # before any request is sent
        file_id = validate_file_id(metadata["file_id"])
        object_path = f"{workspace_id}/{file_id}{extension}"
        status, _ = self._http("POST", self._object_url(object_path), self._headers({
            "Content-Type": content_type, "x-upsert": "false",
        }), original)
        if status >= 300:
            raise StorageError(f"upload of the original failed with HTTP {status}")
        try:
            self._rest("POST", T_DOCUMENTS, payload={
                "file_id": file_id, "workspace_id": workspace_id, "metadata": metadata,
                "extension": extension, "sha256": sha256, "size_bytes": len(original),
                "storage_path": object_path,
            })
        except StorageError:
            # no metadata row: remove the orphan object so nothing unreferenced stays stored
            self._http("DELETE", self._object_url(object_path), self._headers(), None)
            raise

    def _document_row(self, workspace_id, file_id, select: str) -> dict | None:
        rows = self._rest("GET", T_DOCUMENTS, params={
            "file_id": _eq(validate_file_id(file_id)), "workspace_id": _eq(workspace_id), "select": select,
        })
        return rows[0] if rows else None

    def get_document(self, workspace_id, file_id):
        row = self._document_row(workspace_id, file_id, "metadata")
        return row["metadata"] if row else None

    def list_documents(self, workspace_id):
        rows = self._rest("GET", T_DOCUMENTS, params={
            "workspace_id": _eq(workspace_id), "select": "metadata", "order": "created_at.desc",
        })
        return [row["metadata"] for row in rows or []]

    def read_original(self, workspace_id, file_id):
        row = self._document_row(workspace_id, file_id, "storage_path,extension")
        if row is None:
            return None
        status, content = self._http("GET", self._object_url(row["storage_path"]), self._headers(), None)
        if status == 404:
            return None
        if status >= 300:
            raise StorageError(f"download of the original failed with HTTP {status}")
        return content, row["extension"]

    def set_processing_status(self, workspace_id, file_id, status):
        metadata = self.get_document(workspace_id, file_id)
        if metadata is None:
            raise KeyError(file_id)
        self._rest("PATCH", T_DOCUMENTS, params={
            "file_id": _eq(validate_file_id(file_id)), "workspace_id": _eq(workspace_id),
        }, payload={"metadata": {**metadata, "processing_status": status}}, prefer="return=minimal")

    # -- drafts
    def _drafts_row(self, workspace_id, file_id) -> dict | None:
        rows = self._rest("GET", T_DRAFTS, params={
            "file_id": _eq(validate_file_id(file_id)), "workspace_id": _eq(workspace_id), "select": "forms,version",
        })
        return rows[0] if rows else None

    def init_drafts(self, workspace_id, file_id, forms):
        if self._document_row(workspace_id, file_id, "file_id") is None:
            raise KeyError(file_id)
        # insert only if absent; a concurrent init keeps the first version
        self._rest("POST", T_DRAFTS, payload={
            "file_id": file_id, "workspace_id": workspace_id, "forms": forms, "version": 1,
        }, prefer="resolution=ignore-duplicates,return=minimal")
        row = self._drafts_row(workspace_id, file_id)
        return row["forms"], row["version"]

    def get_drafts(self, workspace_id, file_id):
        row = self._drafts_row(workspace_id, file_id)
        return (row["forms"], row["version"]) if row else None

    def save_drafts(self, workspace_id, file_id, forms, expected_version):
        rows = self._rest("PATCH", T_DRAFTS, params={
            "file_id": _eq(validate_file_id(file_id)), "workspace_id": _eq(workspace_id),
            "version": _eq(str(expected_version)),
        }, payload={
            "forms": forms, "version": expected_version + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="return=representation")
        if rows:
            return expected_version + 1
        current = self._drafts_row(workspace_id, file_id)
        if current is None:
            raise KeyError(file_id)
        raise VersionConflict(current["version"])

    # -- sessions and membership
    def create_session(self, token_hash, session):
        self._rest("POST", T_SESSIONS, payload={
            "token_hash": token_hash, "user_id": session.user_id, "workspace_id": session.workspace_id,
            "expires_at": session.expires_at.isoformat(),
        }, prefer="return=minimal")

    def get_session(self, token_hash):
        rows = self._rest("GET", T_SESSIONS, params={
            "token_hash": _eq(token_hash), "select": "user_id,workspace_id,expires_at,revoked_at",
        })
        if not rows:
            return None
        row = rows[0]
        return Session(row["user_id"], row["workspace_id"], datetime.fromisoformat(row["expires_at"]),
                       row["revoked_at"] is not None)

    def revoke_session(self, token_hash):
        self._rest("PATCH", T_SESSIONS, params={"token_hash": _eq(token_hash), "revoked_at": "is.null"},
                   payload={"revoked_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")

    def revoke_user_sessions(self, user_id):
        self._rest("PATCH", T_SESSIONS, params={"user_id": _eq(user_id), "revoked_at": "is.null"},
                   payload={"revoked_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")

    def get_membership(self, user_id):
        rows = self._rest("GET", T_WORKSPACE_MEMBERS, params={
            "user_id": _eq(user_id), "select": "workspace_id", "order": "workspace_id",
        })
        # one workspace per user in Stage A; several memberships are refused rather than guessed
        return rows[0]["workspace_id"] if rows and len(rows) == 1 else None
