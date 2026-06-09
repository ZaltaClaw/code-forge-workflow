# CLAUDE.md — Helm chart `code-forge`

> Loaded when `claude` runs in `charts/code-forge/`. Read the repo-root
> `CLAUDE.md` first for the big picture.

## What this chart deploys

Templates are numbered for render order:

| File | Resources | Purpose |
|---|---|---|
| `00-namespaces.yaml` | 3 × `Namespace` (PSA-restricted) | `session-control`, `platform`, `agent-sandboxes`. Each carries `pod-security.kubernetes.io/enforce: restricted` so Pod Security Admission rejects privileged pods at admission time |
| `05-serviceaccounts.yaml` | `ServiceAccount` | model-gateway SA, federated to an Azure user-assigned managed identity via `azure.workload.identity/client-id`. (The orchestrator SA is created in `35-…`.) |
| `35-sandbox-orchestrator.yaml` | `ServiceAccount` + `Deployment` + `Service` + `Role`/`RoleBinding` | The orchestrator. Provisions ONE agent sandbox per request (claim → run → teardown). RBAC targets the agent-sandbox CRDs + pods/exec in the sandbox namespace |
| `36-sandbox-template.yaml` | `SandboxTemplate` + `SandboxWarmPool` | The warm pool. Stamps the agent-pod image into pre-warmed sandboxes (Foundry env, writable workspace, PSA-clean). Sub-2s allocation via claim adoption |
| `40-model-gateway.yaml` | `Deployment` + `Service` + `ConfigMap` | LiteLLM proxy + its `config.yaml` (model list, budgets, pass-through `/anthropic` endpoint) |
| `50-network-policies.yaml` | `NetworkPolicy` | Sandboxes can ONLY reach `model-gateway` + DNS; gateway can reach Foundry on 443 |

> The legacy `20-agent-pod.yaml` (warm-pool Deployment + KEDA) and
> `30-session-router.yaml` were removed when the chart fully migrated to the
> Sandbox Orchestrator. The agent-pod **image** lives on — it's what the
> `SandboxTemplate` runs.

## Values you'll touch most

```yaml
global:
  foundry:
    resource: <foundry-resource-name>     # required — drives BASE_URL
    models:                               # required — pin model versions
      opus:    claude-opus-4-8
      sonnet:  claude-sonnet-4-6
      haiku:   claude-haiku-4-5

sandboxOrchestrator:
  backend: direct                         # direct | sdk | fake
  sandbox:
    warmpoolReplicas: 2                   # pre-warmed sandboxes kept Ready
    useWarmpool: true                     # claim-adopt a warm pod (sub-2s)
  concurrency:
    maxTotal: 100
    maxPerDev: 3

modelGateway:
  budgets.default.maxBudgetUsd: 50
```

## Editing rules

1. **Don't add resources outside the 5 templates** — pick the right section number. If you genuinely need a new section, use a new file and document its place in the request flow.
2. **Always thread values through `values.yaml` with a comment.** No magic constants in templates.
3. **Touch `_helpers.tpl` for anything reused twice.** The `code-forge.claudeCodeFoundryEnv` block is the canonical example — change Claude Code env vars there, never inline.
4. **Run `helm lint && helm template`** before committing. Both are wired into `make chart-lint` / `make chart-template`.
5. **PSA `restricted` is non-negotiable.** Pods MUST run as non-root, drop ALL caps, set seccompProfile, and never request privilege escalation.

## Testing locally

```bash
# Dry-run with synthetic IDs
make chart-template | yq 'select(.kind=="Deployment") | .metadata.name'
# → sandbox-orchestrator, model-gateway

# Full render to a file for inspection
helm template demo charts/code-forge \
  -f charts/code-forge/values-prod.yaml \
  --set global.azureTenantId=$(uuidgen) \
  --set workloadIdentity.sandboxOrchestrator.clientId=$(uuidgen) \
  --set workloadIdentity.modelGateway.clientId=$(uuidgen) \
  > /tmp/render.yaml
```

## Production install

```bash
helm upgrade --install code-forge charts/code-forge \
  --create-namespace -n session-control \
  -f charts/code-forge/values-prod.yaml \
  --set global.azureTenantId=$AZ_TENANT \
  --set workloadIdentity.sandboxOrchestrator.clientId=$ORCH_MI \
  --set workloadIdentity.modelGateway.clientId=$GATEWAY_MI
```

## Common pitfalls

- **SandboxTemplate/WarmPool CRDs not found** → install the agent-sandbox
  extensions (v0.4.6 `extensions.yaml`) first, or set
  `sandboxOrchestrator.sandbox.provisionTemplate=false` on a minimal cluster.
- **Updating the SandboxTemplate doesn't refresh live warm pods** → delete the
  warm Sandbox CRs by name (`kubectl delete sandbox -n agent-sandboxes <names>`)
  to force the pool to re-stamp from the new template.
- **Model gateway 401 to Foundry** → `refresh-aad-token.py` failed; check the
  gateway pod logs for the `[refresh-aad]` line. Usually a missing federation
  between the gateway MI and its ServiceAccount.
