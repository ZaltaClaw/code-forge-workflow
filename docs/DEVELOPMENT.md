# Development loop

> How to make a change, test it, and ship it.

## TL;DR

```bash
# Edit something
$EDITOR charts/code-forge/values.yaml

# Verify
make chart-lint
make chart-template | less

# Open a PR
git checkout -b yourname/feature
git commit -am "feat: <change>"
gh pr create --fill
```

CI runs: `helm lint`, `helm template`, `pytest` (orchestrator), Bicep `what-if`,
hadolint, and trivy on the resulting images. Green CI + 1 review = mergeable.

## Where to make each kind of change

| You want to | Edit |
|---|---|
| Bump warm-pool size | `charts/code-forge/values.yaml` `sandboxOrchestrator.sandbox.warmpoolReplicas` |
| Add a model | `values.yaml` `global.foundry.models` + LiteLLM ConfigMap |
| Add an env var to agent sandboxes | `_helpers.tpl` (`code-forge.claudeCodeFoundryEnv`) |
| Tighten a network rule | `templates/50-network-policies.yaml` |
| Change orchestrator behavior | `containers/sandbox-orchestrator/sandbox_orchestrator/` |
| Add an Azure service | `infra/modules/<thing>.bicep` + reference from `main.bicep` |
| Add a new doc | `docs/<TOPIC>.md` + link from root `CLAUDE.md` |

## Local dev loops

### 1. Helm changes

```bash
make chart-template > /tmp/before.yaml
$EDITOR charts/code-forge/templates/<file>.yaml
make chart-template > /tmp/after.yaml
diff /tmp/before.yaml /tmp/after.yaml | less
```

### 2. Agent-pod image changes

```bash
docker build -t code-forge/agent-pod:dev containers/agent-pod
docker run --rm -it code-forge/agent-pod:dev claude --version
# Optional: open in VS Code dev container for full IDE
code containers/agent-pod
```

### 3. Sandbox-orchestrator changes

```bash
cd containers/sandbox-orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
# Run locally against a kubeconfig (uses your current context):
uvicorn sandbox_orchestrator.api:app --reload --port 8080
```

### 4. Bicep changes

```bash
cd infra
bicep build main.bicep
az deployment sub what-if \
  --location westus3 \
  --template-file main.bicep \
  --parameters main.bicepparam
```

## Working WITH Claude Code in this repo

This is the meta loop — Claude Code editing Code Forge.

```bash
cd ~/code/code-forge-workflow
claude
```

Claude reads the root `CLAUDE.md` automatically. When you `cd` into a subdir
inside the session, Claude picks up the nearest `CLAUDE.md` too. The
documentation tree is structured for exactly this — every directory it might
work in has its own brief.

Try:

> /init                       — refresh CLAUDE.md if you've reorganized
> /commands                   — list project-specific slash commands
> Refactor the orchestrator idle reaper to use an async task group.
> Find every place we hardcode 'agent-sandboxes' and route it through values.yaml.

The slash commands in `.claude/commands/` are tuned for the most common chores:

- `/render-chart` — `helm template` with sane defaults
- `/lint-everything` — runs all linters in parallel
- `/new-doc <name>` — scaffold a new doc page with our format
- `/build-images` — Docker build all three images (agent-pod, sandbox-orchestrator, model-gateway)
- `/security-review` — opens `docs/SECURITY.md` and asks Claude to red-team a change

## Style conventions

- **Comments explain WHY, not what.** The code says what.
- **YAML keys ordered top-down by audience importance** (in `values.yaml`: global → agent → router → gateway → infra-y stuff). Don't alphabetize.
- **Imperative commit messages** ("add X", not "adds X").
- **Conventional commits** for the type prefix: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `infra:`.
- **One logical change per PR.** Splitting is cheap; rebasing a 30-file PR is not.

## CI

`.github/workflows/` (TODO — not yet committed):

| Workflow | Trigger | Steps |
|---|---|---|
| `chart-ci.yaml` | PR touching `charts/**` | `helm lint`, `helm template`, kubeconform |
| `orchestrator-ci.yaml` | PR touching `containers/sandbox-orchestrator/**` | `pytest`, `ruff`, `mypy` |
| `image-ci.yaml` | PR touching `containers/**` | `hadolint`, `docker build`, `trivy` scan |
| `bicep-ci.yaml` | PR touching `infra/**` | `bicep build`, `az deployment what-if` against a sandbox sub |
| `release.yaml` | Tag push | Build + push images to ACR, helm package, helm push |

## Testing strategy

- **Helm**: `helm template | kubeconform` covers schema. Snapshot-test the rendered output for important diffs.
- **Orchestrator**: unit tests with `pytest` + a fake kube backend; admission/concurrency caps and claim lifecycle are covered in `containers/sandbox-orchestrator/tests/`.
- **Gateway**: integration test with a fake Foundry-like server that returns canned Anthropic responses.
- **End-to-end**: spin up a kind cluster + install the agent-sandbox CRDs/controller + the chart against a dev Foundry resource. Run a synthetic provision → command → teardown loop.
