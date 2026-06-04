#!/usr/bin/env python3
"""Tiny health-check server for the agent pod.

GET /healthz       → 200 OK if the claude CLI is on PATH
GET /activity      → POST-only sentinel: bumps /workspace/.last-activity
                    so the idle watcher resets.
"""

from __future__ import annotations
import http.server
import os
import shutil
import socketserver
import time

LAST_ACTIVITY = "/workspace/.last-activity"


class Handler(http.server.BaseHTTPRequestHandler):
    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._ok(b"ok\n" if shutil.which("claude") else b"degraded\n")
        if self.path == "/readyz":
            return self._ok(b"ready\n")
        self.send_error(404)

    def do_POST(self):
        if self.path == "/activity":
            with open(LAST_ACTIVITY, "w") as f:
                f.write(str(int(time.time())))
            return self._ok(b"bumped\n")
        self.send_error(404)

    def log_message(self, *args, **kwargs):
        pass  # quiet probes


if __name__ == "__main__":
    port = int(os.environ.get("HEALTHZ_PORT", "8081"))
    with socketserver.TCPServer(("0.0.0.0", port), Handler) as srv:
        srv.serve_forever()
