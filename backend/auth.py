"""Sign-in, server-side sessions and request authorization (internal; no routes are exposed here).

- Passwords are verified by an identity provider: Supabase Auth in the cloud, or local scrypt
  hashes for development and tests. Plaintext passwords are never stored.
- A session is an opaque random token in an HttpOnly cookie; only its SHA-256 is stored, so
  logout, expiry and disabling a user take effect on the server immediately.
- Failure messages never reveal whether an account or a workspace exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Protocol

from fastapi import Depends, HTTPException, Request

from backend.storage import Repository, Session, get_repository

SESSION_COOKIE = "lx_session"
SESSION_TTL = timedelta(hours=8)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ password hashes (local provider)
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password_hash(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


_DUMMY_HASH = hash_password("dummy-password-for-constant-time")


# ------------------------------------------------------------------ identity providers
class AuthUnavailable(Exception):
    """The identity provider could not be reached; the user is told to retry, not that the password failed."""


class Authenticator(Protocol):
    def verify(self, email: str, password: str) -> str | None:
        """Return the user id when the credentials are valid, else None."""


class LocalAuthenticator:
    """Development and tests only. Refuses to run on Vercel."""

    def __init__(self, users: dict[str, dict]):
        if os.environ.get("VERCEL"):
            raise RuntimeError("The local identity provider is not allowed on Vercel")
        self._users = {email.strip().lower(): user for email, user in users.items()}

    @classmethod
    def from_env(cls) -> "LocalAuthenticator":
        # LABEXTRACT_DEV_USERS = {"<email>": {"user_id": "...", "password_hash": "scrypt$..."}}
        return cls(json.loads(os.environ.get("LABEXTRACT_DEV_USERS", "{}")))

    def verify(self, email, password):
        user = self._users.get(email.strip().lower())
        # same work whether or not the account exists (no timing hint)
        ok = verify_password_hash(password, user["password_hash"] if user else _DUMMY_HASH)
        return user["user_id"] if user and ok else None


Http = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


class SupabaseAuthenticator:
    """Checks email + password against Supabase Auth. The provider's tokens are discarded."""

    def __init__(self, url: str, anon_key: str, http: Http | None = None):
        from backend.supabase_repo import urllib_http
        self.url = url.rstrip("/")
        self._key = anon_key
        self._http = http or urllib_http

    @classmethod
    def from_env(cls) -> "SupabaseAuthenticator":
        try:
            return cls(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
        except KeyError as missing:
            raise RuntimeError(f"Missing environment variable {missing}") from None

    def verify(self, email, password):
        url = f"{self.url}/auth/v1/token?" + urllib.parse.urlencode({"grant_type": "password"})
        body = json.dumps({"email": email.strip(), "password": password}).encode("utf-8")
        try:
            status, raw = self._http("POST", url, {"apikey": self._key, "Content-Type": "application/json"}, body)
        except Exception:
            raise AuthUnavailable() from None
        if status == 200:
            return json.loads(raw)["user"]["id"]
        if status in (400, 401, 403, 422):
            return None
        raise AuthUnavailable()


# ------------------------------------------------------------------ rate limiting
class LoginRateLimiter:
    """At most `max_failures` failed sign-ins per (client, email) per window.

    In-process only: on serverless each instance counts separately. Supabase Auth applies its own
    limits in the cloud; this is the first line, not the only one.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 300, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self._clock = clock
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key) -> list[float]:
        cutoff = self._clock() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def allowed(self, client: str, email: str) -> bool:
        with self._lock:
            return len(self._recent((client, email.strip().lower()))) < self.max_failures

    def record_failure(self, client: str, email: str) -> None:
        with self._lock:
            self._recent((client, email.strip().lower())).append(self._clock())

    def reset(self, client: str, email: str) -> None:
        with self._lock:
            self._failures.pop((client, email.strip().lower()), None)


# ------------------------------------------------------------------ sessions
class LoginFailed(Exception):
    """Wrong credentials, unknown account or no workspace: deliberately indistinguishable."""


class RateLimited(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str


class AuthService:
    def __init__(self, repo: Repository, authenticator: Authenticator, *, ttl: timedelta = SESSION_TTL,
                 clock: Callable[[], datetime] = _now, limiter: LoginRateLimiter | None = None):
        self.repo = repo
        self.authenticator = authenticator
        self.ttl = ttl
        self._clock = clock
        self.limiter = limiter or LoginRateLimiter()

    def login(self, email: str, password: str, client: str = "") -> tuple[str, Principal]:
        if not self.limiter.allowed(client, email):
            raise RateLimited()
        user_id = self.authenticator.verify(email, password) if email and password else None
        workspace_id = self.repo.get_membership(user_id) if user_id else None
        if user_id is None or workspace_id is None:
            self.limiter.record_failure(client, email)
            raise LoginFailed()
        self.limiter.reset(client, email)
        token = secrets.token_urlsafe(32)
        self.repo.create_session(token_hash(token), Session(user_id, workspace_id, self._clock() + self.ttl))
        return token, Principal(user_id, workspace_id)

    def authenticate(self, token: str | None) -> Principal | None:
        if not token:
            return None
        session = self.repo.get_session(token_hash(token))
        if session is None or session.revoked or session.expires_at <= self._clock():
            return None
        # membership is checked on every request: removing a user from a workspace is immediate
        if self.repo.get_membership(session.user_id) != session.workspace_id:
            return None
        return Principal(session.user_id, session.workspace_id)

    def logout(self, token: str | None) -> None:
        if token:
            self.repo.revoke_session(token_hash(token))

    def disable_user(self, user_id: str) -> None:
        self.repo.revoke_user_sessions(user_id)


@lru_cache
def _authenticator() -> Authenticator:
    provider = os.environ.get("LABEXTRACT_AUTH", "local")
    if provider == "supabase":
        return SupabaseAuthenticator.from_env()
    if provider == "local":
        return LocalAuthenticator.from_env()
    raise RuntimeError(f"Unknown LABEXTRACT_AUTH: {provider}")


def get_authenticator() -> Authenticator:
    return _authenticator()


_limiter = LoginRateLimiter()


def get_auth_service(repo: Repository = Depends(get_repository),
                     authenticator: Authenticator = Depends(get_authenticator)) -> AuthService:
    return AuthService(repo, authenticator, limiter=_limiter)


def require_principal(request: Request, auth: AuthService = Depends(get_auth_service)) -> Principal:
    """Every protected route: the user and the workspace come from the verified session only."""
    principal = auth.authenticate(request.cookies.get(SESSION_COOKIE))
    if principal is None:
        raise HTTPException(401, detail={"error": "UNAUTHENTICATED"})
    return principal


def current_workspace(principal: Principal = Depends(require_principal)) -> str:
    return principal.workspace_id


def check_deployment_config() -> None:
    """Fail closed in production (Vercel or the container): no local storage, no local accounts, no insecure cookie."""
    if not (os.environ.get("VERCEL") or os.environ.get("LABEXTRACT_ENV") == "production"):
        return
    problems = []
    if os.environ.get("LABEXTRACT_STORAGE") != "supabase":
        problems.append("LABEXTRACT_STORAGE must be 'supabase'")
    if os.environ.get("LABEXTRACT_AUTH") != "supabase":
        problems.append("LABEXTRACT_AUTH must be 'supabase'")
    if os.environ.get("LABEXTRACT_COOKIE_SECURE", "true") == "false":
        problems.append("LABEXTRACT_COOKIE_SECURE must not be 'false'")
    if problems:
        raise RuntimeError("Unsafe deployment configuration: " + "; ".join(problems))


def session_cookie_settings(ttl: timedelta = SESSION_TTL) -> dict:
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "secure": os.environ.get("LABEXTRACT_COOKIE_SECURE", "true") != "false",
        "samesite": "lax",
        "path": "/",
        "max_age": int(ttl.total_seconds()),
    }
