# Temporal + Microsoft Agent Framework on AKS

A production-style POC where:

- **Temporal** is the durable orchestration layer (the single orchestration authority).
- **Microsoft Agent Framework (MAF)** is the agent runtime — used **only inside Temporal activities**.
- **AKS** hosts one worker Deployment per task queue.
- **KEDA** scales each worker from its Temporal task-queue backlog (including scale-to-zero).

> **Phase 1 (default, `AGENT_MODE=mock`)** proves the whole topology — parent
> workflow, child workflows, task queues, retries, approval gate, KEDA scaling,
> MAF integration seam — with **no cloud credentials**. **Phase 2
> (`AGENT_MODE=live`)** swaps the mocks for real Azure OpenAI / GitHub /
> Kubernetes / MCP calls *inside the activities only* (TODO stubs are in place).

---

## Architecture

```
User Request
     │
     ▼
AgentOrchestratorWorkflow        (parent, task queue: orchestrator-tq)
     │  executes child workflows in sequence, passing each stage's output forward
     ├─▶ PlannerAgentWorkflow     (planner-agent-tq)   ─▶ RunPlannerAgentActivity  ─▶ PlannerAgent (MAF)
     ├─▶ GitHubAgentWorkflow      (github-agent-tq)    ─▶ RunGitHubAgentActivity   ─▶ GitHubAgent (MAF)
     ├─▶ AKSAgentWorkflow         (aks-agent-tq)       ─▶ RunAKSAgentActivity      ─▶ AKSAgent (MAF)
     └─▶ ApprovalAgentWorkflow    (approval-agent-tq)  ─▶ RunApprovalAgentActivity ─▶ ApprovalAgent (MAF)
                                   │ durable human-in-the-loop wait (signal or auto-approve)
                                   ▼
                            Final OrchestrationResult
```

### The determinism rule (enforced by the Temporal sandbox)

Workflow code (`*/workflows.py`, `shared/contracts.py`, `shared/config.py`,
`shared/child.py`) is **deterministic** and never calls:

- LLMs / Azure OpenAI
- GitHub APIs
- Kubernetes / AKS APIs
- Microsoft Agent Framework tools

All of that happens in **activities** (`*/activities.py` → `shared/maf.py`).
The agent runtime is reached only through the activity boundary, so Temporal
stays the single orchestration authority. We deliberately do **not** use Agent
Framework's own Durable Workflows as the orchestration layer.

---

## Repository layout

```
temporal-maf-agents-poc/
├── docker-compose.yaml          # Temporal + UI + namespace bootstrap + 5 workers
├── Dockerfile                   # one shared image; WORKER_MODULE selects the worker
├── sample-input.json            # example orchestration request
├── sample-output.json           # example orchestration result (mock run)
├── pyproject.toml / requirements.txt
├── Makefile
├── src/
│   ├── shared/                  # contracts, config, logging, runtime, child helper, MAF seam
│   ├── orchestrator_worker/     # parent workflow + worker (orchestrator-tq)
│   ├── planner_agent_worker/    # agent + activity + child workflow + worker
│   ├── github_agent_worker/
│   ├── aks_agent_worker/
│   ├── approval_agent_worker/
│   └── starter.py               # kicks off an orchestration
├── tests/                       # unit tests + end-to-end Temporal time-skipping test
└── k8s/
    ├── namespace.yaml           # agent-platform namespace + shared ConfigMap
    ├── deployments/             # one Deployment per task queue (5)
    └── keda/                    # one ScaledObject per agent task queue (4)
```

---

## Contracts

### Orchestration input (`sample-input.json`)

```json
{
  "request_id": "req-001",
  "goal": "Add a /healthz endpoint to the payments service and deploy it",
  "repo_url": "https://github.com/example-org/payments-service",
  "environment": "dev",
  "approval_required": true
}
```

### Agent output contract

Every agent returns this structure (`shared/contracts.py:AgentOutput`):

```json
{
  "agent_name": "planner",
  "stage": "planning",
  "status": "success",
  "retryable": false,
  "summary": "deployment plan created",
  "details": {},
  "next_action": "continue"
}
```

- `status` ∈ `success | failed | needs_approval`
- `next_action` ∈ `continue | retry | fail | rollback | ask_human`

### Retry & control-flow policy

Two layers, both implemented (see `shared/child.py`):

| Layer | Trigger | Policy |
|-------|---------|--------|
| 1. Temporal activity retries | activity **raises** (transient infra error) | `initial_interval=10s`, `backoff_coefficient=2`, `maximum_interval=120s`, `maximum_attempts=3` |
| 2. Workflow business policy | activity **returns** a structured result | `failed + retryable=true` → re-run · `failed + retryable=false` → fail workflow · `needs_approval` → durable wait for approval |

The pure decision function is `shared.contracts.decide()` (unit-tested).

---

## Run it locally (docker-compose)

Prereqs: Docker + Docker Compose.

```bash
cd temporal-maf-agents-poc

# 1. Start Temporal (with Postgres), create the `agent-platform` namespace,
#    and launch all five workers. Temporal UI: http://localhost:8233
docker compose up --build

# 2. In another shell, install the client deps and start one orchestration.
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m starter            # uses sample-input.json
```

You'll see the final `OrchestrationResult` printed (compare with
`sample-output.json`). Open the **Temporal UI** to watch the parent workflow,
the four child workflows on their task queues, the activities, and the approval
timer.

### Run workers without Docker

Point the workers at any reachable Temporal (e.g. `temporal server start-dev`
on `localhost:7233`, then create the namespace:
`temporal operator namespace create --namespace agent-platform`):

```bash
. .venv/bin/activate && pip install -r requirements.txt -e .
export TEMPORAL_ADDRESS=localhost:7233 TEMPORAL_NAMESPACE=agent-platform
make worker-orchestrator   # in 5 terminals: -orchestrator -planner -github -aks -approval
make run                   # start an orchestration
```

### Demonstrate the retry policy

```bash
# Make the planner activity fail once; Temporal's RetryPolicy re-runs it.
FORCE_TRANSIENT_ERROR=1 python -m planner_agent_worker.worker
```

### Drive the approval gate manually

Set `TEMPORAL_APPROVAL_AUTO=false` so the approval workflow blocks on a human
decision, then signal it (workflow id is `<request_id>-approval`):

```bash
temporal workflow signal \
  --workflow-id req-001-approval \
  --name submit_decision \
  --input 'true' --input '"LGTM"'
```

With the default `TEMPORAL_APPROVAL_AUTO=true`, the gate auto-approves after
`TEMPORAL_APPROVAL_TIMEOUT_SECONDS` so the POC completes end-to-end unattended.

---

## Tests

```bash
. .venv/bin/activate
pip install -r requirements.txt -e ".[dev]"
PYTHONPATH=src pytest -q
```

- `test_contracts.py`, `test_agents.py` — pure, no server.
- `test_workflow_integration.py` — runs the **full parent → 4 children**
  pipeline on Temporal's in-memory time-skipping test server (auto-downloads a
  test binary on first run; skips if unavailable). Covers the happy path
  (auto-approve) and an approval **rejection** that fails the gate.

---

## Deploy to AKS

Prereqs: an AKS cluster, a Temporal deployment reachable in-cluster
(Temporal Helm chart or Temporal Cloud), and KEDA ≥ 2.17 installed
(`helm install keda kedacore/keda -n keda --create-namespace`).

```bash
# 1. Build and push the shared worker image to your registry (e.g. ACR).
az acr build -r <youracr> -t temporal-maf-agents-poc:latest .
# then set that image ref in k8s/deployments/*.yaml (replace temporal-maf-agents-poc:latest)

# 2. Namespace + shared config. Edit k8s/namespace.yaml first so TEMPORAL_ADDRESS
#    points at your Temporal frontend Service (default assumes the Temporal Helm
#    chart at temporal-frontend.temporal.svc.cluster.local:7233).
kubectl apply -f k8s/namespace.yaml

# 3. Worker Deployments (one per task queue).
kubectl apply -f k8s/deployments/

# 4. KEDA ScaledObjects (one per agent task queue) — scale 0..10 from backlog.
kubectl apply -f k8s/keda/

# 5. Create the Temporal namespace if it doesn't exist yet.
#    (from a temporal admin-tools pod / your machine)
temporal operator namespace create --namespace agent-platform
```

Notes:

- The **orchestrator** Deployment is fixed at 1 replica (it must always be
  available to host the parent workflow). The four **agent** Deployments are
  owned by KEDA — they scale to zero when idle and wake when their task queue
  has backlog (`endpoint`, `namespace`, `taskQueue`, `targetQueueSize` in each
  `k8s/keda/*-scaledobject.yaml`).
- **Temporal Cloud**: set `TEMPORAL_ADDRESS` to `<ns>.<acct>.tmprl.cloud:7233`
  and add mTLS / API-key auth to both the workers and the KEDA ScaledObjects
  (via a `TriggerAuthentication` — see the TODO in `k8s/keda/*.yaml`).

---

## Observability

Workers emit structured JSON logs (`shared/logging.py`) carrying the spec's
fields where available: `workflow_id`, `run_id`, `request_id`, `agent_name`,
`stage`, `status`, `duration_ms`, `error_type`. Each worker also serves a
health endpoint on `:8080` (`/healthz`, `/readyz`) used by the k8s probes.

Suggested metrics to scrape next (Temporal SDK + KEDA both export Prometheus):
`workflow_duration`, `activity_duration`, `retry_count`, `task_queue_backlog`,
`worker_replicas`, `approval_wait_time`, `failed_workflows`.

---

## Phase 2 — going live (Azure OpenAI + GitHub)

The planner, github, and approval agents run real Azure OpenAI reasoning (via
Microsoft Agent Framework) and the github agent makes real GitHub writes. The
AKS agent stays mock.

1. Install live deps: `pip install -r requirements-live.txt` (or `pip install -e ".[live]"`).
2. Set env (see `.env.example`):
   - `AGENT_MODE=live`
   - `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`
   - Azure auth: set `AZURE_OPENAI_API_KEY`, **or** leave it unset and use
     `DefaultAzureCredential` (`az login` locally / workload identity on AKS).
   - `GITHUB_TOKEN` (fine-grained PAT) and `GITHUB_ALLOWED_OWNER` (the github
     agent refuses to write unless the target repo's owner matches — fail-closed).
3. Run workers + `python -m starter` as in Phase 1.

What the github agent does: creates branch `feat/<request_id>`, commits
`docs/agent-plan-<request_id>.md`, and opens a PR. All writes are idempotent, so
Temporal activity retries converge instead of duplicating.

Error handling: transient Azure/GitHub errors (5xx, rate limit, timeout, or a
schema-invalid model response) are raised and retried by Temporal's activity
retry policy; permanent errors (auth, missing repo, validation, guard violation)
fail the workflow without pointless retries.

Live on AKS: build the live image with `--build-arg INSTALL_LIVE=true`, apply
`k8s/secrets/live-agents-secret.yaml`, and use the updated planner/github/approval
deployments (which set `AGENT_MODE=live` and mount the secret).

Tests: `pytest` runs everything with mocked clients (no creds). The opt-in live
smoke test runs only with `RUN_LIVE_SMOKE=1` + real Azure env.

---

## Acceptance criteria — status

- [x] Local Temporal starts (docker-compose)
- [x] Parent workflow executes
- [x] Child workflows execute (one per task queue)
- [x] Activities invoke mocked Microsoft Agent Framework agents
- [x] Retry logic works (both layers; `FORCE_TRANSIENT_ERROR` demo + soft-retry)
- [x] Structured outputs work (validated `AgentOutput` contract)
- [x] AKS manifests exist (`k8s/deployments/`)
- [x] KEDA manifests exist (`k8s/keda/`)
- [x] README with local and AKS deployment instructions
- [x] Phase 2 real integrations — Azure OpenAI + GitHub live (AKS still mock)
