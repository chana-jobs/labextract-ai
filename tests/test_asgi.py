"""D12 preparation: the API mounted under /api keeps sign-in, protection and same-origin checks."""
import pytest
from fastapi.testclient import TestClient

from backend import auth as auth_module
from backend.app import app as api
from backend.asgi import app
from backend.auth import LocalAuthenticator, LoginRateLimiter, get_authenticator, hash_password
from backend.storage import LocalRepository, get_repository

pytestmark = pytest.mark.real_auth

USER, EMAIL, PASSWORD = "aaaaaaaa-0000-0000-0000-00000000000a", "secretary@synthetic.test", "synthetic-Passw0rd"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    repo = LocalRepository(tmp_path)
    repo.set_membership(USER, "demo")
    accounts = LocalAuthenticator({EMAIL: {"user_id": USER, "password_hash": hash_password(PASSWORD)}})
    monkeypatch.setattr(auth_module, "_limiter", LoginRateLimiter())
    api.dependency_overrides[get_repository] = lambda: LocalRepository(tmp_path)
    api.dependency_overrides[get_authenticator] = lambda: accounts
    yield
    api.dependency_overrides.clear()


def test_api_is_served_under_api_only():
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 404
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    assert client.get("/api/documents").status_code == 401


def test_vercel_function_entry_is_the_same_app_with_nested_routes():
    import api.index as entry
    assert entry.app is app
    client = TestClient(entry.app, base_url="https://testserver")
    assert client.get("/api/health").json() == {"status": "ok"}
    for method, path in [("GET", "/api/auth/me"), ("GET", "/api/documents"),
                         ("GET", f"/api/documents/{'0' * 32}/drafts"),
                         ("PATCH", f"/api/review/{'0' * 32}/f1/field"),
                         ("POST", f"/api/review/{'0' * 32}/f1/field/approval")]:
        assert client.request(method, path).status_code == 401, path
    assert client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"}).status_code == 401
    assert client.get("/").status_code == 404 and client.get("/api/index").status_code == 404


def test_deployment_is_one_container_with_pinned_dependencies():
    """Deployed as a single Vercel container (Dockerfile.vercel): no separate Python function competes for /api."""
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    lines = [l.strip() for l in (root / "requirements.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", l) for l in lines), lines
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    assert "functions" not in config and "rewrites" not in config and "buildCommand" not in config
    read = lambda name: (root / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert read("Dockerfile.vercel") == read("Dockerfile")
    ignored = read(".vercelignore").splitlines()
    assert "/api/" in ignored and "api/" not in ignored          # unanchored would also drop frontend/src/api
    for secret in (".env", ".env.*"):
        assert secret in ignored


def test_sign_in_and_same_origin_under_api():
    client = TestClient(app, base_url="https://testserver")
    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD},
                        headers={"Origin": "https://testserver"})
    assert login.status_code == 200 and "Path=/" in login.headers["set-cookie"]
    assert client.get("/api/auth/me").json() == {"user_id": USER, "workspace_id": "demo"}
    assert client.get("/api/documents").json() == []
    refused = client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    assert client.post("/api/auth/logout", headers={"Origin": "https://testserver"}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
