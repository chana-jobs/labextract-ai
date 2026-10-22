"""Vercel entrypoint: Vercel looks for a top-level `app` in app.py (FastAPI/ASGI).
The API is served under /api (backend.asgi); the frontend build is served as static files from /."""
from backend.asgi import app  # noqa: F401
