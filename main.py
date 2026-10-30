"""Application entry point — FastAPI wiring only.

The HTTP surface is split into :mod:`routes` modules (one per feature area).
This module creates the application, mounts static assets, registers the
exception handlers that translate service-layer errors into clean JSON, and
includes every router. Business logic lives in :mod:`modules`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from modules import admin_service as adm
from modules import auth
from modules import config as cfg
from modules import db as db_mod
from modules import importer as imp
from modules import llm_client as llm
from modules import report_builder as rb

from routes import admin_data
from routes import admin_models
from routes import admin_packs
from routes import admin_users
from routes import analytics as analytics_routes
from routes import ask
from routes import auth as auth_routes
from routes import common
from routes import config as config_routes
from routes import graph
from routes import graph_studio
from routes import my_models
from routes import packs
from routes import pages
from routes import reports

BASE_DIR = cfg.BASE_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database once per process (idempotent).

    On a fresh checkout this creates the schema and seeds the built-in sample
    pack so the first-run administrator wizard works out of the box.

    Args:
        app (FastAPI): The application instance (provided by FastAPI).

    Yields:
        None: Control back to FastAPI for the lifetime of the process.
    """
    db_mod.init_db()
    yield


app = FastAPI(title=cfg.SYSTEM_NAME, version=cfg.VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------------------
# Error normalization — service-layer exceptions become clean JSON responses.
# ---------------------------------------------------------------------------


@app.exception_handler(auth.AuthError)
async def _auth_error(request: Request, exc: auth.AuthError) -> JSONResponse:
    """Map an :class:`modules.auth.AuthError` to its HTTP status code.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (auth.AuthError): The raised authentication error.

    Returns:
        JSONResponse: JSON body ``{"error": ...}`` with the error's status code.
    """
    return common._err(exc, exc.status_code)


@app.exception_handler(adm.AdminValueError)
async def _admin_value_error(
    request: Request, exc: adm.AdminValueError
) -> JSONResponse:
    """Map an :class:`modules.admin_service.AdminValueError` to a JSON body.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (adm.AdminValueError): The raised validation error.

    Returns:
        JSONResponse: JSON body ``{"error": ..., "conflicts": [...]}`` (the
        ``conflicts`` key is only present when the exception carries conflicts).
    """
    body: Dict[str, Any] = {"error": exc.args[0] if exc.args else "Invalid request."}
    if getattr(exc, "conflicts", None):
        body["conflicts"] = exc.conflicts
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(adm.AdminConflict)
async def _admin_conflict(request: Request, exc: adm.AdminConflict) -> JSONResponse:
    """Map an :class:`modules.admin_service.AdminConflict` to HTTP 409 JSON.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (adm.AdminConflict): The raised conflict error.

    Returns:
        JSONResponse: HTTP 409 body ``{"error": ..., "conflicts": [...]}``.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.args[0] if exc.args else "Conflict.",
            "conflicts": getattr(exc, "conflicts", []),
        },
    )


@app.exception_handler(llm.ModelConfigError)
async def _model_error(request: Request, exc: llm.ModelConfigError) -> JSONResponse:
    """Map a model-configuration error to its HTTP status code.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (llm.ModelConfigError): The raised model-configuration error.

    Returns:
        JSONResponse: JSON body ``{"error": ...}`` with the error's status code.
    """
    return common._err(exc, exc.status_code)


@app.exception_handler(imp.UploadError)
async def _upload_error(request: Request, exc: imp.UploadError) -> JSONResponse:
    """Map an upload/import error to its HTTP status code.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (imp.UploadError): The raised upload/import error.

    Returns:
        JSONResponse: JSON body ``{"error": ...}`` with the error's status code.
    """
    return common._err(exc, exc.status_code)


@app.exception_handler(rb.ReportError)
async def _report_error(request: Request, exc: rb.ReportError) -> JSONResponse:
    """Map a report-generation error to its HTTP status code.

    Args:
        request (Request): The incoming request (unused, provided by FastAPI).
        exc (rb.ReportError): The raised report-generation error.

    Returns:
        JSONResponse: JSON body ``{"error": ...}`` with the error's status code.
    """
    return common._err(exc, exc.status_code)


# ---------------------------------------------------------------------------
# Routers — registration order mirrors the original single-file layout.
# ---------------------------------------------------------------------------

_ROUTERS = (
    pages,
    auth_routes,
    packs,
    config_routes,
    graph,
    analytics_routes,
    ask,
    reports,
    admin_packs,
    admin_data,
    admin_models,
    admin_users,
    graph_studio,
    my_models,
)

for _router_module in _ROUTERS:
    app.include_router(_router_module.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.HOST, port=cfg.PORT)
