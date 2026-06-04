# CLAUDE.md — Helm chart `code-forge`

> Loaded when `claude` runs in `charts/code-forge/`. Read the repo-root
> `CLAUDE.md` first for the big picture.

## What this chart deploys

Five logical sections, one template file each, numbered for render order:

| File | Resources | Purpose |
|---|---|---|
| `00-namespaces.yaml` | 3 × `Namespace` (PSA-restricted) | `session-control`, `agent-pool`, `platform`. Each carries `pod-security.kubernetes.io/enforce: restricted` so Pod Security Admission rejects privileged pods at admission time |
| `05-serviceaccounts.yaml` | 3 × `ServiceAccount` | Federated to Azure user-assigned managed identities via `azure.workload.identity/client-id` annotations. Token volume is auto-projected by the workload-identity webhook |
| `20-agent-pod.yaml` | `Deployment` + `ResourceQuota` + `ScaledObject` | Warm pool of agent containers. KEDA scales on Service Bus `agent-pod-claims` queue depth |
| `30-session-router.yaml` | `Deployment` + `Service` + `HTTPScaledObject` + `Role`/`RoleBinding` | Router itself + KEDA HTTP Add-on for RPS-based scaling. Cross-namespace RBAC lets the router patch agent-pool pods |
| `40-model-gateway.yaml` | `Deployment` + `Service` + `ConfigMap` | LiteLLM proxy + its `config.yaml` (model list, budgets, pass-through `/anthropic` endpoint) |
| `50-network-policies.yaml` | 3 × `NetworkPolicy` | Default-deny everywhere; agent pods can ONLY reach `model-gateway` + DNS; gateway can reach Foundry on 443 |

## Values you'll touch most

```yaml
global:
  foundry:
    resource: <foundry-resource-name>     # required — drives BASE_URL
    models:                               # required — pin model versions
      opus:    claude-opus-4-8
      sonnet:  claude-sonnet-4-6
      haiku:   claude-haiku-4-5

agentPod:
  replicas: 80                            # warm-pool baseline (ignored if KEDA min > this)
  keda:
    minReplicas: 80
    maxReplicas: 400

sessionRouter:
  keda.httpAddon.targetPendingRequests: 50

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
# → agent-pod, session-router, model-gateway

# Full render to a file for inspection
helm template demo charts/code-forge \
  -f charts/code-forge/values-prod.yaml \
  --set global.azureTenantId=$(uuidgen) \
  --set workloadIdentity.agentPod.clientId=$(uuidgen) \
  --set workloadIdentity.sessionRouter.clientId=$(uuidgen) \
  --set workloadIdentity.modelGateway.clientId=$(uuidgen) \
  > /tmp/render.yaml
```

## Production install

```bash
helm upgrade --install code-forge charts/code-forge \
  --create-namespace -n session-control \
  -f charts/code-forge/values-prod.yaml \
  --set global.azureTenantId=$AZ_TENANT \
  --set workloadIdentity.agentPod.clientId=$AGENT_MI \
  --set workloadIdentity.sessionRouter.clientId=$ROUTER_MI \
  --set workloadIdentity.modelGateway.clientId=$GATEWAY_MI \
  --set agentPod.keda.serviceBus.namespace=$SB_NAMESPACE
```

## Common pitfalls

- **`HTTPScaledObject` not found** → install KEDA HTTP Add-on first: `helm install http-add-on kedacore/keda-add-ons-http -n keda`.
- **`ScaledObject` Service Bus auth failing** → make sure `keda-azure-identity` `TriggerAuthentication` exists and KEDA's MI has `Azure Service Bus Data Owner` on the queue.
- **Agent pods stuck `Pending`** → spot capacity exhausted. Check `kubectl describe pod` for the `FailedScheduling` event; bump on-demand fallback in the AKS node-pool spec.
- **Model gateway 401 to Foundry** → `refresh-aad-token.py` failed; check the gateway pod logs for the `[refresh-aad]` line. Usually a missing federation between the gateway MI and its ServiceAccount.
