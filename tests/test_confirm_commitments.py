"""The reviewer confirms a form's commitment rows (POST /review/{file}/{form}/commitments/confirm)."""
import pymupdf
from fastapi.testclient import TestClient

from backend.app import app
from backend.storage import LocalRepository, get_repository

client = TestClient(app)


def _setup(tmp_path):
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    doc = pymupdf.open()
    page = doc.new_page()
    for n, text in enumerate(["First name: Dana", "Last name: Levi", "Israeli ID: 000000018"]):  # no commitment
        page.insert_text((72, 80 + 24 * n), text, fontsize=11)
    meta = client.post("/documents/upload", files={"file": ("s.pdf", doc.tobytes(), "application/pdf")}).json()
    [form] = client.post(f"/documents/{meta['file_id']}/drafts/init").json()
    return meta["file_id"], form


def test_confirming_closes_the_count_issue_and_confirms_the_rows(tmp_path):
    fid, form = _setup(tmp_path)
    try:
        assert any(i["reason"] == "COMMITMENT_COUNT_UNCERTAIN" for i in form["issues"])
        n = len(form["examinations"][0]["commitments"])
        r = client.post(f"/review/{fid}/{form['form_id']}/commitments/confirm", json={"commitment_count": n})
        assert r.status_code == 200
        out = r.json()["form"]
        assert not any(i["reason"] == "COMMITMENT_COUNT_UNCERTAIN" for i in out["issues"])
        assert all(c["existence_status"] == "CONFIRMED" for c in out["examinations"][0]["commitments"])
    finally:
        app.dependency_overrides.pop(get_repository, None)


def test_a_stale_row_count_is_refused(tmp_path):
    fid, form = _setup(tmp_path)
    try:
        r = client.post(f"/review/{fid}/{form['form_id']}/commitments/confirm", json={"commitment_count": 7})
        assert r.status_code == 409
    finally:
        app.dependency_overrides.pop(get_repository, None)
