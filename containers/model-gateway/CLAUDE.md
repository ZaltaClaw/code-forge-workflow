# CLAUDE.md — Model Gateway

> Loaded when `claude` runs in `containers/model-gateway/`.

## What this is

A [LiteLLM](https://github.com/BerriAI/litellm)-based proxy that sits in front
of Microsoft Foundry. Every Claude Code call from every agent pod goes through
here. It provides:

- **Anthropic Messages API → Foundry translation** via the `/anthropic`
  pass-through endpoint. Claude Code thinks it's talking to `api.anthropic.com`;
  it's actually talking to `cluster.local`.
- **AAD authentication via Workload Identity.** A sidecar Python script
  (`refresh-aad-token.py`) calls `DefaultAzureCredential.get_token()` against
  the `https://cognitiveservices.azure.com/.default` scope every ~50 minutes
  and writes the token to `/etc/aad/env`, which the entrypoint sources before
  exec'ing LiteLLM.
- **Per-dev virtual keys + budgets.** The router mints a fresh virtual key on
  session start with `max_budget_usd`, `tpm`, `rpm` caps. Spend is logged to
  the LiteLLM Postgres so we can do cost showback per `dev_id`.
- **Caching.** Prompt cache is enabled with 1-hour TTL (Foundry pricing
  benefits from this).

## Why a gateway at all

Without one, every agent pod would need its own AAD token + would call Foundry
directly. We'd lose:

- Single audit/billing chokepoint.
- Per-dev rate limits (impossible to enforce client-side).
- Key rotation that doesn't require redeploying every pod.
- Foundry deployment-name → friendly model-name aliasing.
- Prompt cache sharing across the fleet.

## File layout

```
Dockerfile                   # FROM ghcr.io/berriai/litellm:main-stable
                             # + adds azure-identity, msal, our two scripts
refresh-aad-token.py         # the AAD sidecar (Python; runs in background)
entrypoint.sh                # bootstrap: start sidecar, wait for first token,
                             # source env, exec litellm
```

The LiteLLM `config.yaml` is **not** in this dir — it lives in the chart as a
ConfigMap (`charts/code-forge/templates/40-model-gateway.yaml`) so it can use
Helm values for the Foundry resource name + model deployments.

## Build & run

```bash
docker build -t code-forge/model-gateway:dev .
# Local smoke test (needs an Azure session via `az login`):
docker run --rm -p 4000:4000 \
  -e AZURE_CLIENT_ID=… -e AZURE_TENANT_ID=… \
  -v $(pwd)/sample-config.yaml:/etc/litellm/config.yaml \
  code-forge/model-gateway:dev --config /etc/litellm/config.yaml --port 4000
```

## Editing rules

1. **Never log the AAD token.** It's a bearer credential. Logging it = breach.
2. **Refresh interval = `expires_on - 5min`.** Don't go closer than that — Foundry rejects tokens with < 60s TTL pretty aggressively.
3. **Budget caps are advisory, not hard.** Hard cap = the AKS namespace `ResourceQuota`. If you need a hard cost cap, push it down to Cost Management budgets at the Foundry resource level.
4. **Don't add static API keys.** If LiteLLM ever asks for one, you've broken Workload Identity — fix the federation, don't paper over it.

## Pitfalls

- **`Failed to get token` at startup** → the ServiceAccount isn't federated to the gateway MI. Check `kubectl describe sa model-gateway -n platform` for the `azure.workload.identity/client-id` annotation; check `az identity federated-credential list` for the matching subject `system:serviceaccount:platform:model-gateway`.
- **`401 Unauthorized` from Foundry** → either the MI lacks `Azure AI User` role on the Foundry resource, or the token scope is wrong. Should be `https://cognitiveservices.azure.com/.default`, NOT `https://management.azure.com/.default`.
- **`429 Too Many Requests`** → Foundry has a per-deployment TPM cap. Configure in the LiteLLM config to fall back to a secondary deployment in another region.
- **Prompt cache misses unexpectedly** → Foundry cache is **per-deployment**, not per-resource. If you load-balance across two Opus deployments, you halve the cache hit rate.
