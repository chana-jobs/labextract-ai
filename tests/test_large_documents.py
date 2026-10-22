"""Large documents: direct upload (browser -> Storage, not bound by the function body limit) and page-by-page
processing (each request extracts one page, inside the function time limit). Synthetic PDFs only."""
import io
import os

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import drafts as drafts_module
from backend.app import app
from backend.storage import DEFAULT_WORKSPACE, LocalRepository, get_repository
from backend.supabase_repo import SupabaseRepository
from backend.uploads import MAX_FILE_SIZE
from tests.fake_supabase import FakeSupabase

client = TestClient(app)
TEXT = ["First name: Dana", "Last name: Levi", "Israeli ID: 000000018", "Sample received date: 2026-08-23",
        "Sender: Synthetic Clinic", "Central number: 000987", "Commitment number: 000730001"]


def _text_page(doc):
    page = doc.new_page()
    for n, line in enumerate(TEXT):
        page.insert_text((72, 80 + 24 * n), line, fontsize=11)


def _noise_page(doc, side=1300):
    """An image-only page of random pixels: no text layer, and too large for the 4 MB function body."""
    image = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    page = doc.new_page()
    page.insert_image(page.rect, stream=buffer.getvalue())


def _pdf(*kinds) -> bytes:
    doc = pymupdf.open()
    for kind in kinds:
        {"text": _text_page, "scan": _noise_page}[kind](doc)
    return doc.tobytes()


@pytest.fixture
def local(tmp_path):
    repo = LocalRepository(tmp_path)
    app.dependency_overrides[get_repository] = lambda: repo
    yield repo
    app.dependency_overrides.pop(get_repository, None)


ANON_KEY = "synthetic-anon-key"


@pytest.fixture
def fake():
    fake = FakeSupabase()
    app.dependency_overrides[get_repository] = lambda: SupabaseRepository(fake.base_url, fake.key,
                                                                          "labextract-originals", http=fake,
                                                                          anon_key=ANON_KEY)
    yield fake
    app.dependency_overrides.pop(get_repository, None)


def _start(filename: str, size: int):
    return client.post("/documents/uploads", json={"filename": filename, "size": size})


# ------------------------------------------------------------------ direct upload, local development path
def test_direct_upload_accepts_a_file_larger_than_the_function_body_limit(local):
    content = _pdf("text", "scan", "scan")
    assert len(content) > MAX_FILE_SIZE
    assert client.post("/documents/upload", files={"file": ("big.pdf", content, "application/pdf")}).status_code == 413

    started = _start("big.pdf", len(content)).json()
    upload = started["upload"]
    assert upload["method"] == "PUT" and "url" not in upload
    assert client.put(upload["api_path"], content=content, headers=upload["headers"]).status_code == 204
    meta = client.post(f"/documents/uploads/{started['file_id']}/complete", json={"filename": "big.pdf"}).json()

    assert meta["file_id"] == started["file_id"] and meta["page_count"] == 3
    assert set(meta) == {"file_id", "filename", "page_count", "processing_status", "split_status", "uploaded_at"}
    assert client.get(f"/documents/{meta['file_id']}/file").content == content
    assert client.get("/documents").json()[0]["file_id"] == meta["file_id"]


def test_direct_upload_refuses_unverified_content_and_keeps_nothing(local):
    started = _start("fake.pdf", 20).json()
    assert client.put(started["upload"]["api_path"], content=b"not a pdf at all!!!!").status_code == 204
    r = client.post(f"/documents/uploads/{started['file_id']}/complete", json={"filename": "fake.pdf"})
    assert r.status_code == 422
    assert local.read_staged(DEFAULT_WORKSPACE, started["file_id"], ".pdf") is None
    assert client.get("/documents").json() == []


def test_direct_upload_limits(local):
    assert _start("notes.txt", 10).status_code == 415
    assert _start("huge.pdf", 21 * 1024 * 1024).status_code == 413
    assert client.post(f"/documents/uploads/{'a' * 32}/complete", json={"filename": "x.pdf"}).status_code == 404
    started = _start("a.pdf", 10).json()
    assert local.read_staged("another-workspace", started["file_id"], ".pdf") is None      # workspace-scoped


# ------------------------------------------------------------------ direct upload, Supabase path
def test_supabase_direct_upload_uses_a_signed_url_and_never_exposes_the_key(fake):
    content = _pdf("text", "scan", "scan")
    started = _start("big.pdf", len(content))
    assert fake.key not in started.text
    upload = started.json()["upload"]
    assert upload["url"].startswith(fake.base_url + "/storage/v1/object/upload/sign/labextract-originals/")
    assert upload["headers"]["apikey"] == ANON_KEY                     # the public key only, never the service key
    assert fake.client_put(upload["url"], content, upload["headers"]) == 200

    file_id = started.json()["file_id"]
    meta = client.post(f"/documents/uploads/{file_id}/complete", json={"filename": "big.pdf"}).json()
    assert meta["page_count"] == 3
    [row] = fake.tables["lx_documents"]
    assert row["size_bytes"] == len(content) and row["storage_path"] == f"{DEFAULT_WORKSPACE}/{file_id}.pdf"

    # an original larger than a function response is served through a short-lived signed link
    r = client.get(f"/documents/{file_id}/file", follow_redirects=False)
    assert r.status_code == 307 and "/storage/v1/object/sign/" in r.headers["location"]
    assert fake.key not in r.headers["location"]


def test_supabase_signed_upload_url_is_bound_to_its_object(fake):
    upload = _start("a.pdf", 100).json()["upload"]
    tampered = upload["url"].replace(f"{DEFAULT_WORKSPACE}/", "other-workspace/")
    assert fake.client_put(tampered, _pdf("text"), upload["headers"]) == 403


def test_supabase_invalid_upload_is_deleted(fake):
    started = _start("fake.pdf", 20).json()
    fake.client_put(started["upload"]["url"], b"not a pdf at all!!!!", started["upload"]["headers"])
    r = client.post(f"/documents/uploads/{started['file_id']}/complete", json={"filename": "fake.pdf"})
    assert r.status_code == 422
    assert fake.objects == {} and fake.tables["lx_documents"] == []


def test_small_supabase_original_is_still_served_inline(fake):
    started = _start("s.pdf", 10).json()
    content = _pdf("text")
    fake.client_put(started["upload"]["url"], content, started["upload"]["headers"])
    client.post(f"/documents/uploads/{started['file_id']}/complete", json={"filename": "s.pdf"})
    r = client.get(f"/documents/{started['file_id']}/file", follow_redirects=False)
    assert r.status_code == 200 and r.content == content


# ------------------------------------------------------------------ page-by-page processing
def _upload_local(content: bytes) -> str:
    r = client.post("/documents/upload", files={"file": ("doc.pdf", content, "application/pdf")})
    assert r.status_code == 200
    return r.json()["file_id"]


def _small_three_pages() -> bytes:
    doc = pymupdf.open()
    for _ in range(3):
        _text_page(doc)
    return doc.tobytes()


def test_pages_are_processed_one_request_each(local):
    file_id = _upload_local(_small_three_pages())
    first = client.post(f"/documents/{file_id}/drafts/pages/1").json()
    assert (first["page"], first["page_count"], first["remaining"]) == (1, 3, 2)
    assert first["form"]["form_id"] == f"{file_id}-p1"
    assert first["form"]["examinations"][0]["commitments"][0]["number"]["normalized"] == "000730001"
    assert client.get(f"/documents/{file_id}").json()["processing_status"] == "RUNNING"

    statuses = [f["processing_status"] for f in client.get(f"/documents/{file_id}/drafts").json()]
    assert statuses == ["DONE", "QUEUED", "QUEUED"]
    summary = client.get(f"/documents/{file_id}/export/summary").json()
    assert summary["is_partial"] and any("עדיין לא עובדו" in w for w in summary["warnings"])

    assert client.post(f"/documents/{file_id}/drafts/pages/2").json()["remaining"] == 1
    assert client.post(f"/documents/{file_id}/drafts/pages/3").json()["remaining"] == 0
    assert client.get(f"/documents/{file_id}").json()["processing_status"] == "DONE"
    forms = client.get(f"/documents/{file_id}/drafts").json()
    assert [f["form_id"] for f in forms] == [f"{file_id}-p{n}" for n in (1, 2, 3)]
    assert {f["processing_status"] for f in forms} == {"DONE"}


def test_a_processed_page_is_never_extracted_twice(local, monkeypatch):
    file_id = _upload_local(_small_three_pages())
    calls = []
    original = drafts_module.extract_page
    monkeypatch.setattr(drafts_module, "extract_page", lambda *a: calls.append(a[3]) or original(*a))
    client.post(f"/documents/{file_id}/drafts/pages/2")
    again = client.post(f"/documents/{file_id}/drafts/pages/2").json()
    assert calls == [1] and again["form"]["processing_status"] == "DONE"


def test_a_failed_page_is_failed_and_can_be_retried(local, monkeypatch):
    file_id = _upload_local(_small_three_pages())
    original = drafts_module.extract_page

    def broken(*args):
        raise RuntimeError("engine down")
    monkeypatch.setattr(drafts_module, "extract_page", broken)
    failed = client.post(f"/documents/{file_id}/drafts/pages/1").json()["form"]
    assert failed["processing_status"] == "FAILED" and failed["issues"][0]["reason"] == "TECHNICAL_FAILURE"

    monkeypatch.setattr(drafts_module, "extract_page", original)
    assert client.post(f"/documents/{file_id}/drafts/pages/1").json()["form"]["processing_status"] == "FAILED"
    retried = client.post(f"/documents/{file_id}/drafts/pages/1?retry=true").json()["form"]
    assert retried["processing_status"] == "DONE"


def test_a_review_edit_survives_processing_of_other_pages(local):
    file_id = _upload_local(_small_three_pages())
    form = client.post(f"/documents/{file_id}/drafts/pages/1").json()["form"]
    sender = form["examinations"][0]["sender"]
    r = client.patch(f"/review/{file_id}/{form['form_id']}/field",
                     json={"target": {"field": "sender"}, "value": "Synthetic Clinic North",
                           "expected_version": sender["version"]})
    assert r.status_code == 200
    client.post(f"/documents/{file_id}/drafts/pages/2")
    client.post(f"/documents/{file_id}/drafts/pages/3")
    first = client.get(f"/documents/{file_id}/drafts").json()[0]
    assert first["examinations"][0]["sender"]["normalized"] == "Synthetic Clinic North"


def test_page_out_of_range_and_unknown_document(local):
    file_id = _upload_local(_small_three_pages())
    assert client.post(f"/documents/{file_id}/drafts/pages/0").status_code == 404
    assert client.post(f"/documents/{file_id}/drafts/pages/4").status_code == 404
    assert client.post(f"/documents/{'b' * 32}/drafts/pages/1").status_code == 404


def test_legacy_init_finishes_a_document_started_page_by_page(local):
    file_id = _upload_local(_small_three_pages())
    client.post(f"/documents/{file_id}/drafts/pages/1")
    forms = client.post(f"/documents/{file_id}/drafts/init").json()
    assert [f["processing_status"] for f in forms] == ["DONE", "DONE", "DONE"]
    assert client.get(f"/documents/{file_id}").json()["processing_status"] == "DONE"


def test_legacy_init_alone_is_unchanged(local):
    file_id = _upload_local(_small_three_pages())
    forms = client.post(f"/documents/{file_id}/drafts/init").json()
    assert len(forms) == 3 and {f["processing_status"] for f in forms} == {"DONE"}
    assert client.post(f"/documents/{file_id}/drafts/pages/2").json()["form"] == forms[1]
