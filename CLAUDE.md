# CLAUDE.md — Code Forge

> This file is auto-loaded by Claude Code (`claude`) when it starts in this
> repo. It is the orientation pack for any human or AI working on Code Forge.
> Subdirectories have their own `CLAUDE.md` with deeper context — open the
> nearest one when you start a task.

## What is this repo?

**Code Forge** is a multi-tenant deployment of [Claude Code](https://claude.com/claude-code)
on Azure Kubernetes Service (AKS), with [Microsoft Foundry](https://learn.microsoft.com/azure/ai-foundry/)
as the model backend. It lets a whole engineering org share Claude Code through
a managed pool of agent containers — each developer gets an ephemeral pod, all
model traffic is routed through a single LiteLLM gateway, and everything is
governed by Azure Workload Identity (no static API keys anywhere).

If a single developer running `claude` locally is the unit, **Code Forge is the
power-strip that lets 200+ developers plug in at once**, with budgets,
rate-limits, audit, and a warm pool so cold-start is sub-2-seconds.

## The three primitives

```
┌────────────────────────────────────────────────────────┐
│  developer  ──HTTP──▶  sandbox-orchestrator ──claim──▶  agent pod │
│   (laptop / IDE)            │                              │      │
│                             ▼                              ▼      │
│                     SandboxWarmPool (CRD)            Claude Code  │
│                     (pre-warmed sandboxes)                 │      │
│                                                            ▼      │
│                                                    model-gateway  │
│                                                     (LiteLLM)     │
│                                                            │      │
│                                                            ▼      │
│                                                   Microsoft Foundry│
│                                                   (Anthropic API)  │
└───────────────────────────────────────────────────────────────────┘
```

| Primitive | What it is | Where it lives | Why |
|---|---|---|---|
| **Agent sandbox** | Ubuntu devcontainer w/ Claude Code installed via the official `ghcr.io/anthropics/devcontainer-features/claude-code:1.0` Feature | `containers/agent-pod/` (image) + `charts/code-forge/templates/36-sandbox-template.yaml` (`SandboxTemplate`/`SandboxWarmPool`) | Stateless, ephemeral, pre-warmed in a pool; writable `/workspace` under a read-only root filesystem |
| **Sandbox orchestrator** | Python (FastAPI) service that provisions ONE agent sandbox per request — claims a warm pod, runs the command over the apiserver exec stream, tears it down | `containers/sandbox-orchestrator/` + `charts/code-forge/templates/35-sandbox-orchestrator.yaml` | The traffic cop. Stateless; relies on the agent-sandbox CRDs (`SandboxClaim`/`SandboxTemplate`/`SandboxWarmPool`) |
| **Model gateway** | LiteLLM proxy that fronts Foundry, holds an AAD token (refreshed via Workload Identity), enforces per-dev budgets | `containers/model-gateway/` + `charts/code-forge/templates/40-model-gateway.yaml` | Single audit/billing chokepoint. Agents never see Foundry directly |

## Repo map (start here)

```
.
├── CLAUDE.md                          ← you are here
├── README.md                          ← human-facing intro (same as chart/README.md)
├── Makefile                           ← image build + helm install targets
│
├── charts/code-forge/                 ← Helm chart (the deployable unit)
│   ├── Chart.yaml
│   ├── values.yaml                    ← all knobs, with comments
│   ├── values-prod.yaml               ← reference production overlay
│   ├── README.md                      ← quickstart
│   ├── CLAUDE.md                      ← chart-specific orientation
│   └── templates/
│       ├── 00-namespaces.yaml
│       ├── 05-serviceaccounts.yaml
│       ├── 35-sandbox-orchestrator.yaml
│       ├── 36-sandbox-template.yaml
│       ├── 40-model-gateway.yaml
│       └── 50-network-policies.yaml
│
├── containers/
│   ├── agent-pod/                     ← Ubuntu devcontainer + Claude Code
│   │   ├── Dockerfile
│   │   ├── .devcontainer/             ← parity with prod for local dev
│   │   ├── agent-entrypoint
│   │   ├── agent-shutdown
│   │   ├── healthz.py
│   │   └── CLAUDE.md
│   ├── sandbox-orchestrator/          ← Python (FastAPI + agent-sandbox SDK)
│   │   ├── sandbox_orchestrator/      ← api, manager, backends, config, models
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── CLAUDE.md
│   └── model-gateway/                 ← LiteLLM + AAD-token sidecar
│       ├── Dockerfile
│       ├── refresh-aad-token.py
│       ├── entrypoint.sh
│       └── CLAUDE.md
│
├── infra/                             ← Bicep IaC for the Azure underlay
│   ├── main.bicep
│   ├── modules/                       ← network, identity, storage, kv, logs,
│   │   …                                servicebus, acr, aks, frontdoor, foundry
│   └── CLAUDE.md
│
└── docs/
    ├── ARCHITECTURE.md                ← deep-dive on every primitive
    ├── ONBOARDING.md                  ← 30-minute new-engineer ramp
    ├── OPERATIONS.md                  ← runbooks (rotations, scaling, P1s)
    ├── SECURITY.md                    ← threat model + isolation strategy
    ├── DEVELOPMENT.md                 ← local-dev loop
    └── architecture-azure-v5.svg/png  ← system diagram
```

## How a request flows (read this once, it makes everything click)

1. Developer runs `claude` in their IDE (or their CLI hits `https://api.codeforge.example.com`).
2. A thin client call goes to the **sandbox-orchestrator** with `{dev_id, command}`.
3. The orchestrator creates a `SandboxClaim` that **adopts a pre-warmed pod** from the `SandboxWarmPool` (sub-2s), instead of cold-starting. No free warm pod? → the claim provisions a fresh sandbox from the `SandboxTemplate`.
4. The orchestrator streams I/O into the sandbox via the kube-apiserver `exec` stream (the agent-pod image runs Claude Code, not an in-pod HTTP server).
5. Inside the sandbox, Claude Code calls the **model-gateway** (env: `ANTHROPIC_FOUNDRY_BASE_URL=http://model-gateway.platform.svc.cluster.local/anthropic`).
6. The gateway authenticates to **Foundry** with a federated AAD token, applies budget/RPM caps, forwards the request, logs cost.
7. On completion (or TTL), the orchestrator deletes the `SandboxClaim`; the warm pool self-heals back to its target `readyReplicas`.

## Conventions

- **No static API keys.** Every credential is Azure Workload Identity (federated OIDC). If you're tempted to add `ANTHROPIC_API_KEY` to a secret, stop and read `docs/SECURITY.md`.
- **Pin model versions explicitly.** Aliases (`opus`, `sonnet`, `haiku`) auto-resolve on Foundry and break when Anthropic releases new models. We pin `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5` in `values.yaml`.
- **Sandboxes are cattle.** Agent sandboxes MUST be safe to nuke at any time. Anything durable goes in the model-gateway audit log or per-dev PVCs.
- **Default-deny networking.** Agent sandboxes can only reach the model-gateway and DNS — no public egress, no direct Foundry calls, no internal lateral movement.
- **Helm is the source of truth** for what's running. Production deploys go through `make chart-install`.
- **Bicep is the source of truth** for what Azure resources exist.

## Common tasks

| Task | Where |
|---|---|
| Change agent-pod image | `containers/agent-pod/Dockerfile` + bump `agentPod.image.tag` |
| Add a model | `values.yaml` → `global.foundry.models` + LiteLLM ConfigMap |
| Tweak warm-pool size | `values.yaml` → `sandboxOrchestrator.sandbox.warmpoolReplicas` |
| Add a per-dev budget | `values.yaml` → `modelGateway.budgets` |
| Add a network egress allow | `templates/50-network-policies.yaml` |
| Rotate LiteLLM master key | `docs/OPERATIONS.md` → "Key rotation" |
| Deploy to a new region | `infra/main.bicep` + `infra/modules/foundry.bicep` |

## Quick smoke-tests

```bash
# Helm sanity
make chart-lint
make chart-template | head -50

# Orchestrator unit tests
cd containers/sandbox-orchestrator && python -m pytest -q

# Dry-run image build (needs Docker daemon)
docker build -t code-forge/agent-pod:dev containers/agent-pod
```

## Key external docs

- Claude Code dev container: <https://code.claude.com/docs/en/devcontainer>
- Claude Code on Foundry:    <https://code.claude.com/docs/en/microsoft-foundry>
- Claude Code LLM gateway:   <https://code.claude.com/docs/en/llm-gateway>
- Azure Workload Identity:   <https://azure.github.io/azure-workload-identity/docs/>

## Who runs this

- **Roey Zalta** (`@ZaltaClaw`) — owner
- **Michael Liav** (`@michaelliav`) — admin

## When in doubt

Read the nearest `CLAUDE.md`. Then `docs/ARCHITECTURE.md`. Then ask in the
PR description so the conversation is preserved.
