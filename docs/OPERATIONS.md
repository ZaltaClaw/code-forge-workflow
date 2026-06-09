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
  - **Sev1**: > 25% of devs can't get a sandbox, OR Foundry budget exceeded by 50% in 1h, OR data exfiltration suspected.
  - **Sev2**: Single-digit-percent error rate, single component down with redundancy intact.
  - **Sev3**: Cosmetic, no user impact.

## Quick triage

```bash
# Cluster-level health
kubectl get pods -A | grep -v Running | grep -v Completed
kubectl top nodes
kubectl get events -A --sort-by='.lastTimestamp' | tail -50

# Code Forge specifics
kubectl -n session-control logs deploy/sandbox-orchestrator --tail=200
kubectl -n agent-sandboxes get sandboxwarmpool,sandboxes,sandboxclaims
kubectl -n agent-sandbox-system logs deploy/agent-sandbox-controller --tail=100
kubectl -n platform logs deploy/model-gateway --tail=200 | grep -E '(refresh-aad|ERROR|429|401)'

# Foundry budget
curl -H "x-litellm-api-key: $LITELLM_MASTER_KEY" \
  https://api.codeforge.example.com/spend/total | jq .
```

## Common P1 patterns

### Devs can't get a sandbox

1. Check orchestrator pod status. `CrashLoopBackOff`? → logs.
2. Check warm pool: `kubectl -n agent-sandboxes get sandboxwarmpool -o wide`. Is `readyReplicas` near `replicas`?
3. If the pool is empty/not filling: check the controller — `kubectl -n agent-sandbox-system logs deploy/agent-sandbox-controller --tail=100`. CRDs installed? (`kubectl get crd | grep agents.x-k8s.io`).
4. If sandboxes are stuck `Pending`: node capacity. `kubectl -n agent-sandboxes describe sandbox <name>` for the `FailedScheduling` event; bump the node pool.
5. Getting HTTP 429? Admission caps hit — raise `sandboxOrchestrator.concurrency.maxTotal` / `maxPerDev`.

### Model gateway 401s

1. `kubectl -n platform logs deploy/model-gateway -c gateway --tail=100 | grep refresh-aad`
2. If "Failed to get token" → ServiceAccount federation broken. `az identity federated-credential list --identity-name codeforge-gateway-mi -g rg-codeforge` and confirm subject = `system:serviceaccount:platform:model-gateway`.
3. If federation is fine, check the MI has `Cognitive Services User` on the Foundry account: `az role assignment list --assignee $GATEWAY_MI_PRINCIPAL_ID --scope $FOUNDRY_RESOURCE_ID`.

### Foundry 429s climbing

- Check per-deployment TPM in Foundry portal. If saturating, add a second deployment in another region and update LiteLLM `config.yaml` to fall back.
- Per-dev RPM cap in LiteLLM is too generous? Tighten `modelGateway.budgets.default.rpmLimit`.

### Sandboxes stuck Pending / pool not filling

- Check the warm pool: `kubectl -n agent-sandboxes get sandboxwarmpool -o wide` — is `readyReplicas` climbing toward `replicas`?
- Check the controller is healthy and has the `--extensions` flag: `kubectl -n agent-sandbox-system get deploy agent-sandbox-controller -o jsonpath='{.spec.template.spec.containers[0].args}'`.
- Node capacity: `kubectl -n agent-sandboxes describe sandbox <name>` and look for `FailedScheduling`.
- After editing the `SandboxTemplate`, live warm pods are NOT auto-recreated — delete them by name so the pool re-stamps: `kubectl -n agent-sandboxes delete sandbox <warm-pod-names>`.

## Scaling

### Increase warm pool / concurrency

```bash
helm upgrade code-forge charts/code-forge \
  -f charts/code-forge/values-prod.yaml \
  --reuse-values \
  --set sandboxOrchestrator.sandbox.warmpoolReplicas=8 \
  --set sandboxOrchestrator.concurrency.maxTotal=200
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
| Foundry usage / cost log (LiteLLM) | LiteLLM DB backup | 15 min | 5 min |
| Sandboxes | Disposable — single-use, no durable state | n/a | n/a |
| Optional per-dev PVCs (Azure Files) | Snapshot daily | 1 h | 24 h |
| Key Vault | Soft-delete + purge protection (90 days) | 1 h | 0 |
| ACR | Geo-replication to a paired region | n/a | tag-bound |
| Helm release | `helm history` (in cluster) + chart in git | minutes | 0 |

DR drill: quarterly. Redeploy the chart (+ agent-sandbox CRDs/controller) in the
paired region's pre-built standby cluster; the warm pool re-fills automatically.

## Useful one-liners

```bash
# How many sandboxes are in flight right now?
kubectl -n agent-sandboxes get sandboxclaims --no-headers | wc -l

# Warm pool readiness
kubectl -n agent-sandboxes get sandboxwarmpool -o jsonpath='{.items[0].status.readyReplicas}/{.items[0].status.replicas}'; echo

# Top spenders this hour
curl -H "x-litellm-api-key: $LITELLM_MASTER_KEY" \
  https://api.codeforge.example.com/spend/users \
  | jq 'sort_by(-.spend)[:10]'

# Force-delete a stuck sandbox + its claim
kubectl -n agent-sandboxes delete sandboxclaim <claim>; kubectl -n agent-sandboxes delete sandbox <name>

# Drain a node gracefully
kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data --grace-period=120
```

## Security incidents

See `docs/SECURITY.md` → "Incident response". Pre-baked playbooks:

- `runbooks/sec-key-leak.md` — TODO
- `runbooks/sec-pod-compromise.md` — TODO
- `runbooks/sec-foundry-budget-blowout.md` — TODO

(These are stubs — file an issue if you have the procedure already.)
