"""IF-5: file ids from the request path are validated before any storage path is built.
Synthetic files in a temporary folder only."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import app
from backend.storage import LocalRepository, get_repository, validate_file_id

FILE_ID = "0123456789abcdef0123456789abcdef"

client = TestClient(app)


@pytest.fixture
def store(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    app.dependency_overrides[get_repository] = lambda: LocalRepository(store)
    # a JSON file OUTSIDE the storage folder that a traversal would try to reach
    (tmp_path / "outside.json").write_text(json.dumps({"filename": "x.pdf"}), encoding="utf-8")
    yield store
    app.dependency_overrides.clear()


@pytest.mark.parametrize("bad", ["abc", FILE_ID.upper(), FILE_ID + "0", "..", "../outside", "..\\outside",
                                 "0123456789abcdef0123456789abcdeg", "", None, 123])
def test_validate_file_id_rejects(bad):
    with pytest.raises(HTTPException) as e:
        validate_file_id(bad)
    assert e.value.status_code == 400


def test_validate_file_id_accepts_uuid_hex():
    assert validate_file_id(FILE_ID) == FILE_ID


@pytest.mark.parametrize("path", ["/documents/..%5Coutside", "/documents/..%5Coutside/file",
                                  "/documents/not-a-file-id", "/documents/not-a-file-id/file"])
def test_document_routes_reject_invalid_ids(store, path):
    r = client.get(path)
    assert r.status_code == 400 and r.json()["detail"] == "Invalid file ID"


def test_valid_but_unknown_id_is_404(store):
    assert client.get(f"/documents/{FILE_ID}").status_code == 404
    assert client.get(f"/documents/{FILE_ID}/file").status_code == 404


def test_valid_id_is_served(store):
    (store / f"{FILE_ID}.json").write_text(json.dumps({"file_id": FILE_ID, "filename": "synthetic.pdf"}),
                                           encoding="utf-8")
    (store / f"{FILE_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic")
    assert client.get(f"/documents/{FILE_ID}").json()["filename"] == "synthetic.pdf"
    r = client.get(f"/documents/{FILE_ID}/file")
    assert r.content == b"%PDF-1.4 synthetic" and r.headers["content-type"] == "application/pdf"
