"""Request-level protections applied to every route.

- State-changing requests (POST/PUT/PATCH/DELETE) from another site are refused: the Origin
  header must match this deployment (or LABEXTRACT_ALLOWED_ORIGINS); without Origin, a browser's
  Sec-Fetch-Site: cross-site is refused. Together with the SameSite=Lax session cookie this
  blocks cross-site request forgery, including forged sign-in.
- Responses are never cached and are not sniffed or framed by other sites.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _allowed_origins(request: Request) -> set[str]:
    configured = {o.strip().rstrip("/") for o in os.environ.get("LABEXTRACT_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return configured | {f"{scheme}://{host}"}


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        return f"{parts.scheme}://{parts.netloc}" in _allowed_origins(request)
    # no Origin header: refuse only what a browser marks as cross-site
    return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in UNSAFE_METHODS and not origin_allowed(request):
            return JSONResponse({"detail": {"error": "ORIGIN_NOT_ALLOWED"}}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response
