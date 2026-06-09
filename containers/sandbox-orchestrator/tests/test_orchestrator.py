"""End-to-end tests for the sandbox orchestrator.

These drive the *full* request lifecycle through the FastAPI app against the
in-memory :class:`FakeSandboxBackend` — no Kubernetes, no SDK, no network. They
prove admission control, the state machine, sandbox teardown, and the HTTP
contract all hold together.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sandbox_orchestrator.api import create_app
from sandbox_orchestrator.backends import FakeSandboxBackend, derive_command
from sandbox_orchestrator.config import Config
from sandbox_orchestrator.manager import SandboxManager
from sandbox_orchestrator.models import ExecResult, RequestState, SandboxRequest


def make_app(backend=None, **cfg_overrides):
    config = Config(backend="fake", connection_mode="in-cluster", **cfg_overrides)
    backend = backend or FakeSandboxBackend()
    manager = SandboxManager(config, backend)
    app = create_app(config=config, manager=manager)
    return app, manager, backend


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_rejects_bad_template():
    with pytest.raises(ValueError):
        Config(agent_command_template="no placeholder here").validate()


def test_config_direct_mode_requires_url():
    with pytest.raises(ValueError):
        Config(connection_mode="direct", router_api_url="").validate()


def test_config_from_env_roundtrip(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_BACKEND", "fake")
    monkeypatch.setenv("SANDBOX_WARMPOOL", "my-pool")
    monkeypatch.setenv("MAX_CONCURRENT_PER_DEV", "7")
    cfg = Config.from_env()
    assert cfg.backend == "fake"
    assert cfg.default_warmpool == "my-pool"
    assert cfg.max_concurrent_per_dev == 7


# ---------------------------------------------------------------------------
# Command derivation
# ---------------------------------------------------------------------------


def test_derive_command_prefers_explicit():
    cfg = Config()
    assert derive_command(cfg, task="ignored", command="ls -la") == "ls -la"


def test_derive_command_quotes_task():
    cfg = Config(agent_command_template="claude -p {task}")
    cmd = derive_command(cfg, task="build a thing; rm -rf /", command="")
    # The malicious-looking task must be a single shell-quoted token.
    assert "claude -p " in cmd
    assert "'build a thing; rm -rf /'" in cmd


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_request_validation():
    with pytest.raises(ValueError):
        SandboxRequest(dev_id="").validate()
    with pytest.raises(ValueError):
        SandboxRequest(dev_id="alice").validate()  # no task or command


def test_request_from_dict_rejects_unknown_fields():
    with pytest.raises(ValueError):
        SandboxRequest.from_dict({"dev_id": "a", "task": "x", "bogus": 1})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_successful_run_full_lifecycle():
    app, manager, backend = make_app()
    client = TestClient(app)

    resp = client.post("/v1/sandboxes", json={"dev_id": "alice", "task": "do work"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == RequestState.SUCCEEDED.value
    assert body["result"]["exit_code"] == 0
    assert body["sandbox_id"].startswith("sbx-")

    # Sandbox was created AND torn down.
    assert len(backend.created) == 1
    assert backend.terminated_ids == [body["sandbox_id"]]

    # Slot was released.
    assert manager.stats()["active_total"] == 0


def test_files_are_staged_before_run():
    captured = {}

    def runner(command, files):
        captured.update(files)
        return ExecResult(stdout="ok", exit_code=0)

    backend = FakeSandboxBackend(runner=runner)
    app, _, _ = make_app(backend=backend)
    client = TestClient(app)

    resp = client.post("/v1/sandboxes", json={
        "dev_id": "bob",
        "command": "run tests",
        "files": {"main.py": "print(1)", "spec.md": "# spec"},
    })
    assert resp.status_code == 201
    assert captured == {"main.py": "print(1)", "spec.md": "# spec"}


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_nonzero_exit_marks_failed():
    backend = FakeSandboxBackend(
        runner=lambda cmd, files: ExecResult(stdout="", stderr="boom", exit_code=2)
    )
    app, _, backend = make_app(backend=backend)
    client = TestClient(app)

    resp = client.post("/v1/sandboxes", json={"dev_id": "carol", "command": "false"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == RequestState.FAILED.value
    assert body["result"]["exit_code"] == 2
    # Even on failure the sandbox is reaped.
    assert len(backend.terminated_ids) == 1


def test_provisioning_failure_is_handled_and_cleaned_up():
    backend = FakeSandboxBackend(fail_warmpools={"broken-pool"})
    app, manager, backend = make_app(backend=backend)
    client = TestClient(app)

    resp = client.post("/v1/sandboxes", json={
        "dev_id": "dave", "task": "x", "warmpool": "broken-pool",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == RequestState.FAILED.value
    assert "broken-pool" in body["error"]
    # No sandbox came up, nothing to terminate, slot released.
    assert backend.terminated_ids == []
    assert manager.stats()["active_total"] == 0


def test_terminate_failure_does_not_break_request():
    class FlakyTerminate(FakeSandboxBackend):
        def terminate(self, handle):
            raise RuntimeError("k8s api flake")

    app, manager, _ = make_app(backend=FlakyTerminate())
    client = TestClient(app)
    resp = client.post("/v1/sandboxes", json={"dev_id": "erin", "command": "ok"})
    # Run still succeeds; teardown error is swallowed (TTL reaps the sandbox).
    assert resp.status_code == 201
    assert resp.json()["state"] == RequestState.SUCCEEDED.value
    assert manager.stats()["active_total"] == 0


# ---------------------------------------------------------------------------
# Admission control
# ---------------------------------------------------------------------------


def test_per_dev_concurrency_cap():
    # Block inside run() so the slot stays held while we fire a second request.
    import threading
    gate = threading.Event()

    def runner(command, files):
        gate.wait(timeout=5)
        return ExecResult(exit_code=0)

    backend = FakeSandboxBackend(runner=runner)
    app, manager, _ = make_app(backend=backend, max_concurrent_per_dev=1)
    client = TestClient(app)

    results = {}

    def fire(key):
        results[key] = client.post("/v1/sandboxes", json={"dev_id": "sam", "command": "x"})

    t1 = threading.Thread(target=fire, args=("first",))
    t1.start()
    # Wait until the first request has actually grabbed the slot.
    for _ in range(500):
        if manager.stats()["active_total"] == 1:
            break
        import time as _t; _t.sleep(0.005)

    second = client.post("/v1/sandboxes", json={"dev_id": "sam", "command": "y"})
    assert second.status_code == 429
    assert "per-dev" in second.json()["error"]

    gate.set()
    t1.join(timeout=5)
    assert results["first"].status_code == 201


def test_global_concurrency_cap_isolated_from_per_dev():
    import threading
    gate = threading.Event()

    def runner(command, files):
        gate.wait(timeout=5)
        return ExecResult(exit_code=0)

    backend = FakeSandboxBackend(runner=runner)
    app, manager, _ = make_app(
        backend=backend, max_concurrent_sandboxes=1, max_concurrent_per_dev=5
    )
    client = TestClient(app)

    def fire():
        client.post("/v1/sandboxes", json={"dev_id": "u1", "command": "x"})

    t = threading.Thread(target=fire); t.start()
    for _ in range(500):
        if manager.stats()["active_total"] == 1:
            break
        import time as _t; _t.sleep(0.005)

    # Different dev, but global cap of 1 is saturated.
    resp = client.post("/v1/sandboxes", json={"dev_id": "u2", "command": "y"})
    assert resp.status_code == 429
    assert "global" in resp.json()["error"]
    gate.set(); t.join(timeout=5)


# ---------------------------------------------------------------------------
# Query + cancel surface
# ---------------------------------------------------------------------------


def test_get_list_and_cancel():
    app, _, _ = make_app()
    client = TestClient(app)

    created = client.post("/v1/sandboxes", json={"dev_id": "z", "command": "x"}).json()
    rid = created["request_id"]

    got = client.get(f"/v1/sandboxes/{rid}")
    assert got.status_code == 200
    assert got.json()["request_id"] == rid

    listed = client.get("/v1/sandboxes", params={"dev_id": "z"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    # Already-terminal records aren't moved to TERMINATED by cancel.
    cancelled = client.delete(f"/v1/sandboxes/{rid}")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == RequestState.SUCCEEDED.value

    assert client.get("/v1/sandboxes/req-doesnotexist").status_code == 404
    assert client.delete("/v1/sandboxes/req-doesnotexist").status_code == 404


def test_healthz_and_stats():
    app, _, _ = make_app()
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    stats = client.get("/stats").json()
    assert stats["active_total"] == 0
    assert stats["max_concurrent_per_dev"] >= 1


def test_invalid_body_returns_400():
    app, _, _ = make_app()
    client = TestClient(app)
    assert client.post("/v1/sandboxes", json={"task": "no dev id"}).status_code == 400
    bad = client.post("/v1/sandboxes", content=b"not json",
                      headers={"content-type": "application/json"})
    assert bad.status_code == 400
