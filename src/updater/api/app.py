"""The FastAPI application: routes, error mapping, CORS and the dashboard.

There is no authentication, because BUILD_SPEC.md section 7 rules it out of the
first version, so ``updater serve`` binds to localhost by default. CORS admits
localhost and 127.0.0.1 on any port, which is where a frontend dev server runs.
A page opened straight from disk sends ``Origin: null`` and is not admitted;
admitting ``null`` would admit every sandboxed page on the machine. Serve the
frontend over http instead.

Given a ``frontend`` directory, the app serves it at ``/`` beside the API, so
the dashboard and the API share one origin and need no CORS at all. The API's
routes are registered first, so ``/api/...``, ``/docs`` and ``/openapi.json``
always win over a file of the same name.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from updater.api.mock import MockState
from updater.api.policy import (
    Invalid,
    NotBuilt,
    NotFound,
    PolicyError,
    Refused,
    Unsupported,
)
from updater.api.routes import router

__all__ = ["LOCAL_ORIGINS", "create_app"]

LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

ERROR_STATUS: dict[type[PolicyError], int] = {
    NotFound: 404,
    Refused: 409,
    Unsupported: 415,
    Invalid: 422,
    NotBuilt: 501,
}


def _responder(status: int) -> Callable[[Request, Exception], JSONResponse]:
    def respond(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=status)

    return respond


def create_app(
    state: MockState | None = None, *, frontend: Path | None = None
) -> FastAPI:
    """The app over ``state``; with none, over a fresh state held in memory.

    Whoever opens a state that keeps a file closes it: ``updater serve`` does.
    ``frontend`` is a directory served at ``/``, ``index.html`` first; without
    one the app is the API alone, as it always was.
    """
    app = FastAPI(
        title="ASIL research update API",
        summary=(
            "Fixture-backed: the claims, sources and dashboard content are "
            "extracted from the approved dashboard prototype, and what people do "
            "with them (decisions, reviews, the audit log, runs, settings, source "
            "changes) is kept in SQLite. Nothing is fetched, extracted or "
            "published."
        ),
    )
    app.state.updater = state if state is not None else MockState()
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for error, status in ERROR_STATUS.items():
        app.add_exception_handler(error, _responder(status))
    app.include_router(router)
    if frontend is not None:
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
