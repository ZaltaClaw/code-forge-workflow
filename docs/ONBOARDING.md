# Onboarding — 30 minutes from clone to first PR

Welcome. By the end of this doc you will:

- Understand what Code Forge does and why
- Have the repo cloned + tools installed
- Have a local Claude Code session running against our Foundry
- Have shipped a one-line PR to verify the loop

If anything below is wrong or stale, **fix it in the same PR you ship**.
That's the contract.

## 1 · The 90-second pitch

Code Forge runs Claude Code as a managed service on AKS. Developers anywhere
in the org get a warm, ephemeral Linux container with `claude` pre-installed,
talking to Microsoft Foundry through a single audited gateway. Per-dev
budgets, no long-lived API keys, sub-2-second cold start.

Read `CLAUDE.md` at the repo root for the full context. Then come back here.

## 2 · Tools you need

| Tool | Why | Install |
|---|---|---|
| Docker Desktop / Rancher / Colima | Build the agent-pod image locally | brew, Docker.com |
| `az` | Talk to our Azure subscription | `brew install azure-cli` |
| `kubectl` ≥ 1.30 | Cluster ops | `brew install kubectl` |
| `helm` ≥ 3.14 | Deploy the chart | `brew install helm` |
| `bicep` | IaC | `az bicep install` |
| `python` ≥ 3.11 | Sandbox orchestrator | `brew install python` |
| `claude` | The CLI itself, for local dev | `npm install -g @anthropic-ai/claude-code` |
| VS Code + Dev Containers extension | Open `containers/agent-pod/.devcontainer` for prod-parity local dev | Marketplace |

## 3 · Get access

Ask Roey (`@ZaltaClaw`) or Michael (`@michaelliav`) for:

1. Repo invite to `ZaltaClaw/code-forge-workflow` (admin or write).
2. Membership in the `Code Forge Developers` AAD group — gates Foundry calls.
3. (If you need cluster access) Membership in the `aks-codeforge-dev-readers` group.

Then:

```bash
gh auth login              # use your GitHub account
git clone git@github.com:ZaltaClaw/code-forge-workflow.git
cd code-forge-workflow
az login
kubectl config use-context aks-codeforge-dev   # only if you have cluster access
```

## 4 · Run Claude Code locally against our Foundry

This is the fastest way to see the system end-to-end. You'll point your local
`claude` at our cluster's gateway via port-forward.

```bash
# 4a. Get your virtual key (Roey or Michael will mint one for you)
export ANTHROPIC_AUTH_TOKEN=sk-...

# 4b. Forward the gateway service to localhost
kubectl -n platform port-forward svc/model-gateway 4000:80 &

# 4c. Point Claude Code at it via the Foundry env vars
export CLAUDE_CODE_USE_FOUNDRY=1
export ANTHROPIC_FOUNDRY_BASE_URL=http://localhost:4000/anthropic
export ANTHROPIC_FOUNDRY_RESOURCE=codeforge-foundry-westus3
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5

# 4d. Run claude in this repo
cd ~/code-forge-workflow
claude
```

You should see Claude pick up `CLAUDE.md` automatically. Try:

> Read CLAUDE.md and tell me what the three primitives are.

If that works, the loop is healthy.

## 5 · Run the dev container locally

```bash
code containers/agent-pod
# VS Code prompts: Reopen in Container → yes
# claude is pre-installed in the rebuilt container.
```

This is the same image (modulo the Feature install path) that runs in prod.
Use it to repro bugs that only show up in containerized environments.

## 6 · Make a one-line PR

The convention here: every new contributor's first PR is a small, real fix.

Pick one:

- Find a typo in this doc and fix it
- Add yourself to the "Who runs this" section in the root `CLAUDE.md`
- Improve a `Pitfalls` section in any subdir `CLAUDE.md` with something you stumbled on during steps 1–5

```bash
git checkout -b yourname/onboarding-fix
# … edit …
git commit -am "docs: <what you changed>"
gh pr create --base main --title "docs: <what>" --body "First PR per ONBOARDING.md."
```

Tag Roey or Michael for review. Merge bar: green CI + one review.

## 7 · Where to go next

| Goal | Read |
|---|---|
| Understand the architecture | `docs/ARCHITECTURE.md` |
| Change something in the chart | `charts/code-forge/CLAUDE.md` |
| Change the agent image | `containers/agent-pod/CLAUDE.md` |
| Add an Azure resource | `infra/CLAUDE.md` |
| Run an incident | `docs/OPERATIONS.md` |
| Threat-model something | `docs/SECURITY.md` |

## 8 · Things that surprise people

- **Sandboxes are completely cattle.** Don't `kubectl exec` into one and `vim` config — it's single-use and gone the moment your request finishes.
- **`api.anthropic.com` doesn't appear in our network policies.** Agents talk to the *gateway*, not Anthropic. The gateway then talks to *Foundry*, not Anthropic.
- **Static API keys will fail review.** Workload Identity, every time. If you can't figure out how to wire it, ask — don't paper over with a key.
- **`pod-security.kubernetes.io/enforce: restricted`** rejects images that run as root. If a build of yours pods-pending into oblivion, that's usually why.
- **The warm pool is a CRD, not a Deployment.** Sizing lives in `sandboxOrchestrator.sandbox.warmpoolReplicas`; editing the `SandboxTemplate` won't recycle live warm pods (delete them by name to re-stamp).

Welcome aboard. 🛠️
