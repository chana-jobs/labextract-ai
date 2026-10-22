"""End to end through the routes on the local repository, including a restart (a new repository
instance over the same folder). Synthetic PDFs only; no record is injected by the test."""
import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.auth import current_workspace
from backend.storage import LocalRepository, get_repository

client = TestClient(app)


def _pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for n, text in enumerate(["Synthetic laboratory form", "First name: Dana", "Last name: Levi",
                              "Israeli ID: 000000018", "Sample received date: 23/08/2026",
                              "Sender: Synthetic Clinic", "Central number: 000987",
                              "Commitment number: 000730001"]):
        page.insert_text((72, 80 + 24 * n), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def folder(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    yield tmp_path
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(current_workspace, None)


def _upload():
    r = client.post("/documents/upload", files={"file": ("synthetic.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200
    return r.json()


def test_upload_persists_and_existing_response_shape_is_kept(folder):
    meta = _upload()
    assert set(meta) == {"file_id", "filename", "page_count", "processing_status", "split_status", "uploaded_at"}
    app.dependency_overrides[get_repository] = lambda: LocalRepository(folder)          # restart
    assert client.get(f"/documents/{meta['file_id']}").json() == meta
    original = client.get(f"/documents/{meta['file_id']}/file")
    assert original.status_code == 200 and original.content.startswith(b"%PDF")


def test_review_saved_before_a_restart_is_there_after_it(folder):
    file_id = _upload()["file_id"]
    form_id = client.post(f"/documents/{file_id}/drafts/init").json()[0]["form_id"]
    assert client.put(f"/review/{file_id}/{form_id}", json={"sender": "Synthetic Clinic North"}).status_code == 200

    app.dependency_overrides[get_repository] = lambda: LocalRepository(folder)          # restart
    exam = client.get(f"/documents/{file_id}/drafts").json()[0]["examinations"][0]
    assert exam["sender"]["normalized"] == "Synthetic Clinic North"
    assert exam["sender"]["candidates"][0]["raw"] == "Synthetic Clinic"     # the extracted reading is kept
    assert client.post(f"/documents/{file_id}/drafts/init").json()[0]["examinations"][0]["sender"]["normalized"] \
        == "Synthetic Clinic North"                                          # init never re-extracts over edits


def test_concurrent_save_returns_409_and_writes_nothing(folder):
    file_id = _upload()["file_id"]
    form_id = client.post(f"/documents/{file_id}/drafts/init").json()[0]["form_id"]
    repo = LocalRepository(folder)

    class StaleRead(LocalRepository):
        def get_drafts(self, workspace_id, fid):
            forms, version = super().get_drafts(workspace_id, fid)
            repo.save_drafts(workspace_id, fid, forms, expected_version=version)   # someone else saves first
            return forms, version

    app.dependency_overrides[get_repository] = lambda: StaleRead(folder)
    r = client.put(f"/review/{file_id}/{form_id}", json={"first_name": "Dina"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "VERSION_CONFLICT"
    assert repo.get_drafts("default", file_id)[0][0]["examinations"][0]["first_name"]["normalized"] == "Dana"


def test_another_workspace_cannot_reach_the_document(folder):
    file_id = _upload()["file_id"]
    form_id = client.post(f"/documents/{file_id}/drafts/init").json()[0]["form_id"]
    app.dependency_overrides[current_workspace] = lambda: "another-workspace"
    for method, path in [("GET", f"/documents/{file_id}"), ("GET", f"/documents/{file_id}/file"),
                         ("GET", f"/documents/{file_id}/drafts"), ("POST", f"/documents/{file_id}/drafts/init"),
                         ("GET", f"/documents/{file_id}/export/summary"),
                         ("GET", f"/documents/{file_id}/export.xlsx")]:
        assert client.request(method, path).status_code == 404, path
    assert client.put(f"/review/{file_id}/{form_id}", json={"first_name": "x"}).status_code == 404
    assert client.get("/documents").json() == []
