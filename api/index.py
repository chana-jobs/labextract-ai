"""Vercel serverless entry (Framework Preset "Other" builds Python functions from api/ only).
Every /api/* request is rewritten here (vercel.json); the ASGI app keeps the original path and
serves the API under /api. Same app as the root app.py used locally."""
import sys
from pathlib import Path

# make the project root importable whatever directory the runtime starts in
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.asgi import app  # noqa: E402,F401
