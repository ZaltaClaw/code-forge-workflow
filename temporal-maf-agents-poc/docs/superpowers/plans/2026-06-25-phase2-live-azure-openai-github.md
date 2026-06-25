# Phase 2 — Live Azure OpenAI + GitHub Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase-1 mocks for the planner, github, and approval agents with real Azure OpenAI reasoning (via Microsoft Agent Framework) and real GitHub branch/plan-file/PR writes, without changing any Temporal workflow, contract, or KEDA code.

**Architecture:** All new I/O lives in activity-side code reached through the existing `shared/maf.py` seam. `shared/maf.py` gains a real `run_live_agent` that drives an Azure OpenAI–backed MAF agent with a per-agent Pydantic `response_format`; a new `shared/github.py` performs idempotent, guarded GitHub writes. Each agent module gains a Pydantic response model, a `build_prompt`, and an async `to_output` mapper. `AGENT_MODE` (mock|live) stays the single switch; the AKS agent stays mock by simply not providing live wiring.

**Tech Stack:** Python 3.10+, Temporal Python SDK (`temporalio`), Microsoft Agent Framework (`agent-framework` / `agent_framework.openai.OpenAIChatClient`), `azure-identity` (`DefaultAzureCredential`), `PyGithub`, `pydantic` v2, `pytest` + `pytest-asyncio`.

## Global Constraints

- **Determinism rule:** No LLM / Azure / GitHub calls in any `*/workflows.py`, `shared/contracts.py`, `shared/config.py`, `shared/child.py`. All live I/O is activity-side only. (Do not import `agent_framework`, `azure`, `github`, or `pydantic` models from workflow modules.)
- **Default unchanged:** `AGENT_MODE` defaults to `mock`. All 16 existing Phase-1 tests must stay green.
- **Live extras stay optional at runtime:** mock mode must run/import without `agent-framework`, `azure-identity`, or `PyGithub` installed. Import those three lazily, inside the live code paths only. (`pydantic` is the one exception — it moves into base requirements because response models are defined at agent-module import time.)
- **MAF API (current, verified Jan 2026):** `from agent_framework.openai import OpenAIChatClient`; `client = OpenAIChatClient(model=<deployment>, azure_endpoint=<endpoint>, api_version=<ver>, api_key=<key>)` or `credential=<DefaultAzureCredential>`; `agent = client.as_agent(name=..., instructions=...)`; `result = await agent.run(prompt, options={"response_format": <PydanticModel>})`; `result.value` is the parsed model instance or `None` on validation failure.
- **GitHub writes are idempotent:** fixed `branch = feat/<request_id>`, fixed `path = docs/agent-plan-<request_id>.md`; every step is check-then-act so a Temporal retry after partial success converges.
- **Write guard fail-closed:** in live mode, refuse GitHub writes unless `GITHUB_ALLOWED_OWNER` matches the repo owner.
- **Error classification:** transient (5xx, rate-limit, timeout, `result.value is None`) → raise so Temporal layer-1 retry handles it; permanent (auth, 404, 422, guard violation) → return `status=failed, retryable=False, next_action=fail` (github) or raise `ApplicationError(non_retryable=True)` (azure).
- Run all commands from the project root: `temporal-maf-agents-poc/`. Tests use `PYTHONPATH=src`.

---

### Task 1: Dependencies + Phase-2 config

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-live.txt`
- Modify: `pyproject.toml` (the `[project.optional-dependencies] live` list)
- Modify: `src/shared/config.py` (add fields to `Settings` + reads in `get_settings`)
- Test: `tests/test_config_phase2.py`

**Interfaces:**
- Produces: `Settings` gains fields `azure_openai_endpoint: str | None`, `azure_openai_deployment: str | None`, `azure_openai_api_version: str`, `azure_openai_api_key: str | None`, `github_token: str | None`, `github_allowed_owner: str | None`. `get_settings()` signature is unchanged (still `() -> Settings`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_phase2.py`:

```python
from __future__ import annotations

from shared.config import get_settings


def test_phase2_defaults(monkeypatch):
    for var in (
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_DEPLOYMENT",
        "AZURE_OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_ALLOWED_OWNER",
    ):
        monkeypatch.delenv(var, raising=False)
    s = get_settings()
    assert s.azure_openai_endpoint is None
    assert s.azure_openai_deployment is None
    assert s.azure_openai_api_key is None
    assert s.github_token is None
    assert s.github_allowed_owner is None
    assert s.azure_openai_api_version  # has a non-empty default


def test_phase2_reads_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")
    s = get_settings()
    assert s.azure_openai_endpoint == "https://x.openai.azure.com"
    assert s.azure_openai_deployment == "gpt-4o"
    assert s.azure_openai_api_key == "k"
    assert s.github_token == "t"
    assert s.github_allowed_owner == "example-org"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_config_phase2.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'azure_openai_endpoint'`

- [ ] **Step 3: Add the fields to `Settings` and `get_settings`**

In `src/shared/config.py`, add these fields to the `Settings` dataclass (after `agent_mode`):

```python
    # Phase 2 — Azure OpenAI (read lazily; activity-side only)
    azure_openai_endpoint: str | None
    azure_openai_deployment: str | None
    azure_openai_api_version: str
    azure_openai_api_key: str | None
    # Phase 2 — GitHub
    github_token: str | None
    github_allowed_owner: str | None
```

And add these reads inside the `Settings(...)` constructor call in `get_settings()` (after `agent_mode=...`):

```python
        azure_openai_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        azure_openai_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        azure_openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_openai_api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_allowed_owner=os.getenv("GITHUB_ALLOWED_OWNER"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_config_phase2.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Update dependency manifests**

Replace `requirements.txt` with (adds `pydantic` to the always-installed base, since response models import at agent-module load):

```
# Phase 1 runtime (mock agents) + response-model definitions.
temporalio>=1.7,<2
python-dotenv>=1.0
pydantic>=2.7
```

Create `requirements-live.txt`:

```
# Phase 2 live integrations. Install with: pip install -r requirements-live.txt
-r requirements.txt
agent-framework>=0.0.0a1
azure-identity>=1.17
PyGithub>=2.3
```

In `pyproject.toml`, replace the `live = [...]` list under `[project.optional-dependencies]` with:

```toml
live = [
    "agent-framework>=0.0.0a1",
    "azure-identity>=1.17",
    "PyGithub>=2.3",
]
```

and add `"pydantic>=2.7"` to the main `[project] dependencies` list.

- [ ] **Step 6: Run the full suite to confirm nothing regressed**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (18 passed — the prior 16 plus 2 new)

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-live.txt pyproject.toml src/shared/config.py tests/test_config_phase2.py
git commit -m "feat(phase2): add Azure OpenAI + GitHub config settings and live deps"
```

---

### Task 2: MAF live seam (`shared/maf.py`)

**Files:**
- Modify: `src/shared/maf.py`
- Test: `tests/test_maf_seam.py`

**Interfaces:**
- Consumes: `Settings` fields from Task 1; `AgentOutput`, `AgentRequest` from `shared.contracts`.
- Produces:
  - `build_chat_client(settings) -> Any` — constructs `OpenAIChatClient` (key vs `DefaultAzureCredential`).
  - `async run_live_agent(*, agent_name: str, instructions: str, prompt: str, response_model: type) -> Any` — returns the parsed Pydantic instance; raises on transient/empty output.
  - `ToOutput = Callable[[AgentRequest, Any], Awaitable[AgentOutput]]`
  - `async run_agent(*, agent_name, stage, request, mock, instructions=None, build_prompt=None, response_model=None, to_output=None) -> AgentOutput` — live when `agent_mode=="live"` **and** both `response_model` and `to_output` are provided; otherwise mock (this is how the AKS agent stays mock).

- [ ] **Step 1: Write the failing test**

Create `tests/test_maf_seam.py`:

```python
from __future__ import annotations

import pytest

from shared.contracts import (
    ACTION_CONTINUE, STATUS_SUCCESS, AgentOutput, AgentRequest,
)
from shared import maf


def _req(stage="planning"):
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="dev", stage=stage,
    )


def _mock_output(req):
    return AgentOutput(
        agent_name="x", stage=req.stage, status=STATUS_SUCCESS,
        retryable=False, summary="mock", next_action=ACTION_CONTINUE,
    )


async def test_run_agent_uses_mock_when_mode_mock(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "mock")
    out = await maf.run_agent(
        agent_name="x", stage="planning", request=_req(), mock=_mock_output,
    )
    assert out.summary == "mock"


async def test_run_agent_falls_back_to_mock_when_live_wiring_absent(monkeypatch):
    # AKS-style: live mode but no response_model/to_output -> stays mock.
    monkeypatch.setenv("AGENT_MODE", "live")
    out = await maf.run_agent(
        agent_name="aks", stage="aks", request=_req("aks"), mock=_mock_output,
    )
    assert out.summary == "mock"


async def test_run_agent_live_path_calls_to_output(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")

    class Parsed:
        value = 42

    async def fake_live(*, agent_name, instructions, prompt, response_model):
        return Parsed()

    captured = {}

    async def to_output(req, parsed):
        captured["parsed"] = parsed
        return AgentOutput(
            agent_name="x", stage=req.stage, status=STATUS_SUCCESS,
            retryable=False, summary="live", next_action=ACTION_CONTINUE,
        )

    monkeypatch.setattr(maf, "run_live_agent", fake_live)
    out = await maf.run_agent(
        agent_name="x", stage="planning", request=_req(), mock=_mock_output,
        instructions="i", build_prompt=lambda r: "p",
        response_model=Parsed, to_output=to_output,
    )
    assert out.summary == "live"
    assert captured["parsed"].value == 42


async def test_run_live_agent_raises_on_none_value(monkeypatch):
    class FakeResult:
        value = None
        text = "garbage"

    class FakeAgent:
        async def run(self, prompt, options=None):
            return FakeResult()

    class FakeClient:
        def as_agent(self, **kwargs):
            return FakeAgent()

    monkeypatch.setattr(maf, "build_chat_client", lambda settings: FakeClient())
    with pytest.raises(Exception):
        await maf.run_live_agent(
            agent_name="x", instructions="i", prompt="p", response_model=object,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_maf_seam.py -v`
Expected: FAIL (`run_live_agent` is the old stub that raises `NotImplementedError`; `run_agent` doesn't accept `to_output`).

- [ ] **Step 3: Rewrite `shared/maf.py`**

Replace the body of `src/shared/maf.py` (keep the module docstring) from the imports down with:

```python
from __future__ import annotations

from typing import Any, Awaitable, Callable

from temporalio.exceptions import ApplicationError

from shared.config import Settings, get_settings
from shared.contracts import AgentOutput, AgentRequest

MockFactory = Callable[[AgentRequest], AgentOutput]
BuildPrompt = Callable[[AgentRequest], str]
ToOutput = Callable[[AgentRequest, Any], Awaitable[AgentOutput]]


async def run_agent(
    *,
    agent_name: str,
    stage: str,
    request: AgentRequest,
    mock: MockFactory,
    instructions: str | None = None,
    build_prompt: BuildPrompt | None = None,
    response_model: type | None = None,
    to_output: ToOutput | None = None,
) -> AgentOutput:
    """Run one agent and return its structured output.

    Goes live only when AGENT_MODE=live AND the agent supplied both a
    response_model and a to_output mapper. Agents without live wiring (e.g.
    AKS) stay on the deterministic mock even in live mode.
    """
    settings = get_settings()
    live_supported = response_model is not None and to_output is not None
    if settings.agent_mode == "live" and live_supported:
        prompt = (build_prompt or _default_prompt)(request)
        parsed = await run_live_agent(
            agent_name=agent_name,
            instructions=instructions or "",
            prompt=prompt,
            response_model=response_model,
        )
        out = await to_output(request, parsed)
        return out.validate()
    return mock(request).validate()


def _default_prompt(request: AgentRequest) -> str:
    return (
        f"Goal: {request.goal}\n"
        f"Repository: {request.repo_url}\n"
        f"Environment: {request.environment}\n"
        f"Stage: {request.stage}\n"
        f"Upstream results: {request.upstream}"
    )


def build_chat_client(settings: Settings) -> Any:
    """Construct an Azure-OpenAI-backed MAF chat client.

    Uses the API key when present, otherwise DefaultAzureCredential (Entra ID /
    AKS workload identity). Imports the live packages lazily so mock mode runs
    without them installed.
    """
    from agent_framework.openai import OpenAIChatClient  # type: ignore

    if not settings.azure_openai_endpoint or not settings.azure_openai_deployment:
        raise ApplicationError(
            "AGENT_MODE=live requires AZURE_OPENAI_ENDPOINT and "
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            type="ConfigError",
            non_retryable=True,
        )

    kwargs: dict[str, Any] = {
        "model": settings.azure_openai_deployment,
        "azure_endpoint": settings.azure_openai_endpoint,
        "api_version": settings.azure_openai_api_version,
    }
    if settings.azure_openai_api_key:
        kwargs["api_key"] = settings.azure_openai_api_key
    else:
        from azure.identity.aio import DefaultAzureCredential  # type: ignore

        kwargs["credential"] = DefaultAzureCredential()
    return OpenAIChatClient(**kwargs)


async def run_live_agent(
    *,
    agent_name: str,
    instructions: str,
    prompt: str,
    response_model: type,
) -> Any:
    """Drive a real MAF agent and return the parsed structured output.

    Raises on transient failures (so Temporal's retry policy handles them) and
    raises a non-retryable ApplicationError on permanent failures (auth/bad
    request) so Temporal fails fast instead of retrying pointlessly.
    """
    settings = get_settings()
    client = build_chat_client(settings)
    agent = client.as_agent(name=agent_name, instructions=instructions)
    try:
        result = await agent.run(prompt, options={"response_format": response_model})
    except Exception as exc:  # noqa: BLE001 - classify then re-raise
        if _is_permanent_azure_error(exc):
            raise ApplicationError(
                f"permanent Azure OpenAI error for {agent_name}: {exc}",
                type=type(exc).__name__,
                non_retryable=True,
            ) from exc
        raise  # transient -> Temporal layer-1 retry

    parsed = getattr(result, "value", None)
    if parsed is None:
        # Model returned output that didn't match the schema. Treat as transient
        # (a re-generation often succeeds); Temporal retries, then fails.
        raise RuntimeError(
            f"{agent_name} returned no schema-valid output: "
            f"{getattr(result, 'text', '')[:300]}"
        )
    return parsed


def _is_permanent_azure_error(exc: Exception) -> bool:
    """Auth / bad-request style errors should not be retried."""
    name = type(exc).__name__
    if name in {"AuthenticationError", "PermissionDeniedError", "BadRequestError",
                "NotFoundError", "UnprocessableEntityError"}:
        return True
    status = getattr(exc, "status_code", None)
    return status in {400, 401, 403, 404, 422}
```

> **API note (fast-moving SDK):** the live path is creds-gated and not exercised by CI — only by the Task 9 live smoke test. The `OpenAIChatClient(model=, azure_endpoint=, api_version=, api_key=|credential=)` form is the verified Jan-2026 API. If the installed `agent-framework` version rejects `api_version` on `OpenAIChatClient`, switch `build_chat_client` to the explicit Azure client: `from agent_framework.azure import AzureOpenAIChatClient` with `AzureOpenAIChatClient(endpoint=<endpoint>, deployment_name=<deployment>, api_version=<ver>, api_key=<key>)` (or `credential=<DefaultAzureCredential>`). The rest of `run_live_agent` is unchanged either way. Confirm the exact constructor against the installed version with `python -c "from agent_framework.openai import OpenAIChatClient; help(OpenAIChatClient.__init__)"` before running the live smoke test.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_maf_seam.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full suite (the changed `run_agent` signature must not break existing activities — they still pass only `mock`)**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (22 passed)

> Note: the existing activities currently call `run_agent(agent_name=..., stage=..., instructions=..., request=..., mock=...)`. The new signature dropped `instructions` from being required but still accepts it as a keyword, so those calls keep working. Verify by running the integration test, which exercises every activity in mock mode.

- [ ] **Step 6: Commit**

```bash
git add src/shared/maf.py tests/test_maf_seam.py
git commit -m "feat(phase2): implement live MAF seam (Azure OpenAI + structured output)"
```

---

### Task 3: GitHub write client (`shared/github.py`)

**Files:**
- Create: `src/shared/github.py`
- Test: `tests/test_github_client.py`

**Interfaces:**
- Produces:
  - `class GitHubWriteNotAllowed(Exception)` — permanent (guard).
  - `class PermanentGitHubError(Exception)` — permanent (auth/404/422).
  - `parse_owner_repo(repo_url: str) -> tuple[str, str]`
  - `assert_write_allowed(owner: str, allowed_owner: str | None) -> None`
  - `create_pr_with_plan(*, repo_url: str, request_id: str, token: str | None, allowed_owner: str | None, pr_title: str, pr_body: str, plan_markdown: str, commit_message: str, client_factory=None) -> dict` returning keys `{"branch", "pr_number", "pr_url", "plan_file", "created_or_existed"}`. Synchronous (PyGithub is blocking); callers invoke it via `asyncio.to_thread`. `client_factory(token)` is injectable for tests; defaults to a real PyGithub client.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_github_client.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared import github as gh


def test_parse_owner_repo():
    assert gh.parse_owner_repo("https://github.com/example-org/svc") == ("example-org", "svc")
    assert gh.parse_owner_repo("https://github.com/example-org/svc.git") == ("example-org", "svc")
    assert gh.parse_owner_repo("git@github.com:example-org/svc.git") == ("example-org", "svc")


def test_assert_write_allowed_fail_closed_when_unset():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.assert_write_allowed("example-org", None)


def test_assert_write_allowed_rejects_mismatch():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.assert_write_allowed("someone-else", "example-org")


def test_assert_write_allowed_accepts_match():
    gh.assert_write_allowed("example-org", "example-org")  # no raise


def _fake_repo(*, branch_exists, file_exists, pr_exists):
    repo = MagicMock()
    repo.default_branch = "main"
    base_ref = SimpleNamespace(object=SimpleNamespace(sha="basesha"))

    def get_git_ref(ref):
        if ref == "heads/main":
            return base_ref
        if ref == f"heads/feat/req-1" and branch_exists:
            return SimpleNamespace(object=SimpleNamespace(sha="branchsha"))
        from github import GithubException
        raise GithubException(404, {"message": "Not Found"}, {})

    repo.get_git_ref.side_effect = get_git_ref

    if file_exists:
        repo.get_contents.return_value = SimpleNamespace(sha="filesha")
    else:
        from github import GithubException
        repo.get_contents.side_effect = GithubException(404, {"message": "nf"}, {})

    if pr_exists:
        existing = SimpleNamespace(number=7, html_url="https://github.com/example-org/svc/pull/7")
        repo.get_pulls.return_value = [existing]
    else:
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = SimpleNamespace(
            number=8, html_url="https://github.com/example-org/svc/pull/8"
        )
    return repo


def _factory_for(repo):
    gh_client = MagicMock()
    gh_client.get_repo.return_value = repo
    return lambda token: gh_client


def test_create_pr_fresh(monkeypatch):
    repo = _fake_repo(branch_exists=False, file_exists=False, pr_exists=False)
    out = gh.create_pr_with_plan(
        repo_url="https://github.com/example-org/svc", request_id="req-1",
        token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
        plan_markdown="# plan", commit_message="add plan",
        client_factory=_factory_for(repo),
    )
    assert out["branch"] == "feat/req-1"
    assert out["pr_number"] == 8
    assert out["created_or_existed"] == "created"
    repo.create_git_ref.assert_called_once()  # branch created from base


def test_create_pr_idempotent_when_everything_exists(monkeypatch):
    repo = _fake_repo(branch_exists=True, file_exists=True, pr_exists=True)
    out = gh.create_pr_with_plan(
        repo_url="https://github.com/example-org/svc", request_id="req-1",
        token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
        plan_markdown="# plan", commit_message="add plan",
        client_factory=_factory_for(repo),
    )
    assert out["pr_number"] == 7
    assert out["created_or_existed"] == "existed"
    repo.create_git_ref.assert_not_called()      # branch reused
    repo.update_file.assert_called_once()         # file updated, not created
    repo.create_pull.assert_not_called()          # PR reused


def test_create_pr_guard_blocks_disallowed_owner():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.create_pr_with_plan(
            repo_url="https://github.com/someone-else/svc", request_id="req-1",
            token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
            plan_markdown="x", commit_message="m", client_factory=lambda token: MagicMock(),
        )


def test_permanent_error_on_404_repo():
    from github import GithubException
    gh_client = MagicMock()
    gh_client.get_repo.side_effect = GithubException(404, {"message": "nf"}, {})
    with pytest.raises(gh.PermanentGitHubError):
        gh.create_pr_with_plan(
            repo_url="https://github.com/example-org/svc", request_id="req-1",
            token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
            plan_markdown="x", commit_message="m", client_factory=lambda token: gh_client,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_github_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.github'` (and `PyGithub` must be installed for the test: `pip install PyGithub`).

- [ ] **Step 3: Implement `shared/github.py`**

Create `src/shared/github.py`:

```python
"""Idempotent, guarded GitHub write client (activity-side only).

No LLM here. PyGithub is synchronous, so callers invoke create_pr_with_plan via
asyncio.to_thread. PyGithub is imported lazily so mock mode runs without it.
"""

from __future__ import annotations

import re
from typing import Any, Callable

PLAN_PATH_TEMPLATE = "docs/agent-plan-{request_id}.md"
BRANCH_TEMPLATE = "feat/{request_id}"

# Statuses we treat as permanent (no point retrying).
_PERMANENT_STATUS = {401, 403, 404, 422}


class GitHubWriteNotAllowed(Exception):
    """The target repo is not permitted by the owner/allowlist guard."""


class PermanentGitHubError(Exception):
    """A non-retryable GitHub failure (auth, missing repo, validation)."""


def parse_owner_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from an https or ssh GitHub URL."""
    cleaned = repo_url.strip()
    cleaned = re.sub(r"\.git$", "", cleaned)
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+)$", cleaned)
    if not m:
        raise PermanentGitHubError(f"cannot parse owner/repo from {repo_url!r}")
    return m.group(1), m.group(2)


def assert_write_allowed(owner: str, allowed_owner: str | None) -> None:
    """Fail-closed guard: refuse unless the owner matches the allowlist."""
    if not allowed_owner:
        raise GitHubWriteNotAllowed(
            "GITHUB_ALLOWED_OWNER is not set; refusing to write (fail-closed)"
        )
    allowed = {o.strip() for o in allowed_owner.split(",") if o.strip()}
    if owner not in allowed:
        raise GitHubWriteNotAllowed(
            f"owner {owner!r} not in allowed owners {sorted(allowed)}"
        )


def _default_client_factory(token: str | None) -> Any:
    from github import Auth, Github  # type: ignore

    if not token:
        raise PermanentGitHubError("GITHUB_TOKEN is required for live GitHub writes")
    return Github(auth=Auth.Token(token))


def create_pr_with_plan(
    *,
    repo_url: str,
    request_id: str,
    token: str | None,
    allowed_owner: str | None,
    pr_title: str,
    pr_body: str,
    plan_markdown: str,
    commit_message: str,
    client_factory: Callable[[str | None], Any] | None = None,
) -> dict:
    """Ensure branch -> upsert plan file -> ensure PR. Idempotent.

    Raises GitHubWriteNotAllowed / PermanentGitHubError for permanent failures;
    lets transient GithubException (5xx / rate limit) propagate for retry.
    """
    from github import GithubException  # type: ignore

    owner, repo_name = parse_owner_repo(repo_url)
    assert_write_allowed(owner, allowed_owner)

    factory = client_factory or _default_client_factory
    gh_client = factory(token)

    branch = BRANCH_TEMPLATE.format(request_id=request_id)
    path = PLAN_PATH_TEMPLATE.format(request_id=request_id)

    try:
        repo = gh_client.get_repo(f"{owner}/{repo_name}")
        base = repo.default_branch

        # 1. ensure branch
        try:
            repo.get_git_ref(f"heads/{branch}")
        except GithubException as exc:
            if exc.status == 404:
                base_sha = repo.get_git_ref(f"heads/{base}").object.sha
                repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
            else:
                raise

        # 2. upsert plan file on the branch
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.update_file(path, commit_message, plan_markdown, existing.sha, branch=branch)
        except GithubException as exc:
            if exc.status == 404:
                repo.create_file(path, commit_message, plan_markdown, branch=branch)
            else:
                raise

        # 3. ensure PR
        open_pulls = list(repo.get_pulls(state="open", head=f"{owner}:{branch}"))
        if open_pulls:
            pr = open_pulls[0]
            created_or_existed = "existed"
        else:
            pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base=base)
            created_or_existed = "created"

        return {
            "branch": branch,
            "pr_number": pr.number,
            "pr_url": pr.html_url,
            "plan_file": path,
            "created_or_existed": created_or_existed,
        }

    except GithubException as exc:
        if getattr(exc, "status", None) in _PERMANENT_STATUS:
            raise PermanentGitHubError(f"GitHub {exc.status}: {exc.data}") from exc
        raise  # transient (5xx, secondary rate limit) -> Temporal retry
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pip install PyGithub && PYTHONPATH=src python -m pytest tests/test_github_client.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/shared/github.py tests/test_github_client.py
git commit -m "feat(phase2): idempotent guarded GitHub write client"
```

---

### Task 4: Planner agent goes live

**Files:**
- Modify: `src/planner_agent_worker/agent.py`
- Modify: `src/planner_agent_worker/activities.py:43-49` (the `run_agent(...)` call)
- Test: `tests/test_planner_live.py`

**Interfaces:**
- Consumes: `run_agent` (Task 2).
- Produces in `planner_agent_worker.agent`: `class PlannerResult(BaseModel)` with `summary: str`, `steps: list[str]`, `risk_level: Literal["low","medium","high"]`, `rationale: str`; `RESPONSE_MODEL = PlannerResult`; `build_prompt(request: AgentRequest) -> str`; `async to_output(request: AgentRequest, parsed: PlannerResult) -> AgentOutput`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_planner_live.py`:

```python
from __future__ import annotations

from shared.contracts import STATUS_SUCCESS, ACTION_CONTINUE, AgentRequest
from planner_agent_worker import agent


def _req():
    return AgentRequest(
        request_id="r1", goal="add healthz", repo_url="https://github.com/o/r",
        environment="dev", stage="planning",
    )


async def test_planner_to_output_maps_to_contract():
    parsed = agent.PlannerResult(
        summary="plan ready", steps=["a", "b"], risk_level="low", rationale="because",
    )
    out = (await agent.to_output(_req(), parsed)).validate()
    assert out.agent_name == "planner"
    assert out.status == STATUS_SUCCESS
    assert out.next_action == ACTION_CONTINUE
    assert out.summary == "plan ready"
    assert out.details["steps"] == ["a", "b"]
    assert out.details["risk_level"] == "low"


def test_planner_build_prompt_includes_goal_and_repo():
    p = agent.build_prompt(_req())
    assert "add healthz" in p
    assert "https://github.com/o/r" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_planner_live.py -v`
Expected: FAIL with `AttributeError: module 'planner_agent_worker.agent' has no attribute 'PlannerResult'`

- [ ] **Step 3: Add live wiring to `planner_agent_worker/agent.py`**

Add to the imports at the top of `src/planner_agent_worker/agent.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field
```

Then append to the module (after `mock`):

```python
class PlannerResult(BaseModel):
    """Structured planning output enforced via Azure OpenAI response_format."""

    summary: str = Field(description="One-line summary of the plan")
    steps: list[str] = Field(description="Ordered, concrete deployment steps")
    risk_level: Literal["low", "medium", "high"]
    rationale: str = Field(description="Why this plan and risk level")


RESPONSE_MODEL = PlannerResult


def build_prompt(request: AgentRequest) -> str:
    return (
        f"Engineering goal: {request.goal}\n"
        f"Target repository: {request.repo_url}\n"
        f"Environment: {request.environment}\n\n"
        "Produce a concrete, ordered deployment plan and assess its risk."
    )


async def to_output(request: AgentRequest, parsed: PlannerResult) -> AgentOutput:
    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_PLANNING,
        status=STATUS_SUCCESS,
        retryable=False,
        summary=parsed.summary,
        next_action=ACTION_CONTINUE,
        details={
            "goal": request.goal,
            "repo_url": request.repo_url,
            "environment": request.environment,
            "steps": parsed.steps,
            "risk_level": parsed.risk_level,
            "rationale": parsed.rationale,
        },
    )
```

- [ ] **Step 4: Wire the activity to pass the live params**

In `src/planner_agent_worker/activities.py`, replace the `run_agent(...)` call with:

```python
    output = await run_agent(
        agent_name=agent.AGENT_NAME,
        stage=request.stage,
        instructions=agent.INSTRUCTIONS,
        request=request,
        mock=agent.mock,
        build_prompt=agent.build_prompt,
        response_model=agent.RESPONSE_MODEL,
        to_output=agent.to_output,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_planner_live.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the full suite (mock-mode integration still green)**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (32 passed)

- [ ] **Step 7: Commit**

```bash
git add src/planner_agent_worker/agent.py src/planner_agent_worker/activities.py tests/test_planner_live.py
git commit -m "feat(phase2): planner agent live wiring (Azure OpenAI)"
```

---

### Task 5: GitHub agent goes live

**Files:**
- Modify: `src/github_agent_worker/agent.py`
- Modify: `src/github_agent_worker/activities.py` (the `run_agent(...)` call)
- Test: `tests/test_github_live.py`

**Interfaces:**
- Consumes: `run_agent` (Task 2); `create_pr_with_plan`, `GitHubWriteNotAllowed`, `PermanentGitHubError` (Task 3); settings `github_token`, `github_allowed_owner` (Task 1).
- Produces in `github_agent_worker.agent`: `class GitHubChange(BaseModel)` with `pr_title: str`, `pr_body_markdown: str`, `plan_file_markdown: str`, `commit_message: str`; `RESPONSE_MODEL = GitHubChange`; `build_prompt`; `async to_output` that runs `create_pr_with_plan` via `asyncio.to_thread` and maps guard/permanent errors to `status=failed, retryable=False, next_action=fail`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_github_live.py`:

```python
from __future__ import annotations

import pytest

from shared.contracts import (
    ACTION_CONTINUE, ACTION_FAIL, STATUS_FAILED, STATUS_SUCCESS,
    STAGE_PLANNING, AgentOutput, AgentRequest,
)
from shared import github as gh
from github_agent_worker import agent


def _req():
    return AgentRequest(
        request_id="req-1", goal="add healthz",
        repo_url="https://github.com/example-org/svc", environment="dev",
        stage="github",
        upstream={STAGE_PLANNING: AgentOutput(
            agent_name="planner", stage="planning", status=STATUS_SUCCESS,
            retryable=False, summary="s", next_action=ACTION_CONTINUE,
            details={"steps": ["x"]},
        )},
    )


def _change():
    return agent.GitHubChange(
        pr_title="Add healthz", pr_body_markdown="body",
        plan_file_markdown="# plan", commit_message="add plan",
    )


async def test_github_to_output_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")

    def fake_create(**kwargs):
        assert kwargs["request_id"] == "req-1"
        return {"branch": "feat/req-1", "pr_number": 5,
                "pr_url": "https://github.com/example-org/svc/pull/5",
                "plan_file": "docs/agent-plan-req-1.md", "created_or_existed": "created"}

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_SUCCESS
    assert out.next_action == ACTION_CONTINUE
    assert out.details["pr_number"] == 5
    assert "#5" in out.summary


async def test_github_to_output_guard_violation_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_ALLOWED_OWNER", raising=False)

    def fake_create(**kwargs):
        raise gh.GitHubWriteNotAllowed("fail-closed")

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_FAILED
    assert out.retryable is False
    assert out.next_action == ACTION_FAIL


async def test_github_to_output_permanent_error_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")

    def fake_create(**kwargs):
        raise gh.PermanentGitHubError("404")

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_FAILED
    assert out.next_action == ACTION_FAIL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_github_live.py -v`
Expected: FAIL with `AttributeError: module 'github_agent_worker.agent' has no attribute 'GitHubChange'`

- [ ] **Step 3: Add live wiring to `github_agent_worker/agent.py`**

Add to the imports at the top of `src/github_agent_worker/agent.py`:

```python
import asyncio

from pydantic import BaseModel, Field

from shared import github as gh
from shared.config import get_settings
from shared.contracts import ACTION_FAIL, STATUS_FAILED
```

Then append to the module (after `mock`):

```python
class GitHubChange(BaseModel):
    """LLM-authored PR content (the git mechanics are owned by the activity)."""

    pr_title: str = Field(description="Concise PR title")
    pr_body_markdown: str = Field(description="PR description in markdown")
    plan_file_markdown: str = Field(description="Full content for the committed plan file")
    commit_message: str = Field(description="Commit message for the plan file")


RESPONSE_MODEL = GitHubChange


def build_prompt(request: AgentRequest) -> str:
    planner = request.upstream.get(STAGE_PLANNING)
    steps = planner.details.get("steps", []) if planner else []
    steps_text = "\n".join(f"- {s}" for s in steps) or "- (no upstream plan)"
    return (
        f"Engineering goal: {request.goal}\n"
        f"Target repository: {request.repo_url}\n"
        f"Environment: {request.environment}\n"
        f"Planner steps:\n{steps_text}\n\n"
        "Write the pull request title, a markdown PR body, the markdown content "
        "for a committed plan file documenting this change, and a commit message."
    )


async def to_output(request: AgentRequest, parsed: GitHubChange) -> AgentOutput:
    settings = get_settings()
    try:
        details = await asyncio.to_thread(
            gh.create_pr_with_plan,
            repo_url=request.repo_url,
            request_id=request.request_id,
            token=settings.github_token,
            allowed_owner=settings.github_allowed_owner,
            pr_title=parsed.pr_title,
            pr_body=parsed.pr_body_markdown,
            plan_markdown=parsed.plan_file_markdown,
            commit_message=parsed.commit_message,
        )
    except (gh.GitHubWriteNotAllowed, gh.PermanentGitHubError) as exc:
        return AgentOutput(
            agent_name=AGENT_NAME,
            stage=STAGE_GITHUB,
            status=STATUS_FAILED,
            retryable=False,
            summary=f"github write failed: {exc}",
            next_action=ACTION_FAIL,
            details={"error": str(exc), "error_type": type(exc).__name__},
        )

    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_GITHUB,
        status=STATUS_SUCCESS,
        retryable=False,
        summary=f"opened pull request #{details['pr_number']} on branch {details['branch']}",
        next_action=ACTION_CONTINUE,
        details=details,
    )
```

- [ ] **Step 4: Wire the activity to pass the live params**

In `src/github_agent_worker/activities.py`, replace the `run_agent(...)` call with:

```python
    output = await run_agent(
        agent_name=agent.AGENT_NAME,
        stage=request.stage,
        instructions=agent.INSTRUCTIONS,
        request=request,
        mock=agent.mock,
        build_prompt=agent.build_prompt,
        response_model=agent.RESPONSE_MODEL,
        to_output=agent.to_output,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_github_live.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (35 passed)

- [ ] **Step 7: Commit**

```bash
git add src/github_agent_worker/agent.py src/github_agent_worker/activities.py tests/test_github_live.py
git commit -m "feat(phase2): github agent live wiring (Azure OpenAI + real PR writes)"
```

---

### Task 6: Approval agent goes live

**Files:**
- Modify: `src/approval_agent_worker/agent.py`
- Modify: `src/approval_agent_worker/activities.py` (the `run_agent(...)` call)
- Test: `tests/test_approval_live.py`

**Interfaces:**
- Consumes: `run_agent` (Task 2); `get_settings` for `approval_auto`/`approval_timeout_seconds` (already used by the existing mock).
- Produces in `approval_agent_worker.agent`: `class ApprovalAssessment(BaseModel)` with `recommendation: Literal["approve","reject","needs_human"]`, `risk_level: Literal["low","medium","high"]`, `reasons: list[str]`; `RESPONSE_MODEL = ApprovalAssessment`; `build_prompt`; `async to_output` implementing the escalation matrix. The workflow's durable human-signal gate is unchanged — `to_output` must keep emitting the `auto_approve`/`timeout_seconds` fields in `details` exactly like the mock does.

- [ ] **Step 1: Write the failing test**

Create `tests/test_approval_live.py`:

```python
from __future__ import annotations

import pytest

from shared.contracts import (
    STATUS_NEEDS_APPROVAL, STATUS_SUCCESS, STAGE_AKS, AgentOutput, AgentRequest,
)
from approval_agent_worker import agent


def _req(approval_required, aks_pending=False):
    upstream = {}
    if aks_pending:
        upstream[STAGE_AKS] = AgentOutput(
            agent_name="aks", stage="aks", status=STATUS_NEEDS_APPROVAL,
            retryable=False, summary="staged", next_action="ask_human",
            details={"promotion_pending": True},
        )
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="prod" if approval_required else "dev", stage="approval",
        approval_required=approval_required, upstream=upstream,
    )


def _assess(recommendation, risk):
    return agent.ApprovalAssessment(
        recommendation=recommendation, risk_level=risk, reasons=["r"],
    )


@pytest.mark.parametrize("required,aks,reco,risk,expected", [
    (True,  False, "approve",     "low",  STATUS_NEEDS_APPROVAL),  # required by config
    (False, True,  "approve",     "low",  STATUS_NEEDS_APPROVAL),  # AKS staged a promotion
    (False, False, "needs_human", "low",  STATUS_NEEDS_APPROVAL),  # LLM asks for a human
    (False, False, "approve",     "high", STATUS_NEEDS_APPROVAL),  # LLM escalates on risk
    (False, False, "approve",     "low",  STATUS_SUCCESS),         # low-risk, not required
])
async def test_approval_escalation_matrix(monkeypatch, required, aks, reco, risk, expected):
    monkeypatch.setenv("TEMPORAL_APPROVAL_AUTO", "true")
    out = (await agent.to_output(_req(required, aks), _assess(reco, risk))).validate()
    assert out.status == expected
    # The workflow reads these regardless of branch:
    assert "auto_approve" in out.details
    assert "timeout_seconds" in out.details
    assert out.details["recommendation"] == reco
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_approval_live.py -v`
Expected: FAIL with `AttributeError: module 'approval_agent_worker.agent' has no attribute 'ApprovalAssessment'`

- [ ] **Step 3: Add live wiring to `approval_agent_worker/agent.py`**

Add to the imports at the top of `src/approval_agent_worker/agent.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field
```

Then append to the module (after `mock`):

```python
class ApprovalAssessment(BaseModel):
    """LLM risk classification. The workflow still owns the durable human gate."""

    recommendation: Literal["approve", "reject", "needs_human"]
    risk_level: Literal["low", "medium", "high"]
    reasons: list[str] = Field(description="Short bullet reasons for the recommendation")


RESPONSE_MODEL = ApprovalAssessment


def build_prompt(request: AgentRequest) -> str:
    aks = request.upstream.get(STAGE_AKS)
    aks_pending = bool(aks and aks.details.get("promotion_pending"))
    return (
        f"Engineering goal: {request.goal}\n"
        f"Environment: {request.environment}\n"
        f"Approval required by policy: {request.approval_required}\n"
        f"AKS staged a pending promotion: {aks_pending}\n\n"
        "Assess deployment risk and recommend approve, reject, or needs_human."
    )


async def to_output(request: AgentRequest, parsed: ApprovalAssessment) -> AgentOutput:
    settings = get_settings()
    aks = request.upstream.get(STAGE_AKS)
    aks_pending = bool(aks and aks.details.get("promotion_pending"))

    needs_approval = (
        request.approval_required
        or aks_pending
        or parsed.recommendation == "needs_human"
        or parsed.risk_level == "high"
    )

    details = {
        "auto_approve": settings.approval_auto,
        "timeout_seconds": settings.approval_timeout_seconds,
        "environment": request.environment,
        "recommendation": parsed.recommendation,
        "risk_level": parsed.risk_level,
        "reasons": parsed.reasons,
    }

    if not needs_approval:
        return AgentOutput(
            agent_name=AGENT_NAME,
            stage=STAGE_APPROVAL,
            status=STATUS_SUCCESS,
            retryable=False,
            summary="low risk; auto-promoted without human gate",
            next_action=ACTION_CONTINUE,
            details=details,
        )

    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_APPROVAL,
        status=STATUS_NEEDS_APPROVAL,
        retryable=False,
        summary=f"human approval required to promote to {request.environment}",
        next_action=ACTION_ASK_HUMAN,
        details=details,
    )
```

- [ ] **Step 4: Wire the activity to pass the live params**

In `src/approval_agent_worker/activities.py`, replace the `run_agent(...)` call with:

```python
    output = await run_agent(
        agent_name=agent.AGENT_NAME,
        stage=request.stage,
        instructions=agent.INSTRUCTIONS,
        request=request,
        mock=agent.mock,
        build_prompt=agent.build_prompt,
        response_model=agent.RESPONSE_MODEL,
        to_output=agent.to_output,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_approval_live.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (40 passed)

- [ ] **Step 7: Commit**

```bash
git add src/approval_agent_worker/agent.py src/approval_agent_worker/activities.py tests/test_approval_live.py
git commit -m "feat(phase2): approval agent live wiring with risk escalation"
```

---

### Task 7: AKS stays mock — lock it with a regression test

**Files:**
- Test: `tests/test_aks_stays_mock.py`
- (No source change expected — this task proves the fallback in Task 2 keeps AKS on the mock even when `AGENT_MODE=live`.)

**Interfaces:**
- Consumes: `aks_agent_worker.agent.mock`, `aks_agent_worker.activities.run_aks_agent`.

- [ ] **Step 1: Write the test**

Create `tests/test_aks_stays_mock.py`:

```python
from __future__ import annotations

from shared.contracts import STATUS_NEEDS_APPROVAL, STATUS_SUCCESS, AgentRequest
from aks_agent_worker import agent
from aks_agent_worker.activities import run_aks_agent


def _req(approval_required=True):
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="dev", stage="aks", approval_required=approval_required,
    )


def test_aks_module_has_no_live_wiring():
    # AKS must NOT expose a response model / to_output -> run_agent stays mock.
    assert not hasattr(agent, "RESPONSE_MODEL")
    assert not hasattr(agent, "to_output")


async def test_aks_activity_stays_mock_even_in_live_mode(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")
    out = await run_aks_agent(_req(approval_required=True))
    # Deterministic mock behaviour (needs_approval when approval_required).
    assert out.agent_name == "aks"
    assert out.status == STATUS_NEEDS_APPROVAL
```

- [ ] **Step 2: Run the test**

Run: `PYTHONPATH=src python -m pytest tests/test_aks_stays_mock.py -v`
Expected: PASS (2 passed). If `test_aks_activity_stays_mock_even_in_live_mode` fails because the activity tried to go live, the fallback in Task 2 (`live_supported`) is wrong — fix `run_agent` so missing `response_model`/`to_output` always falls back to mock.

- [ ] **Step 3: Commit**

```bash
git add tests/test_aks_stays_mock.py
git commit -m "test(phase2): lock AKS agent to mock even under AGENT_MODE=live"
```

---

### Task 8: Docker + k8s for live mode

**Files:**
- Modify: `Dockerfile`
- Create: `k8s/secrets/live-agents-secret.example.yaml`
- Modify: `k8s/deployments/planner-agent-worker.yaml`, `github-agent-worker.yaml`, `approval-agent-worker.yaml` (add `envFrom` secret + `AGENT_MODE` note)

**Interfaces:**
- Consumes: env var names from Task 1.

- [ ] **Step 1: Add a live build stage to the Dockerfile**

In `Dockerfile`, replace the dependency-install line
`RUN pip install --no-cache-dir -r requirements.txt`
with a build-arg-controlled install:

```dockerfile
ARG INSTALL_LIVE=false
COPY requirements.txt requirements-live.txt ./
RUN if [ "$INSTALL_LIVE" = "true" ]; then \
        pip install --no-cache-dir -r requirements-live.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi
```

(Also update the earlier `COPY requirements.txt ./` line if present so both files are available — the snippet above already copies both.)

Build a live image with: `docker build --build-arg INSTALL_LIVE=true -t temporal-maf-agents-poc:live .`

- [ ] **Step 2: Create the example Secret**

Create `k8s/secrets/live-agents-secret.example.yaml`:

```yaml
# Copy to live-agents-secret.yaml, fill in real values, and `kubectl apply -f` it.
# Keyless Azure (AKS workload identity) is preferred: omit AZURE_OPENAI_API_KEY and
# annotate the worker ServiceAccount for workload identity instead.
apiVersion: v1
kind: Secret
metadata:
  name: live-agents-secret
  namespace: agent-platform
type: Opaque
stringData:
  AZURE_OPENAI_ENDPOINT: "https://<your-resource>.openai.azure.com"
  AZURE_OPENAI_CHAT_DEPLOYMENT: "gpt-4o"
  AZURE_OPENAI_API_VERSION: "2024-10-21"
  # AZURE_OPENAI_API_KEY: "<key>"   # omit to use workload identity
  GITHUB_TOKEN: "<fine-grained-pat>"
  GITHUB_ALLOWED_OWNER: "example-org"
```

- [ ] **Step 3: Reference the Secret + flip AGENT_MODE in the three live workers**

In each of `k8s/deployments/planner-agent-worker.yaml`, `github-agent-worker.yaml`, and `approval-agent-worker.yaml`, add an `envFrom` entry alongside the existing `configMapRef` and add `AGENT_MODE: live` via the env list. The container `env`/`envFrom` block becomes:

```yaml
          envFrom:
            - configMapRef:
                name: temporal-config
            - secretRef:
                name: live-agents-secret
          env:
            - name: WORKER_MODULE
              value: planner_agent_worker.worker   # github_/approval_ in the others
            - name: AGENT_MODE
              value: live
```

(Leave `orchestrator-worker.yaml` and `aks-agent-worker.yaml` unchanged.)

- [ ] **Step 4: Validate the manifests parse**

Run:
```bash
PYTHONPATH=src python - <<'PY'
import glob, yaml
for f in sorted(glob.glob("k8s/**/*.yaml", recursive=True)):
    list(yaml.safe_load_all(open(f)))
    print("ok", f)
PY
```
Expected: every file prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile k8s/secrets/live-agents-secret.example.yaml k8s/deployments/planner-agent-worker.yaml k8s/deployments/github-agent-worker.yaml k8s/deployments/approval-agent-worker.yaml
git commit -m "feat(phase2): live Docker build arg + k8s secret wiring"
```

---

### Task 9: Docs + opt-in live smoke test

**Files:**
- Modify: `README.md` (expand the "Phase 2 — going live" section; flip the acceptance-criteria checkbox)
- Modify: `.env.example` (fill real Azure/GitHub vars)
- Create: `tests/test_live_smoke.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Create the opt-in live smoke test**

Create `tests/test_live_smoke.py`:

```python
"""Opt-in live smoke test. Skipped unless real creds + RUN_LIVE_SMOKE=1.

Run with:
    RUN_LIVE_SMOKE=1 AGENT_MODE=live \
    AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_CHAT_DEPLOYMENT=... \
    GITHUB_TOKEN=... GITHUB_ALLOWED_OWNER=<owner> \
    PYTHONPATH=src pytest tests/test_live_smoke.py -v
"""

from __future__ import annotations

import os

import pytest

from shared.contracts import STATUS_SUCCESS, AgentRequest
from planner_agent_worker import agent as planner

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_SMOKE") != "1"
    or not os.getenv("AZURE_OPENAI_ENDPOINT")
    or not os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
    reason="live smoke disabled (set RUN_LIVE_SMOKE=1 + Azure env to enable)",
)


async def test_planner_round_trip_against_azure_openai(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")
    from shared.maf import run_agent

    req = AgentRequest(
        request_id="smoke-1", goal="add a /healthz endpoint",
        repo_url=f"https://github.com/{os.getenv('GITHUB_ALLOWED_OWNER','example-org')}/svc",
        environment="dev", stage="planning",
    )
    out = await run_agent(
        agent_name=planner.AGENT_NAME, stage="planning", instructions=planner.INSTRUCTIONS,
        request=req, mock=planner.mock, build_prompt=planner.build_prompt,
        response_model=planner.RESPONSE_MODEL, to_output=planner.to_output,
    )
    assert out.status == STATUS_SUCCESS
    assert out.details["steps"]
```

- [ ] **Step 2: Run it to confirm it skips cleanly without creds**

Run: `PYTHONPATH=src python -m pytest tests/test_live_smoke.py -v`
Expected: `1 skipped` (reason: live smoke disabled).

- [ ] **Step 3: Update `.env.example`**

Replace the Phase-2 commented block at the bottom of `.env.example` with real-looking, uncommented placeholders:

```bash
# ---------------------------------------------------------------------------
# Phase 2 — live integrations (consumed inside activities only)
# Set AGENT_MODE=live above to enable.
# ---------------------------------------------------------------------------
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-10-21
# Leave AZURE_OPENAI_API_KEY unset to use DefaultAzureCredential (az login / workload identity)
# AZURE_OPENAI_API_KEY=
GITHUB_TOKEN=
# Required in live mode (fail-closed): the owner the GitHub agent may write to
GITHUB_ALLOWED_OWNER=
```

- [ ] **Step 4: Expand the README "Phase 2" section**

In `README.md`, replace the existing "Phase 2 — going live" section body with:

```markdown
## Phase 2 — going live (Azure OpenAI + GitHub)

The planner, github, and approval agents run real Azure OpenAI reasoning (via
Microsoft Agent Framework) and the github agent makes real GitHub writes. The
AKS agent stays mock.

1. Install live deps: `pip install -r requirements-live.txt` (or `pip install -e ".[live]"`).
2. Set env (see `.env.example`):
   - `AGENT_MODE=live`
   - `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`
   - Azure auth: set `AZURE_OPENAI_API_KEY`, **or** leave it unset and use
     `DefaultAzureCredential` (`az login` locally / workload identity on AKS).
   - `GITHUB_TOKEN` (fine-grained PAT) and `GITHUB_ALLOWED_OWNER` (the github
     agent refuses to write unless the target repo's owner matches — fail-closed).
3. Run workers + `python -m starter` as in Phase 1.

What the github agent does: creates branch `feat/<request_id>`, commits
`docs/agent-plan-<request_id>.md`, and opens a PR. All writes are idempotent, so
Temporal activity retries converge instead of duplicating.

Error handling: transient Azure/GitHub errors (5xx, rate limit, timeout, or a
schema-invalid model response) are raised and retried by Temporal's activity
retry policy; permanent errors (auth, missing repo, validation, guard violation)
fail the workflow without pointless retries.

Live on AKS: build the live image with `--build-arg INSTALL_LIVE=true`, apply
`k8s/secrets/live-agents-secret.yaml`, and use the updated planner/github/approval
deployments (which set `AGENT_MODE=live` and mount the secret).

Tests: `pytest` runs everything with mocked clients (no creds). The opt-in live
smoke test runs only with `RUN_LIVE_SMOKE=1` + real Azure env.
```

Also flip the acceptance-criteria checkbox from
`- [ ] Phase 2 real integrations (TODO stubs in place)`
to
`- [x] Phase 2 real integrations — Azure OpenAI + GitHub live (AKS still mock)`.

- [ ] **Step 5: Run the entire suite one last time**

Run: `PYTHONPATH=src python -m pytest -q --timeout=120 --timeout-method=thread`
Expected: PASS (42 passed, 1 skipped).

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example tests/test_live_smoke.py
git commit -m "docs(phase2): live-mode README, .env.example, opt-in live smoke test"
```

---

## Notes for the implementer

- **Run from `temporal-maf-agents-poc/`** with a venv that has `temporalio`, `pydantic`, `pytest`, `pytest-asyncio`, `pytest-timeout`, and `PyGithub`. The full suite needs `PyGithub` (Task 3 imports `github.GithubException`) and `pydantic` (response models), but **not** `agent-framework` or `azure-identity` — the MAF tests monkeypatch `build_chat_client`/`run_live_agent`, so no real Azure SDK is imported in CI. Install for testing with: `pip install -e ".[dev]" PyGithub`. `asyncio_mode = "auto"` is already set in `pyproject.toml`, so `async def test_...` functions run without decorators.
- **Test counts** in the "Expected" lines assume the prior task's tests are present and passing; if you run a single file the totals differ — that's fine, just confirm no failures.
- **Do not touch** any `*/workflows.py`, `shared/contracts.py`, `shared/child.py`, the orchestrator, KEDA manifests, or the AKS agent's behavior.
- **Determinism guard:** if a test or import ever pulls `agent_framework`/`azure`/`github` into a workflow module, you've crossed the boundary — move it back into the activity/seam.
