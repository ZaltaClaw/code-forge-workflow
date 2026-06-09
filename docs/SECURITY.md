# Security model

> What we worry about, what we don't, and how the controls fit together.

## Trust boundary

The hard boundary is the **agent sandbox**. Anything inside the sandbox is
treated as a potentially compromised process: a malicious package the developer
installed, a prompt-injection from a malicious repo, a buggy MCP server.

Everything outside the sandbox (orchestrator, gateway, Foundry, Azure plane) is
in a strictly higher trust tier. The controls below enforce one-way trust: the
sandbox cannot reach back into the platform.

## Threat model

| Threat | Asset at risk | Control |
|---|---|---|
| Malicious code in a repo Claude Code reads | Other devs' work, Foundry credentials | Single-use ephemeral sandbox (no shared state); optional per-dev PVC scoped by Azure RBAC; AAD token never on the sandbox |
| Prompt injection makes Claude exfiltrate data | Source code, secrets | NetworkPolicy default-deny egress (only gateway reachable); no `~/.aws`, `~/.azure`, `~/.ssh` mounted |
| Compromised dev laptop | Foundry budget abuse | Per-dev `max_budget` + `rpm` caps enforced at the gateway (`modelGateway.budgets`) |
| Compromised gateway pod | All in-flight Foundry traffic | AAD token sidecar uses Workload Identity, can't be exfiltrated as a static secret; rotated every ~50 min |
| Stolen LiteLLM master key | Ability to mint new keys / change budgets | Master key in Key Vault → CSI driver mount; rotated quarterly; access scoped to gateway MI |
| Orchestrator compromise | Ability to provision/claim sandboxes | Stateless; namespaced RBAC limited to `Sandbox`/`SandboxClaim` in `agent-sandboxes`; cannot read platform secrets |
| AKS API server compromise | Cluster takeover | Private cluster + AAD-only auth + Conditional Access with MFA; audit to Log Analytics |
| Foundry deployment quota exhaustion | DoS for all devs | Multi-deployment + LiteLLM fallback; per-dev RPM cap + orchestrator concurrency caps are the main throttles |
| Sandbox reuse across devs | Cross-dev data leak | Sandboxes are single-use — a claim is deleted on completion/TTL and the warm pool re-stamps a fresh pod from the image |

## Controls — defense in depth

### Identity & access

- **Azure Workload Identity** (federated OIDC) for every workload. No SP secrets, no static API keys, no managed-identity-via-IMDS. One UAMI per workload role with the minimum role on the minimum scope.
- **AAD-only auth on AKS API server.** No local accounts, no kubeconfig sharing.
- **Gateway-enforced budgets** for Foundry traffic. Per-dev `max_budget` + `rpm` caps live in LiteLLM config; tightening a budget is an O(1) config change.

### Network

- **Default-deny `NetworkPolicy`** in `agent-sandboxes` and `platform`.
- Agent sandboxes can reach: cluster DNS + `model-gateway` Service. That's it.
- Gateway can reach: cluster DNS + 443 outbound (to Foundry). Lock further with private endpoint to Foundry.
- Front Door + WAF on the public ingress. mTLS between Front Door and the AKS ingress controller.

### Workload

- **PSA `restricted`** enforced at namespace level. Non-root, no privilege escalation, drop ALL caps, RuntimeDefault seccomp, read-only root FS.
- **No host mounts.** No `~/.ssh`, no `/var/run/docker.sock`, no host network, no hostPID, no hostIPC.
- **Single-use sandboxes.** A sandbox serves one claim, then the claim is deleted and the pod is discarded — no workspace survives across requests.
- **Resource quotas** per namespace + LimitRange to prevent runaway pods exhausting the node.

### Data

- **Audit / cost log** lives in the model-gateway (LiteLLM): `dev_id`, `model`, `tokens_in`, `tokens_out`, `spend`, timestamps. CMK-backed store.
- **No session-state datastore.** The orchestrator is stateless; sandbox state lives only in the kube API (`SandboxClaim`/`Sandbox` objects) and is torn down on completion.
- **Optional per-dev PVCs** on Azure Files with CMK, ACL'd per dev via Azure RBAC on the share (only if durable scratch is enabled).
- **Key Vault** for the LiteLLM master key, TLS certs, any other root-of-trust secrets. Soft-delete + purge protection.

### Supply chain

- **Pinned image digests** in production values. Tags are human-friendly; digests are what runs.
- **`@anthropic-ai/claude-code`** installed at image build time. We don't blindly run `npm i -g` at pod startup.
- **ACR with content-trust** + image scanning (Defender for Containers). Block-on-critical CVE.

### Operations

- **Audit log** of every orchestrator decision, every gateway call. Shipped to Log Analytics + retained 90 days.
- **Cost alerts** at 50/80/100% of monthly Foundry budget per environment.
- **Quarterly rotation** of LiteLLM master key, AKS local accounts disabled.
- **Pen test** annually + scoped pen test on the gateway after major changes.

## Explicit non-goals

We chose **not** to do these — at least not yet — and the reasoning:

| Non-goal | Why |
|---|---|
| **gVisor / Kata sandboxing** | Adds operational complexity; threat model already addressed by PSA + NetworkPolicy + non-root + ephemeral FS. Revisit if we onboard customers with stricter compliance |
| **Per-tenant cluster** | Cost — multi-tenancy via namespace + RBAC + NetworkPolicy is industry-standard for this trust level |
| **End-to-end encryption of the I/O channel** | TLS terminates at Front Door; cluster-internal is plaintext over the AKS service network. If we move to a service mesh (Istio mTLS) this is free; until then, the threat model treats the cluster as a single trust zone |
| **Customer keys for Foundry** | One Foundry resource per environment; per-tenant data isolation enforced via gateway budgets + per-dev tagging in the audit log |

## Incident response

See `docs/OPERATIONS.md` → "Security incidents". Quick map:

- **Suspected key leak** → tighten/zero the dev's budget in LiteLLM, rotate the LiteLLM master key, audit the gateway log for the `dev_id`.
- **Suspected sandbox compromise** → cordon the node, capture the pod (`kubectl debug`), delete the `SandboxClaim`/`Sandbox`; the warm pool re-stamps clean.
- **Suspected gateway compromise** → scale gateway to 0, rotate AAD federation, force AAD secret rotation, redeploy.

## Reporting

Found something? `security@codeforge.example.com` or DM Roey or Michael
on the team channel. Don't open a public issue.
