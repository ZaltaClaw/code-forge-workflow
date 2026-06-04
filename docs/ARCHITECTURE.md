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
                          │  │  │ session-router (Go)   │   │  │
                          │  │  │ HTTPScaledObject (KEDA│   │  │
                          │  │  │ HTTP Add-on, RPS)     │   │  │
                          │  │  └────┬────────┬─────────┘   │  │
                          │  └───────│────────│─────────────┘  │
                          │          │        │ patch labels   │
                          │   bind   │        │                │
                          │          ▼        ▼                │
                          │  ┌──────────────────────────────┐  │
                          │  │  Namespace: agent-pool       │  │
                          │  │  PSA: restricted             │  │
                          │  │  ┌──────┐ ┌──────┐ ┌──────┐  │  │
                          │  │  │ pod  │ │ pod  │ │ pod  │  │  │
                          │  │  │ warm │ │bound │ │ warm │  │  │
                          │  │  │claude│ │claude│ │claude│  │  │
                          │  │  └──┬───┘ └──┬───┘ └──┬───┘  │  │
                          │  │     └────────┴────────┘      │  │
                          │  │           │                  │  │
                          │  │   ScaledObject (KEDA,        │  │
                          │  │   Service Bus depth)         │  │
                          │  └───────────│──────────────────┘  │
                          │              │                     │
                          │              ▼ /anthropic           │
                          │  ┌──────────────────────────────┐  │
                          │  │  Namespace: platform         │  │
                          │  │  ┌────────────────────────┐  │  │
                          │  │  │ model-gateway (LiteLLM)│  │  │
                          │  │  │ + AAD-token sidecar    │  │  │
                          │  │  └─────────┬──────────────┘  │  │
                          │  │  ┌─────────┴──────┐          │  │
                          │  │  │ Redis (state)  │          │  │
                          │  │  └────────────────┘          │  │
                          │  └─────────────│────────────────┘  │
                          └────────────────│───────────────────┘
                                           │ HTTPS + AAD bearer
                                           ▼
                          ┌──────────────────────────────────┐
                          │     Microsoft Foundry             │
                          │  - claude-opus-4-8 deployment     │
                          │  - claude-sonnet-4-6 deployment   │
                          │  - claude-haiku-4-5 deployment    │
                          └──────────────────────────────────┘

   Side services: Cosmos DB (audit), Service Bus (KEDA queue), Key Vault
   (LiteLLM master key), Azure Files (per-dev workspace PVCs), ACR.
```

## Request lifecycle in detail

### 1. Claim

```
client → POST https://api.codeforge.example.com/sessions
         { dev_id: "alice", project_id: "checkout-svc" }
```

Front Door → router. Router:

1. `GET sess:alice:checkout-svc` from Redis.
2. **Hit** → bump TTL, return `{pod_name, exec_url}`. Done.
3. **Miss** → check `MAX_CONCURRENT_PER_DEV` (Redis SCard).
4. **Miss** → `kubectl get pods -n agent-pool -l app=agent-pod,state=warm --limit=10`.
5. Pick first; **strategic-merge patch** labels: `state=bound, dev-id=alice, session-id=<ulid>, project-id=checkout-svc`.
6. Bind workspace: patch the pod to mount Azure Files PVC `workspace-alice-checkout-svc`.
7. Mint a virtual key in LiteLLM: `POST /key/generate` with `models=[claude-opus,…]`, `max_budget=50`, `tpm=200000`, `rpm=200`. LiteLLM returns `sk-…`.
8. Write that key to a per-pod K8s `Secret` named `agent-virtual-key-<sessid>`; the pod's env reads it via `secretKeyRef` (already wired in `_helpers.tpl`).
9. `SET sess:alice:checkout-svc {pod_name, …}` with TTL = idle timeout.
10. `INSERT` audit row into Cosmos.
11. Return.

### 2. Use

The pod's Claude Code now has a virtual key, hits the in-cluster gateway, which authenticates to Foundry via AAD, gets a streaming completion, decrements the dev's budget in LiteLLM's Postgres, returns to Claude Code.

### 3. Release

Three triggers:

- **Idle** — pod's `agent-entrypoint` watchdog sees no activity for 15 min → calls `agent-shutdown` → exits → ReplicaSet replaces.
- **Spot eviction** — Service Bus message published by AKS spot-eviction handler. Router picks it up, marks the session `migrating`, claims a new warm pod, re-mounts the same PVC, transparently resumes (lossy if mid-stream — TODO: resume-on-reconnect via Claude Code session resume).
- **Explicit logout** — `DELETE /sessions/<id>` from the client. Router patches `state=cooldown`, deletes the K8s secret, evicts from Redis.

## Why these tech choices

| Choice | Why | What we considered |
|---|---|---|
| AKS (vs. ACI / Container Apps) | Full PSA, NetworkPolicies, KEDA HTTP Add-on, custom CSI drivers | Container Apps doesn't expose NetworkPolicy; ACI doesn't pool |
| KEDA HTTP Add-on for the router | Scale-to-zero off-hours, RPS-based, request buffering during cold-start | Plain HPA on CPU lags by minutes |
| KEDA Service Bus for agent pool | Decouples claim demand from pool size; queue acts as buffer for burst | HPA-on-Redis would work but Service Bus is the standard pattern with auth via MI |
| LiteLLM (vs. APIM / Traefik) | Native LLM features: virtual keys, budgets, prompt-cache routing, model fallback | APIM lacks LLM-native budget; Traefik isn't aware of token semantics |
| Workload Identity (vs. AAD Pod Identity / static keys) | OIDC-federated, no secret on disk, per-workload MI | Pod Identity is deprecated; static keys are an audit nightmare |
| Bicep (vs. Terraform) | First-party Azure, `what-if` is excellent, no state file to manage | Terraform if multi-cloud — we're not |
| Helm (vs. raw YAML / Kustomize) | Templating + values + lint + release lifecycle | Kustomize for variant overlays; we use values overlays for that |
| Devcontainer Feature install (vs. baking a fixed CLI version) | Latest Claude Code automatically; matches official docs | Fixed version when security review needs reproducibility — set `CLAUDE_CODE_DISABLE_AUTOUPDATE=1` and pin |

## Capacity model

- **Warm pool baseline**: 80 pods. Each pod = 1 active session.
- **Burst max**: 400 pods (KEDA `maxReplicas`).
- **Pod size**: `1.5–4 CPU`, `3–8 GiB`. Spot-priced D8s_v5 ≈ $0.08/hr → ~$58/mo per pod.
- **Steady-state cost**: 80 pods × $58 + cluster overhead + Foundry tokens. Estimate: $8–12k/mo for ~150 active developers (2–3 sessions per dev per day, 90-min average).
- **Idle scrub**: 15 min. Tunable via `agentPod.idleTimeoutSeconds`.

## Failure modes & blast radius

| Failure | Blast radius | Mitigation |
|---|---|---|
| Router crash | New claims fail until pod restarts (~10s). Existing sessions unaffected (they talk to pods directly) | 3+ replicas behind a Service; KEDA HTTP Add-on absorbs the request burst |
| Gateway crash | All in-flight Claude Code requests fail | 2+ replicas; LiteLLM auto-failover between Foundry deployments |
| Foundry outage | Same as above | Multi-region Foundry deployment + LiteLLM `fallback_models` config |
| Redis crash | Session state lost — all dev sessions get a new pod on next message | Azure Cache for Redis with replication; Cosmos as durable backup |
| Spot eviction storm | Many pods evicted at once → claim queue backs up | KEDA scales pool from on-demand fallback; spot diversification across SKUs |
| AAD token refresh failure | Gateway returns 401 to all agents | Sidecar logs; if it can't refresh for 50 min, alert fires; gateway keeps retrying |

## Roadmap

- [ ] **HTTP shim per pod** — replace `kubectl exec` I/O channel with a pod-local HTTP shim + Service Mesh; eliminates API server proxy hops.
- [ ] **Resume-on-reconnect** — Claude Code's session resume + idempotency tokens so spot eviction is invisible.
- [ ] **Multi-region active-active** — pair of clusters in westus3 + eastus2; Front Door routes by latency.
- [ ] **Confidential Containers** — swap PSA-restricted for AKS Confidential Containers when GA on the SKU we use; isolation upgrade for sensitive customers.
- [ ] **OpenTelemetry traces** end-to-end with `session_id` and `dev_id` propagated.
