"""Internal auth components: identity check, sessions, revocation, workspace membership, rate limiting.
No route is exposed by them yet; a test-only app exercises the request dependency. Synthetic users only."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.auth import (
    SESSION_COOKIE, AuthService, AuthUnavailable, LocalAuthenticator, LoginFailed, LoginRateLimiter, Principal,
    RateLimited, SupabaseAuthenticator, get_auth_service, hash_password, require_principal,
    session_cookie_settings, token_hash, verify_password_hash,
)
from backend.storage import LocalRepository

EMAIL, PASSWORD, USER = "secretary@synthetic.test", "synthetic-Passw0rd", "11111111-1111-1111-1111-111111111111"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def repo(tmp_path):
    repo = LocalRepository(tmp_path)
    repo.set_membership(USER, "demo")
    return repo


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(repo, clock):
    users = {EMAIL: {"user_id": USER, "password_hash": hash_password(PASSWORD)}}
    return AuthService(repo, LocalAuthenticator(users), clock=clock, limiter=LoginRateLimiter(max_failures=3))


# ---- passwords ---------------------------------------------------------------------------------
def test_password_hash_roundtrip_and_salting():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$") and PASSWORD not in encoded
    assert verify_password_hash(PASSWORD, encoded) and not verify_password_hash("wrong", encoded)
    assert hash_password(PASSWORD) != encoded                       # random salt
    assert not verify_password_hash(PASSWORD, "garbage") and not verify_password_hash(PASSWORD, "md5$x$y")


# ---- login --------------------------------------------------------------------------------------
def test_login_creates_a_session_stored_only_as_a_hash(auth, repo, tmp_path):
    token, principal = auth.login(EMAIL, PASSWORD)
    assert principal == Principal(USER, "demo")
    assert token not in (tmp_path / "_sessions.json").read_text(encoding="utf-8")
    assert repo.get_session(token_hash(token)).user_id == USER
    assert auth.authenticate(token) == principal


@pytest.mark.parametrize("email,password", [(EMAIL, "wrong"), ("nobody@synthetic.test", PASSWORD), ("", ""),
                                            (EMAIL, "")])
def test_failed_logins_are_indistinguishable(auth, email, password):
    with pytest.raises(LoginFailed):
        auth.login(email, password)


def test_user_without_a_workspace_cannot_log_in(repo, clock, tmp_path):
    users = {EMAIL: {"user_id": "no-membership", "password_hash": hash_password(PASSWORD)}}
    with pytest.raises(LoginFailed):
        AuthService(repo, LocalAuthenticator(users), clock=clock).login(EMAIL, PASSWORD)


def test_repeated_failures_are_rate_limited_even_with_the_right_password(auth):
    for _ in range(3):
        with pytest.raises(LoginFailed):
            auth.login(EMAIL, "wrong", client="10.0.0.1")
    with pytest.raises(RateLimited):
        auth.login(EMAIL, PASSWORD, client="10.0.0.1")
    assert auth.login(EMAIL, PASSWORD, client="10.0.0.2")[1].user_id == USER     # other client unaffected


# ---- session lifetime ---------------------------------------------------------------------------
def test_session_expires(auth, clock):
    token, _ = auth.login(EMAIL, PASSWORD)
    clock.now += timedelta(hours=8)
    assert auth.authenticate(token) is None


def test_logout_disable_and_membership_removal_take_effect_immediately(auth, repo):
    token, _ = auth.login(EMAIL, PASSWORD)
    auth.logout(token)
    assert auth.authenticate(token) is None

    token, _ = auth.login(EMAIL, PASSWORD)
    auth.disable_user(USER)
    assert auth.authenticate(token) is None

    token, _ = auth.login(EMAIL, PASSWORD)
    repo.set_membership(USER, "another-workspace")
    assert auth.authenticate(token) is None


def test_unknown_or_empty_tokens_are_rejected(auth):
    assert auth.authenticate(None) is None and auth.authenticate("") is None
    assert auth.authenticate("not-a-real-token") is None


# ---- request dependency (test-only app; no product route is wired) -------------------------------
def test_require_principal_dependency(auth):
    app = FastAPI()

    @app.get("/protected")
    def protected(principal: Principal = Depends(require_principal)):
        return {"user_id": principal.user_id, "workspace_id": principal.workspace_id}

    app.dependency_overrides[get_auth_service] = lambda: auth
    client = TestClient(app)
    assert client.get("/protected").status_code == 401
    assert client.get("/protected").json()["detail"] == {"error": "UNAUTHENTICATED"}

    token, _ = auth.login(EMAIL, PASSWORD)
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/protected").json() == {"user_id": USER, "workspace_id": "demo"}
    auth.logout(token)
    assert client.get("/protected").status_code == 401


def test_cookie_settings_are_strict_by_default(monkeypatch):
    monkeypatch.delenv("LABEXTRACT_COOKIE_SECURE", raising=False)
    settings = session_cookie_settings()
    assert settings["httponly"] is True and settings["secure"] is True and settings["samesite"] == "lax"
    assert settings["max_age"] == 8 * 3600


# ---- identity providers ---------------------------------------------------------------------------
def test_local_provider_is_refused_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError):
        LocalAuthenticator({})


def test_supabase_provider(monkeypatch):
    calls = []

    def http(method, url, headers, body):
        calls.append((method, url, headers, json.loads(body)))
        password = json.loads(body)["password"]
        if password == PASSWORD:
            return 200, json.dumps({"access_token": "discarded", "user": {"id": USER}}).encode()
        if password == "down":
            return 503, b"{}"
        return 400, b'{"error":"invalid_grant"}'

    provider = SupabaseAuthenticator("https://synthetic.supabase.test", "synthetic-anon-key", http=http)
    assert provider.verify(EMAIL, PASSWORD) == USER
    assert provider.verify(EMAIL, "wrong") is None
    with pytest.raises(AuthUnavailable):
        provider.verify(EMAIL, "down")
    method, url, headers, body = calls[0]
    assert method == "POST" and url.endswith("/auth/v1/token?grant_type=password")
    assert PASSWORD not in url and headers["apikey"] == "synthetic-anon-key"
