# Phase 2 — Live Azure OpenAI + GitHub agents

**Project:** `temporal-maf-agents-poc` (inside the `code-forge-workflow` repo)
**Date:** 2026-06-25
**Status:** Approved design, ready for implementation planning

## Objective

Replace the Phase-1 mocks for three of the four agents with **real** integrations,
without changing the Temporal orchestration model:

- **Planner**, **GitHub**, and **Approval** agents run live.
  - LLM reasoning via **Azure OpenAI** (through Microsoft Agent Framework).
  - The GitHub agent additionally performs **real GitHub API writes** (branch, plan file, PR).
- **AKS agent stays mocked** this phase.

Temporal remains the single orchestration authority. All real I/O happens **inside
activities only**. Workflow code, the `AgentOutput` contract + enums, the two-layer
retry policy, the orchestrator, the k8s Deployments, and the KEDA ScaledObjects are
**unchanged**.

## Locked decisions

| Topic | Decision |
|-------|----------|
| Live agents | Planner (Azure OpenAI), GitHub (Azure OpenAI + GitHub API), Approval (Azure OpenAI); AKS stays mock |
| GitHub action | Create branch + commit a generated plan file + open a PR |
| Tool pattern | LLM plans, the **activity executes** GitHub writes deterministically (no agentic side-effect tools) |
| Azure auth | Env-selected: `DefaultAzureCredential` (Entra ID / AKS workload identity) when no key set, else API key |
| GitHub auth | Fine-grained Personal Access Token (`GITHUB_TOKEN`) |
| Structured output | Azure OpenAI `response_format` JSON schema per agent; activity maps to `AgentOutput` |
| Write guard | Owner/allowlist guard (`GITHUB_ALLOWED_OWNER`); **fail-closed** in live mode if unset |
| Testing | Mocked-client unit tests in CI + opt-in creds-gated live smoke; `AGENT_MODE=mock` stays default |
| Code structure | **Centralized seams**: extend `shared/maf.py`, add `shared/github.py`; agents stay thin |

## Architecture & module layout

The `AGENT_MODE` dispatch in `shared/maf.run_agent(...)` is the only entry point;
`mock` (default) vs `live` is purely an env flip.

| File | Change |
|------|--------|
| `src/shared/maf.py` | Implement `run_live_agent(...)`: build an Azure OpenAI–backed MAF agent (`AzureOpenAIChatClient().as_agent(...)`), run with a per-agent `response_format` schema, return validated JSON. Factor client construction into `build_chat_client()` (env-selected key vs `DefaultAzureCredential`). Lazy imports of the live packages. |
| `src/shared/github.py` *(new)* | LLM-free, idempotent GitHub write client (PyGithub): repo guard, ensure-branch, upsert-file, ensure-PR, error classification. |
| `src/shared/config.py` | Add Phase-2 settings (Azure endpoint/deployment/api-version/key, GitHub token, allowed owner). Read lazily; never imported by workflow code paths that run in the sandbox. |
| `src/planner_agent_worker/agent.py` | Add response schema + prompt builder + `to_output()` mapper. Keep `mock()`. |
| `src/github_agent_worker/agent.py` | Add response schema + prompt builder + `to_output()`; hand the LLM plan to `shared/github.py`. Keep `mock()`. |
| `src/approval_agent_worker/agent.py` | Add response schema + prompt builder + `to_output()` (risk classification → gate). Keep `mock()`. |
| `src/aks_agent_worker/agent.py` | **Unchanged** (stays mock). |
| `requirements.txt` / `requirements-live.txt` / `pyproject.toml` | Keep mock runtime minimal; live extras (`agent-framework-azure-ai`, `azure-identity`, `PyGithub`) in `[live]` extra + `requirements-live.txt` for the Docker image. |
| `Dockerfile` | Build arg / second requirements layer to build a mock image or a live image. |
| `k8s/` | Optional `Secret` (Azure key if used + GitHub PAT) via `envFrom` on the three live workers; document workload-identity as the keyless alternative. No workflow/KEDA changes. |
| `tests/` | New `test_live_mapping.py` (mocked clients) + opt-in `test_live_smoke.py`. |
| `README.md` / `.env.example` | Phase-2 env table + run instructions; fill real vars. |

## Per-agent live behavior

Each agent's LLM returns a **small per-agent schema** (not the full `AgentOutput`).
The activity still owns `agent_name`/`stage` and derives `status`/`next_action`/`retryable`.

### Planner (`planner`, stage `planning`)
- Prompt: goal + repo_url + environment.
- `response_format`: `{ summary: str, steps: [str], risk_level: "low|medium|high", rationale: str }`
- `to_output()`: `status=success`, `retryable=false`, `next_action=continue`,
  `summary=<llm.summary>`, `details={steps, risk_level, rationale}`.

### GitHub (`github`, stage `github`)
- Prompt: goal + environment + planner `steps` (from `upstream["planning"]`).
- `response_format` (content only — not git mechanics):
  `{ pr_title: str, pr_body_markdown: str, plan_file_markdown: str, commit_message: str }`
- Activity supplies deterministic mechanics: `branch = feat/<request_id>`,
  `path = docs/agent-plan-<request_id>.md`; calls `shared/github.py` (see below).
- `to_output()`: `status=success`, `next_action=continue`, `summary="opened PR #<n>"`,
  `details={branch, pr_number, pr_url, plan_file, created_or_existed}`.
- Guard rejection → `status=failed`, `retryable=false`, `next_action=fail`.

### Approval (`approval`, stage `approval`)
- Prompt: goal + environment + planner risk + github PR + whether AKS staged a pending promotion.
- `response_format`: `{ recommendation: "approve|reject|needs_human", risk_level: "low|medium|high", reasons: [str] }`
- The **workflow still owns the durable human-signal gate** (unchanged). The agent only classifies. Mapping:
  - `needs_approval` / `ask_human` when **any** of: `approval_required`, AKS `promotion_pending`,
    `recommendation == needs_human`, or `risk_level == high` (lets the LLM **escalate**).
    `details` carries `recommendation`/`risk_level`/`reasons` **plus** the existing
    `auto_approve`/`timeout_seconds` fields the workflow reads.
  - Otherwise `success` / `continue`.

## GitHub write flow (`shared/github.py`)

Idempotent because Temporal's layer-1 retry can re-run after a partial success.

**Guard (before any write):** parse `owner/repo` from `repo_url`; if `GITHUB_ALLOWED_OWNER`
(or an `owner/repo` allowlist) is set and `owner` doesn't match → raise `GitHubWriteNotAllowed`.
If no allowlist is configured in live mode → **fail-closed** (refuse).

**Sequence (each step check-then-act):**
1. `base = repo.default_branch`
2. `ensure_branch(feat/<request_id>)` — reuse ref if present, else create from `base` HEAD.
3. `upsert_file(docs/agent-plan-<request_id>.md)` — update with existing blob SHA if present, else create.
4. `ensure_pr(head=feat/<request_id>, base=default)` — return existing open PR for that head, else create.
5. Return `{branch, pr_number, pr_url, created_or_existed}`.

**Error classification (shared by `maf.py` and `github.py`):**
- **Transient** — 5xx, rate-limit/secondary-limit, network/timeout → **raise** → Temporal layer-1 retry (10s / ×2 / 120s, 3 attempts).
- **Permanent** — 401/403 auth, 404 repo, 422 validation, guard violation → `status=failed, retryable=false, next_action=fail`.

## Auth, config & dependencies

New `shared/config.py` settings (read lazily):

| Env var | Purpose | Default |
|---------|---------|---------|
| `AGENT_MODE` | `mock` / `live` | `mock` |
| `AZURE_OPENAI_ENDPOINT` | resource endpoint | required in live |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | deployment name (e.g. `gpt-4o`) | required in live |
| `AZURE_OPENAI_API_VERSION` | API version | recent default |
| `AZURE_OPENAI_API_KEY` | optional key | unset → `DefaultAzureCredential` |
| `GITHUB_TOKEN` | fine-grained PAT | required in live |
| `GITHUB_ALLOWED_OWNER` | owner/allowlist guard | unset → live fail-closed |

- `build_chat_client()`: key auth if `AZURE_OPENAI_API_KEY` set, else `DefaultAzureCredential`
  (local `az login` / AKS workload identity). Same image local and on-cluster.
- Mock-mode stays dependency-free: live packages imported **lazily inside the live path**;
  a clear error tells you to `pip install -e ".[live]"` if `AGENT_MODE=live` without them.
- Dockerfile: mock image (default) or live image via build arg / second requirements layer.
- k8s: optional `Secret` via `envFrom` on the three live workers; workload identity documented as keyless path.

## Testing

CI (no creds):
- `test_live_mapping.py` with **mocked** Azure + GitHub clients:
  - Each agent `to_output()` maps a sample LLM JSON → valid `AgentOutput` (schema + enum validation).
  - Approval escalation matrix (`needs_human` / `risk_level=high` / `approval_required` / AKS-pending → `needs_approval`; low-risk + not-required → `success`).
  - GitHub guard: disallowed owner → `failed/non-retryable`; fail-closed when unset.
  - `shared/github.py` idempotency with a fake PyGithub (branch-exists, PR-exists → `existed`, no double create).
  - Error classification: fake 429/5xx **raises**; 401/422 → `failed/non-retryable`.
- Existing 16 Phase-1 tests stay green (`AGENT_MODE=mock` default unchanged).

Opt-in:
- `test_live_smoke.py` — `skipif` unless real creds + `RUN_LIVE_SMOKE=1`: one planner round-trip
  against Azure OpenAI and one branch+file+PR against a sandbox repo, with cleanup.

## Docs

- `README.md`: "Phase 2 — going live" section (env table, `pip install -e ".[live]"`,
  `AGENT_MODE=live`, the owner guard, keyless-on-AKS note) and flip the acceptance-criteria checkbox.
- `.env.example`: fill real Azure/GitHub vars (replace current TODO comments).

## Explicitly unchanged

All `*/workflows.py`, `shared/contracts.py` (enums + `decide()`), `shared/child.py`
(retry policy), the orchestrator, k8s Deployments, KEDA ScaledObjects, and the AKS agent.

## Out of scope (future phases)

- Live AKS / Kubernetes agent (stays mock).
- Full feature code-generation by the GitHub agent (plan file + PR only).
- GitHub App auth (PAT only this phase).
- Agentic MAF side-effect tools.
