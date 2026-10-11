"""Vercel serverless entrypoint.

Vercel's Python runtime looks for a module under `api/` exporting an ASGI
callable named `app`. Everything else lives in the `app/` package so the same
codebase runs unchanged under `uvicorn`, Docker, or Vercel.
"""
from app.main import app

__all__ = ["app"]
