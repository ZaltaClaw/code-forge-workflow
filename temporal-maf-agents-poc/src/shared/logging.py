"""Structured (JSON-line) logging plus a tiny health endpoint.

Every log line carries the observability fields the spec asks for
(``workflow_id``, ``run_id``, ``request_id``, ``agent_name``, ``stage``,
``status``, ``duration_ms``, ``error_type``) when they are available.

This module is imported by *activity* and *worker* code, not by workflow
code, so it is free to do I/O. (Workflows should use ``temporalio.workflow``'s
own logger, which already injects ``workflow_id`` / ``run_id``.)
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Canonical observability field order (also documented in the README).
LOG_FIELDS = (
    "workflow_id",
    "run_id",
    "request_id",
    "agent_name",
    "stage",
    "status",
    "duration_ms",
    "error_type",
)


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Promote any of the canonical fields that were passed via `extra=`.
        for key in LOG_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Health endpoint — used by the Kubernetes liveness/readiness probes.
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/healthz", "/readyz", "/health", "/"):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args: Any) -> None:  # silence default stderr spam
        return


def start_health_server(port: int) -> ThreadingHTTPServer:
    """Start a background health server and return it (call .shutdown() to stop)."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    return server
