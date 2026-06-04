# Code Forge — Claude Code on AKS, fronted by Microsoft Foundry

Multi-tenant Claude Code deployment for engineering orgs.
Built around three primitives:

```
┌─────────────────────────────────────────────────────────────┐
│  developer  ──HTTP──▶  session-router  ──k8s patch──▶  pod  │
│                              │                          │   │
│                              ▼                          ▼   │
│                          Redis + Cosmos          Claude Code│
│                                                       │     │
│                                                       ▼     │
│                                             model-gateway   │
│                                                       │     │
│                                                       ▼     │
│                                             Microsoft Foundry│
└─────────────────────────────────────────────────────────────┘
```

- **Agent pod** — Ubuntu devcontainer image with Claude Code installed via the
  official `ghcr.io/anthropics/devcontainer-features/claude-code:1.0` Feature.
  Runs as non-root, ephemeral `/workspace`, idle timeout, label-driven state
  machine (`warm` → `bound` → `cooldown`).
- **Session router** — Go service that maps `(dev_id, project_id)` to a free
  warm pod, mints a per-session virtual key, and patches pod labels.
- **Model gateway** — LiteLLM proxy in front of Foundry. Translates the
  Anthropic Messages API used by Claude Code, holds an AAD token refreshed
  via Workload Identity, and enforces per-dev budgets / rate limits.

## What's in the chart

```
charts/code-forge/
  Chart.yaml
  values.yaml          ← all knobs
  templates/
    00-namespaces.yaml          # PSA-restricted namespaces
    05-serviceaccounts.yaml     # workload-identity SAs
    20-agent-pod.yaml           # warm pool + KEDA service-bus scaler
    30-session-router.yaml      # router + KEDA HTTP Add-on RPS scaler + RBAC
    40-model-gateway.yaml       # LiteLLM + ConfigMap
    50-network-policies.yaml    # default-deny + tight allow-list
```

## Container images

```
containers/
  agent-pod/
    Dockerfile             # devcontainers/base + claude-code feature
    .devcontainer/         # local-dev devcontainer.json (matches prod)
    agent-entrypoint       # idle watchdog + healthz
    agent-shutdown         # preStop scrub
    healthz.py
  session-router/
    main.go                # claim/release loop (Go + client-go + Redis)
    Dockerfile             # distroless multi-stage
    go.mod
  model-gateway/
    Dockerfile             # LiteLLM + AAD-token sidecar
    refresh-aad-token.py
    entrypoint.sh
```

## Quickstart

### 0. Prereqs

- AKS cluster with Workload Identity + OIDC issuer enabled.
- KEDA installed (`keda` namespace) — core scaler + HTTP Add-on.
- Azure resources: ACR, Foundry resource with Claude deployments
  (`claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`),
  Service Bus namespace + queue `agent-pod-claims`, Cosmos DB
  `codeforge` / `sessions`, Redis (Azure Cache for Redis or in-cluster),
  one user-assigned managed identity per role:
  agent-pod, session-router, model-gateway, KEDA.
- Federate each MI to the corresponding K8s ServiceAccount via
  `az identity federated-credential create`.
- Grant the agent-pod and model-gateway MIs the **Azure AI User** role
  on the Foundry resource (or the custom role from the Foundry docs).

### 1. Build & push images

```bash
TAG=1.0.0
ACR=acrtheclouds.azurecr.io

az acr login -n acrtheclouds

docker build -t $ACR/code-forge/agent-pod:$TAG       containers/agent-pod
docker build -t $ACR/code-forge/session-router:$TAG  containers/session-router
docker build -t $ACR/code-forge/model-gateway:$TAG   containers/model-gateway

docker push $ACR/code-forge/agent-pod:$TAG
docker push $ACR/code-forge/session-router:$TAG
docker push $ACR/code-forge/model-gateway:$TAG
```

### 2. Install the chart

```bash
helm upgrade --install code-forge charts/code-forge \
  --create-namespace \
  -f charts/code-forge/values-prod.yaml \
  --set global.azureTenantId=$(az account show --query tenantId -o tsv) \
  --set global.foundry.resource=codeforge-foundry-westus3 \
  --set workloadIdentity.agentPod.clientId=$AGENT_MI_CLIENT_ID \
  --set workloadIdentity.sessionRouter.clientId=$ROUTER_MI_CLIENT_ID \
  --set workloadIdentity.modelGateway.clientId=$GATEWAY_MI_CLIENT_ID \
  --set agentPod.keda.serviceBus.namespace=codeforge-bus.servicebus.windows.net \
  --set agentPod.keda.serviceBus.identityClientId=$KEDA_MI_CLIENT_ID
```

### 3. Verify

```bash
# Warm pool fully ready?
kubectl -n agent-pool get pods -l state=warm

# Router reachable?
kubectl -n session-control port-forward svc/session-router 8080:8080
curl -X POST localhost:8080/sessions -d '{"dev_id":"alice","project_id":"web"}'

# Model gateway healthy?
kubectl -n platform port-forward svc/model-gateway 4000:80
curl localhost:4000/health/readiness
```

### 4. Run Claude Code locally against the same backend

`containers/agent-pod/.devcontainer/devcontainer.json` matches the in-cluster
image. From your laptop:

```bash
export ANTHROPIC_FOUNDRY_RESOURCE=codeforge-foundry-westus3
export ANTHROPIC_FOUNDRY_BASE_URL=https://codeforge.example.com/anthropic   # via the gateway
code .   # opens VS Code, Reopen in Container, claude is installed
```

## Production hardening checklist

- [ ] Budgets — set `modelGateway.budgets.default.maxBudgetUsd` per tenant
- [ ] Pin chart version, image digests
- [ ] Spot-fleet diversification (multiple SKUs)
- [ ] OpenTelemetry traces (router → gateway → Foundry) tagged with
      `session_id`, `dev_id`
- [ ] Audit log shipped to Log Analytics
- [ ] Rotate LiteLLM master key quarterly via CSI driver
- [ ] Disable Claude Code auto-update if you need version pinning
      (`CLAUDE_CODE_DISABLE_AUTOUPDATE=1`)

## What's intentionally NOT here

- **gVisor** — using PSA-restricted + NetworkPolicy + non-root only.
  Add Kata or AKS Confidential Containers if you need stronger isolation.
- **Direct Foundry calls from agent pods** — all traffic goes through the
  gateway so we get a single audit/billing chokepoint.
- **Long-lived API keys mounted in pods** — every credential flows from
  Azure Workload Identity (federated OIDC).

## References

- [Claude Code dev container docs](https://code.claude.com/docs/en/devcontainer)
- [Claude Code on Microsoft Foundry](https://code.claude.com/docs/en/microsoft-foundry)
- [Claude Code LLM gateway](https://code.claude.com/docs/en/llm-gateway)
- [KEDA HTTP Add-on](https://kedacore.github.io/http-add-on/)
- [Azure Workload Identity](https://azure.github.io/azure-workload-identity/docs/)
