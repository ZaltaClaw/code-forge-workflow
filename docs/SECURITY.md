# Security model

> What we worry about, what we don't, and how the controls fit together.

## Trust boundary

The hard boundary is the **agent pod**. Anything inside the pod is treated as
a potentially compromised process: a malicious package the developer
installed, a prompt-injection from a malicious repo, a buggy MCP server.

Everything outside the pod (router, gateway, Foundry, Azure plane) is in a
strictly higher trust tier. The controls below enforce one-way trust: the pod
cannot reach back into the platform.

## Threat model

| Threat | Asset at risk | Control |
|---|---|---|
| Malicious code in a repo Claude Code reads | Other devs' workspaces, Foundry credentials | Per-pod ephemeral workspace; PVC scoped to one `(dev, project)`; AAD token never on the pod |
| Prompt injection makes Claude exfiltrate data | Source code, secrets | NetworkPolicy default-deny egress (only gateway reachable); no `~/.aws`, `~/.azure`, `~/.ssh` mounted |
| Compromised dev laptop | Foundry budget abuse | Per-dev virtual keys with hard `max_budget` + `rpm` caps in LiteLLM |
| Compromised gateway pod | All in-flight Foundry traffic | AAD token sidecar uses Workload Identity, can't be exfiltrated as a static secret; rotated every ~50 min |
| Stolen LiteLLM master key | Ability to mint new virtual keys | Master key in Key Vault → CSI driver mount; rotated quarterly; access scoped to gateway MI |
| Leaked agent virtual key | Spend up to that key's `max_budget` | Per-session, short-lived (TTL = idle timeout); revoked on session release |
| AKS API server compromise | Cluster takeover | Private cluster + AAD-only auth + Conditional Access with MFA; audit to Log Analytics |
| Foundry deployment quota exhaustion | DoS for all devs | Multi-deployment + LiteLLM fallback; per-dev RPM cap is the main throttle |
| Spot-eviction-driven session migration | Mid-flight tokens leak across sessions | preStop hook scrubs `/workspace`; new pod starts from a fresh image; PVC content survives but is per-`(dev,project)` |

## Controls — defense in depth

### Identity & access

- **Azure Workload Identity** (federated OIDC) for every workload. No SP secrets, no static API keys, no managed-identity-via-IMDS. One UAMI per workload role with the minimum role on the minimum scope.
- **AAD-only auth on AKS API server.** No local accounts, no kubeconfig sharing.
- **Per-session virtual keys** for Foundry traffic. Revocable in O(1) by deleting the K8s secret.

### Network

- **Default-deny `NetworkPolicy`** in `agent-pool` and `platform`.
- Agent pods can reach: cluster DNS + `model-gateway` Service. That's it.
- Gateway can reach: cluster DNS + 443 outbound (to Foundry). Lock further with private endpoint to Foundry.
- Front Door + WAF on the public ingress. mTLS between Front Door and the AKS ingress controller.

### Workload

- **PSA `restricted`** enforced at namespace level. Non-root, no privilege escalation, drop ALL caps, RuntimeDefault seccomp, read-only root FS.
- **No host mounts.** No `~/.ssh`, no `/var/run/docker.sock`, no host network, no hostPID, no hostIPC.
- **Ephemeral workspace** wiped on every session release.
- **Resource quotas** per namespace + LimitRange to prevent runaway pods exhausting the node.

### Data

- **Cosmos** session audit: `dev_id`, `pod_name`, `bound_at`, `released_at`, `tokens_in`, `tokens_out`. Customer-managed key (CMK) on the account.
- **Redis** is hot-path only — no PII, just session ↔ pod mapping. TTL-bounded.
- **Workspace PVCs** on Azure Files with CMK. Per-`(dev, project)` ACL via Azure RBAC on the share.
- **Key Vault** for the LiteLLM master key, TLS certs, any other root-of-trust secrets. Soft-delete + purge protection.

### Supply chain

- **Pinned image digests** in production values. Tags are human-friendly; digests are what runs.
- **`@anthropic-ai/claude-code`** installed at image build time. We don't blindly run `npm i -g` at pod startup.
- **ACR with content-trust** + image scanning (Defender for Containers). Block-on-critical CVE.

### Operations

- **Audit log** of every router decision, every gateway call. Shipped to Log Analytics + retained 90 days.
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
| **Customer keys for Foundry** | One Foundry resource per environment; per-tenant data isolation enforced via virtual keys + Cosmos partition keys |

## Incident response

See `docs/OPERATIONS.md` → "Security incidents". Quick map:

- **Suspected key leak** → revoke virtual keys (`router /admin/revoke`), rotate LiteLLM master, audit Cosmos for the `dev_id`.
- **Suspected pod compromise** → cordon the node, capture pod (`kubectl debug`), force-recycle the pool.
- **Suspected gateway compromise** → scale gateway to 0, rotate AAD federation, force AAD secret rotation, redeploy.

## Reporting

Found something? `security@codeforge.example.com` or DM Roey or Michael
on the team channel. Don't open a public issue.
