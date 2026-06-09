# CLAUDE.md — Sandbox Orchestrator

> Loaded when `claude` runs in `containers/sandbox-orchestrator/`.

## What this is

The **sandbox-native successor to the Go `session-router`**. Where the router
listed warm pods labeled `app=agent-pod,state=warm`, patched one to
`state=bound`, and ran a reaper to recycle it, this service talks to the
upstream [agent-sandbox](https://agent-sandbox.sigs.k8s.io) project instead:

- A request comes in on `POST /v1/sandboxes` with `{dev_id, project_id, task}`.
- The orchestrator **creates/claims an agent Sandbox** from a `SandboxWarmPool`
  via the `k8s-agent-sandbox` Python SDK (`SandboxClient.create_sandbox`).
- It stages any input files, runs the agent command inside the sandbox
  (`sandbox.commands.run`), captures the result, and **always terminates** the
  sandbox in a `finally` (the controller-side TTL is the second safety net).
- The request record (state machine + result) is queryable on
  `GET /v1/sandboxes/{id}` until the reaper evicts it.

The win over warm-pod label-patching: lifecycle, isolation, and TTL are owned
by the agent-sandbox controller (a real CRD with status conditions), not by our
own bespoke reaper racing label patches against the kubelet.

## Why Python (not Go)

The session-router was Go. This service is Python because (a) the rest of the
app under `src/code_forge/` is Python, and (b) agent-sandbox ships a first-class
Python client. One language for the app, one obvious SDK.

## File layout

```
sandbox_orchestrator/
  __main__.py     # `python -m sandbox_orchestrator` → uvicorn boot
  api.py          # FastAPI app factory + HTTP surface (lifespan-managed)
  manager.py      # SandboxManager: admission control + lifecycle + reaper
  backends.py     # SandboxBackend abstraction: real SDK + FakeSandboxBackend
  models.py       # SandboxRequest / SandboxRecord / RequestState / ExecResult
  config.py       # env-driven Config (no static secrets — workload identity)
tests/
  test_orchestrator.py   # full lifecycle vs the fake backend (no cluster)
Dockerfile        # multi-stage, non-root UID 1000, read-only-rootfs friendly
requirements.txt  # fastapi, uvicorn, k8s-agent-sandbox
```

The chart resources (Deployment/Service/SA/RBAC/NetworkPolicy) live in
`charts/code-forge/templates/35-sandbox-orchestrator.yaml`, not here — same
convention as the model-gateway.

## HTTP surface

| Method | Path                     | Purpose                                  |
|--------|--------------------------|------------------------------------------|
| GET    | `/healthz`               | liveness/readiness (no backend calls)    |
| GET    | `/stats`                 | live concurrency + record counts         |
| POST   | `/v1/sandboxes`          | provision → run → teardown; returns record |
| GET    | `/v1/sandboxes`          | list records (`?dev_id=` filter)         |
| GET    | `/v1/sandboxes/{id}`     | fetch one record                         |
| DELETE | `/v1/sandboxes/{id}`     | cancel/terminate a request               |

`POST` is synchronous-by-design: it blocks until the sandbox run finishes, then
returns the full result. The blocking SDK calls run in a threadpool so the event
loop stays responsive; concurrency is bounded by the admission caps below.

## Build & run

```bash
# Unit tests — no cluster, no SDK needed (fake backend):
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

# Local run against the fake backend (no Kubernetes):
ORCHESTRATOR_BACKEND=fake python -m sandbox_orchestrator
curl -s localhost:8080/v1/sandboxes -d '{"dev_id":"me","task":"write a fn"}' | jq

# Container:
docker build -t code-forge/sandbox-orchestrator:dev .
```

## Configuration (env)

| Var                          | Default        | Meaning                                   |
|------------------------------|----------------|-------------------------------------------|
| `ORCHESTRATOR_BACKEND`       | `sdk`          | `sdk` (real SDK) or `fake` (in-mem)       |
| `SANDBOX_CONNECTION_MODE`    | `in-cluster`   | SDK connection config selector            |
| `SANDBOX_NAMESPACE`          | `agent-sandboxes` | namespace the sandboxes live in        |
| `SANDBOX_WARMPOOL`           | `python-sandbox-warmpool` | default `SandboxWarmPool` to claim from |
| `SANDBOX_TTL_SECONDS`        | `3600`         | controller-side sandbox TTL (safety net)  |
| `SANDBOX_COMMAND_TIMEOUT`    | `300`          | per-run command timeout                   |
| `AGENT_COMMAND_TEMPLATE`     | `claude -p {task}` | how a free-text task becomes a command |
| `MAX_CONCURRENT_SANDBOXES`   | `100`          | global admission cap → HTTP 429           |
| `MAX_CONCURRENT_PER_DEV`     | `3`            | per-dev admission cap → HTTP 429          |

## Editing rules

1. **Never add static cluster credentials.** The SDK authenticates in-cluster
   via the pod ServiceAccount. If you reach for a kubeconfig secret, you've
   broken the SA wiring — fix the RBAC, don't paper over it.
2. **Always terminate in `finally`.** A request that provisions a sandbox must
   tear it down even on crash. The TTL is a backstop, not the primary path.
3. **Shell-quote anything from the request.** `derive_command` runs
   `shlex.quote` on the task before substituting into `AGENT_COMMAND_TEMPLATE`.
   Never string-concat request input into a command. (There's a test for this.)
4. **Keep the fake backend in lockstep with the real one.** Every method on
   `SandboxBackend` must have a fake impl, or the tests stop proving the logic.

## Pitfalls

- **`429` on every request** → an admission cap is set too low, or slots are
  leaking. Check `GET /stats`; `active_total` should drop back to 0 once a run
  finishes. A non-zero idle `active_total` means a `finally` release was skipped.
- **`create_sandbox` hangs** → the `SandboxWarmPool` is empty or the controller
  can't schedule. The SDK blocks until the Sandbox reaches `Ready`. Tune the
  warmpool size or the command/connect timeouts.
- **SDK import error at boot** → `k8s-agent-sandbox` isn't installed. Tests
  don't need it (fake backend), but the real image does — it's in
  `requirements.txt`. `ORCHESTRATOR_BACKEND=fake` bypasses the import entirely.
- **`readOnlyRootFilesystem` write errors** → something is writing to disk. The
  service shouldn't; if a dep needs a scratch dir, mount an `emptyDir` at
  `/tmp` in the chart rather than relaxing the securityContext.
