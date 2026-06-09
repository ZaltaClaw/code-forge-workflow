# Architecture

> The deep dive. Read `CLAUDE.md` first for the 10-minute overview.

## Goals

1. **Many developers, one Foundry tenant.** 200+ engineers running Claude Code concurrently, sharing model capacity, with per-dev rate limits and budgets.
2. **Sub-2s cold start.** Developer hits the platform → has a working Claude Code session in under 2 seconds, including auth + workspace mount.
3. **No static credentials.** Zero long-lived API keys anywhere — everything via Azure Workload Identity (federated OIDC).
4. **Bring-your-own-laptop.** Engineers can use VS Code, Codespaces, JetBrains, or Cursor — anything that speaks the Dev Containers spec.
5. **Cost transparency.** Per-developer, per-team token spend is observable in real time.

## Topology

```
                              ┌────────────────────────────┐
                              │      Azure Front Door      │
                              │  api.codeforge.example.com │
                              └─────────────┬──────────────┘
                                            │ HTTPS (mTLS optional)
                          ┌─────────────────▼──────────────────┐
                          │              AKS Cluster           │
                          │  ┌──────────────────────────────┐  │
                          │  │  Namespace: session-control  │  │
                          │  │  ┌───────────────────────┐   │  │
                          │  │  │ sandbox-orchestrator  │   │  │
                          │  │  │ (FastAPI, Python)     │   │  │
                          │  │  │ claims + apiserver exec│  │  │
                          │  │  └────┬────────┬─────────┘   │  │
                          │  └───────│────────│─────────────┘  │
                          │          │        │ SandboxClaim   │
                          │   claim  │        │ (adopt warm)   │
                          │          ▼        ▼                │
                          │  ┌──────────────────────────────┐  │
                          │  │  Namespace: agent-sandboxes  │  │
                          │  │  PSA: restricted             │  │
                          │  │  SandboxWarmPool (pre-warmed)│  │
                          │  │  ┌──────┐ ┌──────┐ ┌──────┐  │  │
                          │  │  │ sbox │ │ sbox │ │ sbox │  │  │
                          │  │  │ warm │ │ used │ │ warm │  │  │
                          │  │  │claude│ │claude│ │claude│  │  │
                          │  │  └──┬───┘ └──┬───┘ └──┬───┘  │  │
                          │  │     └────────┴────────┘      │  │
                          │  └───────────│──────────────────┘  │
                          │              │                     │
                          │              ▼ /anthropic           │
                          │  ┌──────────────────────────────┐  │
                          │  │  Namespace: platform         │  │
                          │  │  ┌────────────────────────┐  │  │
                          │  │  │ model-gateway (LiteLLM)│  │  │
                          │  │  │ + AAD-token sidecar    │  │  │
                          │  │  └─────────┬──────────────┘  │  │
                          │  └────────────│────────────────┘  │
                          └───────────────│───────────────────┘
                                           │ HTTPS + AAD bearer
                                           ▼
                          ┌──────────────────────────────────┐
                          │     Microsoft Foundry             │
                          │  - claude-opus-4-8 deployment     │
                          │  - claude-sonnet-4-6 deployment   │
                          │  - claude-haiku-4-5 deployment    │
                          └──────────────────────────────────┘

   Provided by the agent-sandbox controller (agent-sandbox-system namespace):
   the SandboxClaim / SandboxTemplate / SandboxWarmPool CRDs.
   Side services: Key Vault (LiteLLM master key), ACR, optional per-dev PVCs.
```

## Request lifecycle in detail

### 1. Claim

```
client → POST https://api.codeforge.example.com/v1/sandboxes
         { dev_id: "alice", command: "…" }   # or a free-text task
```

Front Door → orchestrator. The orchestrator:

1. Admission-checks `MAX_CONCURRENT_TOTAL` and `MAX_CONCURRENT_PER_DEV`; over either → HTTP 429.
2. Creates a `SandboxClaim` (`cf-claim-<uuid>`) referencing the configured `SandboxTemplate` + `SandboxWarmPool`, with a controller-side TTL (`shutdownTime` / `shutdownPolicy: Delete`) as a safety net.
3. The agent-sandbox controller **adopts a pre-warmed pod** from the `SandboxWarmPool` and binds it to the claim (sub-2s). The claim's `status.sandbox.name` carries the adopted sandbox's name (which differs from the claim name).
4. The orchestrator resolves that sandbox name, waits for Ready (first podIP), and stages any request `files` into `/workspace` via the apiserver exec stream.
5. It runs the command inside the sandbox over the same exec stream and collects stdout/stderr/exit code.

### 2. Use

Inside the sandbox, Claude Code is configured for Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, `ANTHROPIC_FOUNDRY_BASE_URL=http://model-gateway.platform.svc.cluster.local/anthropic`, pinned model names). It calls the in-cluster **gateway**, which authenticates to Foundry via a federated AAD token, applies the per-dev budget / RPM caps, forwards the request, and logs cost. The sandbox never sees Foundry or an API key directly.

### 3. Release

- **Completion** — when the command finishes, the orchestrator deletes the `SandboxClaim`. The warm pool controller self-heals back to its target `readyReplicas`.
- **TTL** — if the orchestrator crashes mid-request, the claim's `shutdownTime` lets the controller reap the sandbox without manual cleanup.
- **Pool refresh** — each sandbox is single-use; the read-only root FS + emptyDir `/workspace` mean there is no cross-session residue to scrub.

## Why these tech choices

| Choice | Why | What we considered |
|---|---|---|
| AKS (vs. ACI / Container Apps) | Full PSA, NetworkPolicies, custom CSI drivers, and the agent-sandbox CRDs | Container Apps doesn't expose NetworkPolicy; ACI doesn't pool |
| agent-sandbox `SandboxWarmPool` (vs. a hand-rolled warm Deployment + KEDA) | Pre-warmed pods + claim adoption give sub-2s allocation with a controller that owns lifecycle; no label-patching state machine | A KEDA-scaled Deployment with `state=warm/bound` labels (our original v1 — retired) |
| apiserver `exec` stream for I/O (vs. an in-pod HTTP server) | The agent-pod image runs Claude Code, not a runtime HTTP server; exec needs no extra surface or NetworkPolicy ingress | The agent-sandbox SDK's HTTP transport — needs a runtime server in the image we don't ship |
| LiteLLM (vs. APIM / Traefik) | Native LLM features: virtual keys, budgets, prompt-cache routing, model fallback | APIM lacks LLM-native budget; Traefik isn't aware of token semantics |
| Workload Identity (vs. AAD Pod Identity / static keys) | OIDC-federated, no secret on disk, per-workload MI | Pod Identity is deprecated; static keys are an audit nightmare |
| Bicep (vs. Terraform) | First-party Azure, `what-if` is excellent, no state file to manage | Terraform if multi-cloud — we're not |
| Helm (vs. raw YAML / Kustomize) | Templating + values + lint + release lifecycle | Kustomize for variant overlays; we use values overlays for that |
| Devcontainer Feature install (vs. baking a fixed CLI version) | Latest Claude Code automatically; matches official docs | Fixed version when security review needs reproducibility — set `CLAUDE_CODE_DISABLE_AUTOUPDATE=1` and pin |

## Capacity model

- **Warm pool baseline**: `sandboxOrchestrator.sandbox.warmpoolReplicas` pre-warmed sandboxes kept Ready by the controller. Each adopted sandbox serves one request, then is torn down.
- **Admission caps**: `concurrency.maxTotal` (cluster-wide) and `concurrency.maxPerDev` bound in-flight sandboxes; exceeding either returns HTTP 429.
- **Sandbox size**: governed by `sandboxOrchestrator.sandbox.resources` (default `250m–1 CPU`, `256Mi–1Gi`). Tune up for heavier Claude Code workloads.
- **Scaling knob**: raise `warmpoolReplicas` for deeper burst headroom (more idle pods, faster claims) and `maxTotal` for higher concurrency ceilings.

## Failure modes & blast radius

| Failure | Blast radius | Mitigation |
|---|---|---|
| Orchestrator crash | New requests fail until the pod restarts (~10s). In-flight sandboxes keep running; their claims are reaped by TTL | 2+ replicas behind a Service; requests are stateless and retryable |
| agent-sandbox controller down | No new claims can be adopted/provisioned; existing sandboxes unaffected | Controller runs with leader-election; restart is fast and stateless |
| Gateway crash | All in-flight Claude Code requests fail | 2+ replicas; LiteLLM auto-failover between Foundry deployments |
| Foundry outage | Same as above | Multi-region Foundry deployment + LiteLLM `fallback_models` config |
| Warm pool exhausted | Claims fall back to a cold provision from the `SandboxTemplate` (slower, still works) | Raise `warmpoolReplicas`; the controller refills the pool continuously |
| Sandbox pod eviction | One in-flight request fails | Request is retryable; the orchestrator deletes the claim and the pool self-heals |
| AAD token refresh failure | Gateway returns 401 to all agents | Sidecar logs; if it can't refresh for 50 min, alert fires; gateway keeps retrying |

## Roadmap

- [ ] **Session affinity / resume** — let a dev reattach to a still-running sandbox for multi-turn work instead of one-shot claims.
- [ ] **Per-request virtual keys** — have the orchestrator mint a short-lived LiteLLM key per claim for finer-grained budget attribution.
- [ ] **Multi-region active-active** — pair of clusters in westus3 + eastus2; Front Door routes by latency.
- [ ] **Confidential Containers** — swap PSA-restricted for AKS Confidential Containers when GA on the SKU we use; isolation upgrade for sensitive customers.
- [ ] **OpenTelemetry traces** end-to-end with `request_id` and `dev_id` propagated.
