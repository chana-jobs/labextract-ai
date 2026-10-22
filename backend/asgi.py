"""Same-origin deployment entry (D12, prepared; not wired): the API is served under /api and the
frontend's static build is served by the host at /. Switching the deployment to it is a one-line
change in the root app.py (`from backend.asgi import app`), pending approval."""
from fastapi import FastAPI

from backend.app import app as api

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/api", api)
