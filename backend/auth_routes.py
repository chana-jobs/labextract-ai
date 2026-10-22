"""Sign-in routes: POST /auth/login, POST /auth/logout, GET /auth/me."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from backend.auth import (
    SESSION_COOKIE, AuthService, AuthUnavailable, LoginFailed, Principal, RateLimited, get_auth_service,
    require_principal, session_cookie_settings,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginInput(BaseModel):
    email: str
    password: str


def _client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "")


def _principal_body(principal: Principal) -> dict:
    return {"user_id": principal.user_id, "workspace_id": principal.workspace_id}


@router.post("/login")
def login(data: LoginInput, request: Request, response: Response, auth: AuthService = Depends(get_auth_service)):
    try:
        token, principal = auth.login(data.email, data.password, client=_client(request))
    except LoginFailed:
        raise HTTPException(401, detail={"error": "INVALID_CREDENTIALS"})
    except RateLimited:
        raise HTTPException(429, detail={"error": "RATE_LIMITED"})
    except AuthUnavailable:
        raise HTTPException(503, detail={"error": "AUTH_UNAVAILABLE"})
    # a session cookie from before this sign-in is never reused
    auth.logout(request.cookies.get(SESSION_COOKIE))
    response.set_cookie(value=token, **session_cookie_settings())
    return _principal_body(principal)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, auth: AuthService = Depends(get_auth_service)):
    auth.logout(request.cookies.get(SESSION_COOKIE))
    settings = session_cookie_settings()
    response.delete_cookie(settings["key"], path=settings["path"], secure=settings["secure"],
                           httponly=True, samesite=settings["samesite"])


@router.get("/me")
def me(principal: Principal = Depends(require_principal)):
    return _principal_body(principal)
