# CLAUDE.md — Infrastructure (Bicep)

> Loaded when `claude` runs in `infra/`.

## What lives here

Bicep IaC for the Azure underlay. The chart deploys the **workloads** on top
of an AKS cluster — this directory deploys the **AKS cluster itself plus every
managed Azure service the workloads talk to**:

| Module | What it makes |
|---|---|
| `network.bicep` | VNet + subnets (AKS, private endpoints, Application Gateway) |
| `identity.bicep` | User-assigned managed identities (one per workload role) + federated credentials |
| `acr.bicep` | Azure Container Registry; `AcrPull` role to AKS kubelet identity |
| `aks.bicep` | AKS cluster + system pool + spotagents pool. OIDC issuer + Workload Identity enabled |
| `keyvault.bicep` | Key Vault for LiteLLM master key, TLS certs |
| `storage.bicep` | Azure Files share for per-dev workspace PVCs (RWX) |
| `servicebus.bicep` | Service Bus namespace + `agent-pod-claims` queue (KEDA scaler) |
| `logging.bicep` | Log Analytics + Application Insights + diagnostic settings on AKS, ACR, KV |
| `foundry.bicep` | Microsoft Foundry resource + Anthropic deployments (Opus/Sonnet/Haiku) |
| `frontdoor.bicep` | Azure Front Door + WAF for the `api.codeforge.example.com` ingress |

## Deploy

```bash
az login
az account set --subscription "the-clouds"
az deployment sub create \
  --location westus3 \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

`main.bicep` is **subscription-scoped** (`targetScope = 'subscription'`) — it
creates the resource group, then deploys each module into it.

## Editing rules

1. **One module per concern.** Don't mix Cosmos + Service Bus in the same module just because they're both "messaging". Future-you will untangle it.
2. **Outputs are the contract.** Every module emits the IDs / endpoints downstream modules need. Never read another resource group's state by name — wire it through `outputs`.
3. **Tag everything** with `tags: { project: 'code-forge', env: 'prod' }` so cost showback works.
4. **`az deployment what-if` before applying.** Wired in `make infra-whatif`.

## Pitfalls

- **OIDC issuer URL** — must be enabled on the AKS cluster *before* you create federated credentials on the MIs. The Bicep handles the order, but if you redeploy and AKS gets recreated, the issuer URL changes and federations break.
- **Foundry deployments are a separate dance** from the resource. The Bicep creates the resource; deployments need `az cognitiveservices account deployment create`. There's a `deploymentScript` resource at the bottom of `foundry.bicep` that automates it.
- **Service Bus + KEDA** — KEDA's MI needs `Azure Service Bus Data Receiver`, not `Owner`. Tighter scope = fewer alerts during pen tests.
