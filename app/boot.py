"""Build the ASGI app for main.py; on a startup failure return a tiny app that
reports the error at any path instead of a bare platform 500. Secrets are
never echoed (exception text and traceback only)."""

from __future__ import annotations

import traceback
from typing import Any


def make_app() -> Any:
    try:
        from .main import app

        return app
    except Exception as exc:  # pragma: no cover - exercised only on broken deploys
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        tb = traceback.format_exc()[-3000:]
        err = f"{type(exc).__name__}: {exc}"
        fallback = FastAPI(title="Agent Guard (startup failed)")

        @fallback.get("/{path:path}")
        def _startup_error(path: str = "") -> JSONResponse:
            return JSONResponse({"ok": False, "startup_error": err, "traceback": tb}, 500)

        return fallback
