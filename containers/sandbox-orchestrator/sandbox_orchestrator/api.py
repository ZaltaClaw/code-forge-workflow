"""FastAPI application — the orchestrator's HTTP front door.

Endpoints (mirrors the session-router's surface, sandbox-native semantics):

* ``GET  /healthz``        — liveness/readiness probe (no backend calls).
* ``GET  /stats``          — current concurrency + record counts.
* ``POST /v1/sandboxes``   — submit a request; provision a sandbox, run, return.
* ``GET  /v1/sandboxes``   — list request records (optional ``?dev_id=``).
* ``GET  /v1/sandboxes/{id}``    — fetch one record.
* ``DELETE /v1/sandboxes/{id}``  — cancel/terminate a request.

The blocking lifecycle (``manager.handle_request``) is dispatched to a
threadpool so the event loop stays responsive under concurrent load.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .backends import build_backend
from .config import Config
from .manager import CapacityError, NotFoundError, SandboxManager
from .models import SandboxRequest

logger = logging.getLogger("sandbox_orchestrator.api")


def create_app(config: Config | None = None, manager: SandboxManager | None = None) -> FastAPI:
    """Application factory.

    ``config``/``manager`` are injectable so tests can wire a fake-backed
    manager without environment variables or a real cluster.
    """
    config = config or Config.from_env()
    if manager is None:
        backend = build_backend(config)
        manager = SandboxManager(config, backend)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start_reaper()
        logger.info(
            "orchestrator up: backend=%s connection_mode=%s namespace=%s",
            config.backend, config.connection_mode, config.sandbox_namespace,
        )
        try:
            yield
        finally:
            manager.stop_reaper()

    app = FastAPI(
        title="Code Forge — Sandbox Orchestrator",
        version="0.1.0",
        summary="Provisions an agent sandbox per request via the agent-sandbox SDK.",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.manager = manager

    # -- probes -----------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/stats")
    def stats() -> dict:
        return manager.stats()

    # -- core API ---------------------------------------------------------

    @app.post("/v1/sandboxes")
    async def create_sandbox(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid JSON body")
        try:
            req = SandboxRequest.from_dict(body)
        except ValueError as exc:
            return _error(400, str(exc))

        try:
            record = await run_in_threadpool(manager.handle_request, req)
        except CapacityError as exc:
            return _error(429, str(exc))

        status = 201 if record.state.value in {"succeeded"} else 200
        return JSONResponse(status_code=status, content=record.to_public_dict())

    @app.get("/v1/sandboxes")
    def list_sandboxes(dev_id: str | None = Query(default=None)) -> dict:
        records = manager.list_records(dev_id=dev_id)
        return {"sandboxes": [r.to_public_dict() for r in records], "count": len(records)}

    @app.get("/v1/sandboxes/{request_id}")
    def get_sandbox(request_id: str) -> Response:
        try:
            record = manager.get(request_id)
        except NotFoundError:
            return _error(404, f"unknown request_id: {request_id}")
        return JSONResponse(content=record.to_public_dict())

    @app.delete("/v1/sandboxes/{request_id}")
    def cancel_sandbox(request_id: str) -> Response:
        try:
            record = manager.cancel(request_id)
        except NotFoundError:
            return _error(404, f"unknown request_id: {request_id}")
        return JSONResponse(content=record.to_public_dict())

    return app


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})
