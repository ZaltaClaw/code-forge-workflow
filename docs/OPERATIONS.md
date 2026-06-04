# Operations runbook

> Day-2 stuff. P1 procedures, scaling, rotations, common SRE tasks.

## Prerequisites (one-time, per subscription)

### Anthropic / Claude on Foundry — Marketplace terms acceptance

Claude models are a **Marketplace SaaS offer**. Before `infra/modules/foundry.bicep`
can deploy the `claude-*` deployments, the offer's terms must be accepted **once
per subscription**. This is **not** an ARM property and **not** a callable REST
path — it cannot be automated inside the Bicep deploy (an earlier draft tried a
`deploymentScripts` curl hack; it was removed because no such stable endpoint
exists). Attempting to deploy the model before accepting terms fails with a
`MarketplacePurchaseEligibilityFailed` / `SkuNotAvailable` style error.

Requirements:

- **Subscription type**: Enterprise Agreement or MCA-E (pay-as-you-go/MSDN are
  not eligible for the Anthropic offer).
- **Region**: `eastus2` or `swedencentral` only. The infra pins `eastus2`.

Accept the terms once, before the first `az deployment sub create`:

```bash
# Portal path: Foundry portal → Model catalog → Claude → "Agree & continue"
#   on the Marketplace terms dialog (per subscription, one time).
#
# CLI path (if the offer is surfaced as an Azure Marketplace term):
az term accept \
  --publisher anthropic \
  --product anthropic-claude-foundry \
  --plan claude          # confirm publisher/product/plan IDs in the portal first

# Verify before deploying:
az term show --publisher anthropic --product anthropic-claude-foundry --plan claude \
  --query accepted -o tsv   # → true
```

> If `az term` does not list the offer for your tenant, use the portal flow —
> it is the canonical path. Treat this as a hard gate in the deploy runbook.

## On-call basics

- **Pager**: Code Forge SRE rotation (PagerDuty service `code-forge-prod`).
- **Sev mapping**:
  - **Sev1**: > 25% of devs can't claim a session, OR Foundry budget exceeded by 50% in 1h, OR data exfiltration suspected.
  - **Sev2**: Single-digit-percent error rate, single component down with redundancy intact.
  - **Sev3**: Cosmetic, no user impact.

## Quick triage

```bash
# Cluster-level health
kubectl get pods -A | grep -v Running | grep -v Completed
kubectl top nodes
kubectl get events -A --sort-by='.lastTimestamp' | tail -50

# Code Forge specifics
kubectl -n session-control logs deploy/session-router --tail=200
kubectl -n agent-pool get pods -l app=agent-pod -L state -L dev-id
kubectl -n platform logs deploy/model-gateway --tail=200 | grep -E '(refresh-aad|ERROR|429|401)'

# Foundry budget
curl -H "x-litellm-api-key: $LITELLM_MASTER_KEY" \
  https://api.codeforge.example.com/spend/total | jq .
```

## Common P1 patterns

### Devs can't claim sessions

1. Check session-router pod status. `CrashLoopBackOff`? → logs.
2. Check warm pool: `kubectl -n agent-pool get pods -l state=warm | wc -l`. Should be ≥ `agentPod.keda.minReplicas`.
3. If pool is empty: `kubectl describe scaledobject agent-pod -n agent-pool` — KEDA scaler healthy?
4. If KEDA is healthy but pods are `Pending`: spot capacity. Bump `agentPool` node-pool with on-demand fallback.

### Model gateway 401s

1. `kubectl -n platform logs deploy/model-gateway -c gateway --tail=100 | grep refresh-aad`
2. If "Failed to get token" → ServiceAccount federation broken. `az identity federated-credential list --identity-name codeforge-gateway-mi -g rg-codeforge` and confirm subject = `system:serviceaccount:platform:model-gateway`.
3. If federation is fine, check the MI has `Cognitive Services User` on the Foundry account: `az role assignment list --assignee $GATEWAY_MI_PRINCIPAL_ID --scope $FOUNDRY_RESOURCE_ID`.

### Foundry 429s climbing

- Check per-deployment TPM in Foundry portal. If saturating, add a second deployment in another region and update LiteLLM `config.yaml` to fall back.
- Per-dev RPM cap in LiteLLM is too generous? Tighten `modelGateway.budgets.default.rpmLimit`.

### Spot eviction storm

- Check Service Bus `agent-pod-claims` queue depth — if it's growing, KEDA is asking for pods that can't schedule.
- Tactical: scale `agentPod` `nodeSelector` to a non-spot node pool (helm value override + `helm upgrade`).
- Strategic: diversify spot SKUs in the AKS node pool spec.

## Scaling

### Increase warm pool baseline

```bash
helm upgrade code-forge charts/code-forge \
  -f charts/code-forge/values-prod.yaml \
  --reuse-values \
  --set agentPod.replicas=120 \
  --set agentPod.keda.minReplicas=120
```

### Add a new model

1. Create the Foundry deployment (Azure portal or CLI).
2. Add it to `values.yaml` `global.foundry.models.<alias>`.
3. Add a `model_list` entry in the LiteLLM ConfigMap (`templates/40-model-gateway.yaml`).
4. `helm upgrade`. Verify via `curl /v1/models`.

## Rotations

### LiteLLM master key (quarterly)

```bash
NEW=$(openssl rand -hex 32)
az keyvault secret set --vault-name kv-codeforge --name litellm-master --value "sk-$NEW"
# CSI driver picks it up on next pod restart:
kubectl -n platform rollout restart deploy/model-gateway
# Cycle all virtual keys (router will mint new ones on next claim):
kubectl -n agent-pool delete secret -l app=agent-pod
```

### TLS cert (quarterly, automated via cert-manager)

If cert-manager is wired correctly, this is no-op. If it isn't:

```bash
az keyvault certificate import --vault-name kv-codeforge --name api-codeforge --file new.pfx
```

### MI federation rotation (annually)

```bash
az identity federated-credential update \
  --identity-name codeforge-gateway-mi -g rg-codeforge \
  --name k8s-fed --subject system:serviceaccount:platform:model-gateway \
  --issuer $(az aks show -n aks-codeforge-prod -g rg-codeforge --query oidcIssuerProfile.issuerUrl -o tsv)
```

## Backup & DR

| Thing | Backup | RTO | RPO |
|---|---|---|---|
| Cosmos session audit | Continuous backup, 30 days | 15 min | 5 min |
| Redis | Replication only — disposable | n/a | session-bound |
| Workspace PVCs (Azure Files) | Snapshot daily | 1 h | 24 h |
| Key Vault | Soft-delete + purge protection (90 days) | 1 h | 0 |
| ACR | Geo-replication to a paired region | n/a | tag-bound |
| Helm release | `helm history` (in cluster) + chart in git | minutes | 0 |

DR drill: quarterly. Restore Cosmos from PITR + redeploy the chart in the
paired region's pre-built standby cluster.

## Useful one-liners

```bash
# How many devs are bound right now?
kubectl -n agent-pool get pods -l state=bound -o jsonpath='{.items[*].metadata.labels.dev-id}' \
  | tr ' ' '\n' | sort -u | wc -l

# Top spenders this hour
curl -H "x-litellm-api-key: $LITELLM_MASTER_KEY" \
  https://api.codeforge.example.com/spend/users \
  | jq 'sort_by(-.spend)[:10]'

# Force-evict a stuck dev
kubectl -n agent-pool label pod $POD state=cooldown --overwrite

# Drain a node gracefully
kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data --grace-period=120
```

## Security incidents

See `docs/SECURITY.md` → "Incident response". Pre-baked playbooks:

- `runbooks/sec-key-leak.md` — TODO
- `runbooks/sec-pod-compromise.md` — TODO
- `runbooks/sec-foundry-budget-blowout.md` — TODO

(These are stubs — file an issue if you have the procedure already.)
