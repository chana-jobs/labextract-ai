"""HTTP integration with REAL sign-in (no test principal): sessions, 401 on every protected route,
cross-site refusal, workspace isolation, and the full path upload -> drafts/init (text extraction)
-> review -> export. Synthetic users and synthetic documents only; nothing is injected into storage."""
import io

import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from backend import auth as auth_module
from backend.app import app
from backend.auth import LocalAuthenticator, LoginRateLimiter, check_deployment_config, get_authenticator, hash_password
from backend.storage import LocalRepository, get_repository

pytestmark = pytest.mark.real_auth

USER_A, USER_B = "aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b"
EMAIL_A, EMAIL_B, PASSWORD = "secretary.a@synthetic.test", "secretary.b@synthetic.test", "synthetic-Passw0rd"
PUBLIC = {"/health", "/auth/login", "/auth/logout"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    repo = LocalRepository(tmp_path)
    repo.set_membership(USER_A, "demo")
    repo.set_membership(USER_B, "other-lab")
    accounts = LocalAuthenticator({
        EMAIL_A: {"user_id": USER_A, "password_hash": hash_password(PASSWORD)},
        EMAIL_B: {"user_id": USER_B, "password_hash": hash_password(PASSWORD)},
    })
    monkeypatch.setattr(auth_module, "_limiter", LoginRateLimiter())
    app.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    app.dependency_overrides[get_authenticator] = lambda: accounts
    yield tmp_path
    app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _signed_in(email=EMAIL_A) -> TestClient:
    client = _client()
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def _pdf(lines) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for n, text in enumerate(lines):
        page.insert_text((72, 80 + 24 * n), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


FORM = ["Synthetic laboratory form", "First name: Dana", "Last name: Levi", "Israeli ID: 000000018",
        "Sample received date: 23/08/2026", "Sender: Synthetic Clinic", "Central number: 000987",
        "Commitment number: 000730001"]


def _upload(client, content, name="synthetic.pdf", media="application/pdf"):
    return client.post("/documents/upload", files={"file": (name, content, media)})


# ---- D5: every protected route answers 401 without a session --------------------------------
def test_every_protected_route_requires_a_session():
    # the OpenAPI schema lists every route, including those of included routers
    routes = [(method.upper(), path) for path, operations in app.openapi()["paths"].items()
              for method in operations if path not in PUBLIC]
    client = _client()
    for method, template in routes:
        path = template.replace("{file_id}", "0" * 32).replace("{form_id}", "f1")
        r = client.request(method, path)
        assert r.status_code == 401, f"{method} {template} -> {r.status_code}"
        assert r.json() == {"detail": {"error": "UNAUTHENTICATED"}}
    assert {(m, p) for m, p in routes} >= {
        ("POST", "/documents/upload"), ("GET", "/documents"), ("GET", "/documents/{file_id}"),
        ("GET", "/documents/{file_id}/file"), ("POST", "/documents/{file_id}/drafts/init"),
        ("GET", "/documents/{file_id}/drafts"), ("PUT", "/review/{file_id}/{form_id}"),
        ("GET", "/documents/{file_id}/export/summary"), ("GET", "/documents/{file_id}/export.xlsx"),
        ("GET", "/auth/me"), ("GET", "/mock/result"),
    }


# ---- D1-D3: sign-in, current user, sign-out ----------------------------------------------------
def test_login_me_logout():
    client = _client()
    r = client.post("/auth/login", json={"email": EMAIL_A, "password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"user_id": USER_A, "workspace_id": "demo"}
    cookie = r.headers["set-cookie"]
    assert "lx_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower() and "Path=/" in cookie
    token = client.cookies.get("lx_session")
    assert client.get("/auth/me").json() == {"user_id": USER_A, "workspace_id": "demo"}

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401
    replay = _client()
    replay.cookies.set("lx_session", token)
    assert replay.get("/auth/me").status_code == 401                     # revoked on the server


@pytest.mark.parametrize("email,password", [(EMAIL_A, "wrong"), ("nobody@synthetic.test", PASSWORD)])
def test_failed_login_does_not_say_why(email, password):
    r = _client().post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 401 and r.json() == {"detail": {"error": "INVALID_CREDENTIALS"}}
    assert "set-cookie" not in r.headers


def test_login_is_rate_limited():
    client = _client()
    for _ in range(5):
        assert client.post("/auth/login", json={"email": EMAIL_A, "password": "wrong"}).status_code == 401
    r = client.post("/auth/login", json={"email": EMAIL_A, "password": PASSWORD})
    assert r.status_code == 429 and r.json() == {"detail": {"error": "RATE_LIMITED"}}


# ---- full path over HTTP ----------------------------------------------------------------------
def test_upload_extract_review_export_over_http():
    client = _signed_in()
    meta = _upload(client, _pdf(FORM)).json()
    file_id = meta["file_id"]
    assert meta["processing_status"] == "QUEUED"
    assert [d["file_id"] for d in client.get("/documents").json()] == [file_id]

    forms = client.post(f"/documents/{file_id}/drafts/init").json()
    exam = forms[0]["examinations"][0]
    form_id = forms[0]["form_id"]
    assert exam["first_name"]["normalized"] == "Dana" and exam["israeli_id"]["normalized"] == "000000018"
    assert exam["first_name"]["approval_status"] == "PENDING"
    assert exam["first_name"]["evidence"][0]["method"] == "pdf_text_layer"
    assert [c["number"]["normalized"] for c in exam["commitments"]] == ["000730001"]
    assert client.get(f"/documents/{file_id}").json()["processing_status"] == "DONE"

    # a correction is saved first, then everything shown is approved
    assert client.put(f"/review/{file_id}/{form_id}", json={"sender": "Synthetic Clinic North"}).status_code == 200
    values = {"first_name": "Dana", "last_name": "Levi", "israeli_id": "000000018",
              "sample_received_date": "2026-08-23", "sender": "Synthetic Clinic North",
              "central_numbers": ["000987"], "commitment_numbers": ["000730001"]}
    approved = client.put(f"/review/{file_id}/{form_id}", json={**values, "approve": True})
    assert approved.status_code == 200 and approved.json()["approved"] is True

    summary = client.get(f"/documents/{file_id}/export/summary").json()
    assert summary["rows"] == 1 and summary["orange_cells"] == 0 and summary["is_partial"] is True
    assert client.get(f"/documents/{file_id}/export.xlsx").status_code == 409        # consent first
    excel = client.get(f"/documents/{file_id}/export.xlsx?confirm_partial=true")
    assert excel.status_code == 200 and excel.headers["cache-control"] == "private, no-store"
    row = [c.value for c in load_workbook(io.BytesIO(excel.content)).active[2]]
    assert row == ["Dana", "Levi", "000000018", "000987", "23/08/2026", "Synthetic Clinic North", "000730001"]

    original = client.get(f"/documents/{file_id}/file")
    assert original.headers["content-type"] == "application/pdf"
    assert original.headers["content-disposition"] == f'inline; filename="{file_id}.pdf"'
    assert original.headers["cache-control"] == "private, no-store"

    client.post("/auth/logout")
    assert client.get(f"/documents/{file_id}/export.xlsx?confirm_partial=true").status_code == 401
    assert client.get(f"/documents/{file_id}/file").status_code == 401


@pytest.mark.parametrize("content,name,media", [
    (_pdf([]), "scanned.pdf", "application/pdf"),
    (None, "photo.png", "image/png"),
])
def test_document_without_text_layer_is_marked_unsupported(content, name, media):
    if content is None:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
        content = buffer.getvalue()
    client = _signed_in()
    file_id = _upload(client, content, name, media).json()["file_id"]
    [form] = client.post(f"/documents/{file_id}/drafts/init").json()
    exam = form["examinations"][0]
    assert exam["first_name"]["normalized"] is None and "no_text_layer" in exam["first_name"]["flags"]
    assert form["issues"][0]["reason"] == "COMMITMENT_COUNT_UNCERTAIN"
    assert "OCR is not available" in form["issues"][0]["note"]
    # one row to review: existence uncertain, number empty and pending; nothing invented
    [row] = exam["commitments"]
    assert row["existence_status"] == "UNCERTAIN" and row["number"]["normalized"] is None
    assert row["number"]["extraction_status"] == "MISSING" and row["number"]["approval_status"] == "PENDING"
    summary = client.get(f"/documents/{file_id}/export/summary").json()
    assert summary["rows"] == 1 and summary["ready"] == 0 and summary["is_partial"]


# ---- isolation, cross-site, limits, headers ------------------------------------------------------
def test_other_workspace_sees_nothing():
    owner = _signed_in(EMAIL_A)
    file_id = _upload(owner, _pdf(FORM)).json()["file_id"]
    form_id = owner.post(f"/documents/{file_id}/drafts/init").json()[0]["form_id"]
    other = _signed_in(EMAIL_B)
    assert other.get("/documents").json() == []
    for path in (f"/documents/{file_id}", f"/documents/{file_id}/file", f"/documents/{file_id}/drafts",
                 f"/documents/{file_id}/export/summary", f"/documents/{file_id}/export.xlsx?confirm_partial=true"):
        assert other.get(path).status_code == 404, path
    assert other.put(f"/review/{file_id}/{form_id}", json={"first_name": "x"}).status_code == 404
    assert owner.get(f"/documents/{file_id}/drafts").json()[0]["examinations"][0]["first_name"]["normalized"] == "Dana"


def test_state_changes_from_another_site_are_refused():
    client = _signed_in()
    evil = {"Origin": "https://evil.example"}
    for method, path, kwargs in [("POST", "/documents/upload", {"files": {"file": ("x.pdf", _pdf(FORM), "application/pdf")}}),
                                 ("PUT", f"/review/{'0' * 32}/f1", {"json": {"first_name": "x"}}),
                                 ("POST", "/auth/login", {"json": {"email": EMAIL_A, "password": PASSWORD}}),
                                 ("POST", "/auth/logout", {})]:
        r = client.request(method, path, headers=evil, **kwargs)
        assert r.status_code == 403 and r.json() == {"detail": {"error": "ORIGIN_NOT_ALLOWED"}}, path
    assert client.post("/documents/upload", headers={"Sec-Fetch-Site": "cross-site"},
                       files={"file": ("x.pdf", _pdf(FORM), "application/pdf")}).status_code == 403
    same_site = client.post("/documents/upload", headers={"Origin": "https://testserver"},
                            files={"file": ("x.pdf", _pdf(FORM), "application/pdf")})
    assert same_site.status_code == 200
    assert client.get("/auth/me").status_code == 200                   # the refused logout did nothing


def test_upload_over_4_mb_is_refused_clearly():
    r = _upload(_signed_in(), b"%PDF-1.4\n" + b"0" * (4 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["detail"] == "File exceeds 4 MB (demo limit; synthetic documents only)"


def test_security_headers():
    r = _client().get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "same-origin"


def test_deployment_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    for name in ("LABEXTRACT_STORAGE", "LABEXTRACT_AUTH", "LABEXTRACT_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="LABEXTRACT_STORAGE.*LABEXTRACT_AUTH"):
        check_deployment_config()
    monkeypatch.setenv("LABEXTRACT_STORAGE", "supabase")
    monkeypatch.setenv("LABEXTRACT_AUTH", "supabase")
    check_deployment_config()
    monkeypatch.setenv("LABEXTRACT_COOKIE_SECURE", "false")
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        check_deployment_config()
