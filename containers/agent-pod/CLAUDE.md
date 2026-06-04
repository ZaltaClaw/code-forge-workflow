# CLAUDE.md — Agent Pod container

> Loaded when `claude` runs in `containers/agent-pod/`.

## What this is

The container image developers actually run their Claude Code session inside.
Built on `mcr.microsoft.com/devcontainers/base:ubuntu-22.04` so it's compatible
with the [Dev Containers spec](https://containers.dev/) — VS Code, Codespaces,
JetBrains, and Cursor can all open it.

## How Claude Code gets installed

Two parallel paths that both produce the same `claude` binary:

1. **Production image** (`Dockerfile`):
   `RUN npm install -g @anthropic-ai/claude-code@latest`
   This is what the [Claude Code Dev Container Feature](https://github.com/anthropics/devcontainer-features/tree/main/src/claude-code)
   does internally; we inline it so the image is self-contained and we
   don't depend on the Feature's runtime install.

2. **Local dev parity** (`.devcontainer/devcontainer.json`):
   References the same image AND adds the official Feature
   `ghcr.io/anthropics/devcontainer-features/claude-code:1.0` so any
   improvements upstream apply to local dev automatically.

## Foundry wiring

Claude Code reads these env vars (set by the chart):

```bash
CLAUDE_CODE_USE_FOUNDRY=1
ANTHROPIC_FOUNDRY_BASE_URL=http://model-gateway.platform.svc.cluster.local/anthropic
ANTHROPIC_FOUNDRY_RESOURCE=<foundry-resource>
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5
ENABLE_PROMPT_CACHING_1H=1
```

Note: agent pods point at the **in-cluster gateway**, not at Foundry directly.
That's the chokepoint that holds the AAD token, enforces budgets, and audits
every request.

## State machine (label-driven)

```
        ┌──────────────┐  router patches    ┌──────────────┐
        │ state=warm   │ ─────────────────▶ │ state=bound  │
        │ (idle pool)  │  on dev claim       │ (in session) │
        └──────────────┘                     └──────────────┘
                ▲                                    │
                │                          idle 15min│
                │                                    ▼
        ReplicaSet replaces                ┌──────────────┐
        with fresh warm pod                │ state=cooldown│
                │                          │  preStop +    │
                └────── pod deleted ◀──────│  exit         │
                                           └──────────────┘
```

The pod participates in this dance via three things:

- `agent-entrypoint` — runs an idle watchdog that monitors `/workspace/.last-activity`. If `IDLE_TIMEOUT_SECONDS` elapses without a bump, calls `agent-shutdown` and exits.
- `agent-shutdown` — preStop hook + idle-timeout cleanup. Kills `claude` processes, scrubs `/workspace` contents (preserves the `.claude` config volume), wipes session caches.
- `healthz.py` — tiny HTTP server on `:8081`. `GET /healthz` for kubelet probes; `POST /activity` for the router to bump the activity timer.

## Editing rules

1. **Stay non-root.** `USER agent` in the Dockerfile, `runAsNonRoot: true` in the chart, `readOnlyRootFilesystem: true`. If you need writable disk, use `/workspace` (PVC) or `/tmp` (emptyDir).
2. **No host secrets.** Never mount `~/.ssh`, `~/.kube`, `~/.azure`, etc. into this container. Auth flows through Workload Identity only.
3. **Pin Claude Code if you need reproducibility.** The image installs `@latest`. Set `CLAUDE_CODE_DISABLE_AUTOUPDATE=1` and pin a version in the Dockerfile if your security review requires it.
4. **Test devcontainer parity.** After image changes, open `containers/agent-pod/.devcontainer` in VS Code and rebuild — if it works there, prod will work too.

## Pitfalls

- **`claude` first-run hangs on auth** — only happens if `CLAUDE_CODE_USE_FOUNDRY` isn't set. Foundry path skips the browser flow entirely.
- **Spot eviction kills mid-session work** — `preStop` runs but only has 30s. The router catches the eviction signal (Service Bus message) and rebinds the dev to a new pod with the same workspace PVC. Make sure that flow is wired before adopting spot in prod.
- **`/workspace` fills up** — Claude Code session history + node_modules. Either bump `agentPod.workspace.size` or add a periodic GC sidecar.
