"""Vercel entrypoint: Vercel looks for a top-level `app` in app.py (FastAPI/ASGI)."""
from backend.app import app  # noqa: F401
