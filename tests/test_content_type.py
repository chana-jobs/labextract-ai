"""The stored original's content type comes from the verified format, matches the bucket's
allowed_mime_types, and a mismatched upload is refused before anything is stored. Synthetic files only."""
import io
import json
import re
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import app
from backend.storage import CONTENT_TYPES, LocalRepository, get_repository
from backend.supabase_repo import T_DOCUMENTS, T_DRAFTS, T_SESSIONS, T_WORKSPACE_MEMBERS, SupabaseRepository
from tests.fake_supabase import BUCKETS, FakeSupabase

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_labextract_init.sql"
FID = "dddddddddddddddddddddddddddddddd"
client = TestClient(app)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _meta():
    return {"file_id": FID, "filename": "synthetic", "page_count": 1, "processing_status": "QUEUED",
            "split_status": "UNCERTAIN", "uploaded_at": "2026-10-21T08:00:00+00:00"}


# ---- code, migration and bucket agree -------------------------------------------------------------
def test_migration_bucket_allows_exactly_the_stored_types():
    sql = MIGRATION.read_text(encoding="utf-8")
    allowed = set(re.findall(r"'([a-z]+/[a-z]+)'", re.search(r"allowed_mime_types\)[\s\S]*?;", sql).group(0)))
    assert allowed == set(CONTENT_TYPES.values()) == BUCKETS["labextract-originals"]["allowed_mime_types"]
    assert "4194304" in sql


def test_migration_creates_only_lx_tables_and_never_touches_the_spike():
    sql = MIGRATION.read_text(encoding="utf-8")
    created = re.findall(r"create table if not exists public\.(\w+)", sql)
    assert created and all(name.startswith("lx_") for name in created)
    assert {T_DOCUMENTS, T_DRAFTS, T_SESSIONS, T_WORKSPACE_MEMBERS} <= set(created)
    statements = re.sub(r"--[^\n]*", "", sql)                 # comments may name the spike; statements may not
    assert "spike" not in statements and "drop " not in statements.lower()


# ---- the repository sends the verified type, and refuses a wrong one before any request -----------
@pytest.mark.parametrize("extension,content_type", sorted(CONTENT_TYPES.items()))
def test_supabase_upload_uses_the_verified_type(extension, content_type):
    fake = FakeSupabase()
    repo = SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake)
    repo.create_document("ws", _meta(), b"synthetic", extension=extension, sha256="0" * 64, content_type=content_type)
    assert fake.object_types == {f"labextract-originals/ws/{FID}{extension}": content_type}


@pytest.mark.parametrize("make_repo", ["local", "supabase"])
@pytest.mark.parametrize("extension,content_type", [(".pdf", "image/png"), (".png", "application/octet-stream"),
                                                    (".exe", "application/pdf")])
def test_wrong_type_is_refused_before_anything_is_written(tmp_path, make_repo, extension, content_type):
    fake = FakeSupabase()
    repo = (LocalRepository(tmp_path) if make_repo == "local"
            else SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake))
    with pytest.raises(ValueError):
        repo.create_document("ws", _meta(), b"synthetic", extension=extension, sha256="0" * 64,
                             content_type=content_type)
    assert fake.requests == [] and list(tmp_path.iterdir()) == []


def test_bucket_would_refuse_the_old_generic_type():
    fake = FakeSupabase()
    status, _ = fake("POST", f"{fake.base_url}/storage/v1/object/labextract-originals/ws/x.pdf",
                     {"apikey": fake.key, "Authorization": f"Bearer {fake.key}",
                      "Content-Type": "application/octet-stream"}, b"synthetic")
    assert status == 415


# ---- the upload route: the content decides, a mismatch is refused -----------------------------------
@pytest.fixture
def folder(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    yield tmp_path
    app.dependency_overrides.pop(get_repository, None)


@pytest.mark.parametrize("name,content,expected", [
    ("synthetic.pdf", _pdf(), "application/pdf"),
    ("synthetic.png", _png(), "image/png"),
    ("synthetic.jpg", _jpeg(), "image/jpeg"),
    ("synthetic.jpeg", _jpeg(), "image/jpeg"),
])
def test_upload_stores_the_verified_type(folder, name, content, expected):
    r = client.post("/documents/upload", files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200
    file_id = r.json()["file_id"]
    owner = json.loads((folder / f"{file_id}.owner.json").read_text(encoding="utf-8"))
    assert owner["content_type"] == expected                      # the client's declared type is ignored
    assert client.get(f"/documents/{file_id}/file").headers["content-type"] == expected


@pytest.mark.parametrize("name,content,detail", [
    ("synthetic.jpg", _png(), "File content does not match its extension"),
    ("synthetic.png", _jpeg(), "File content does not match its extension"),
    ("synthetic.png", _pdf(), "Cannot read the uploaded document"),
    ("synthetic.pdf", _png(), "Cannot read the uploaded document"),
])
def test_mismatched_upload_is_refused_and_nothing_is_stored(folder, name, content, detail):
    r = client.post("/documents/upload", files={"file": (name, content, "image/png")})
    assert r.status_code == 422 and r.json()["detail"] == detail
    assert list(folder.iterdir()) == []
