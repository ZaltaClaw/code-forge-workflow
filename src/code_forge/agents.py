"""Agent definitions for the Code Forge workflow.

Each agent is a focused specialist. Agents can be backed by **two different
providers** in the same workflow — this is the core value proposition of
Microsoft Agent Framework's `BaseAgent` abstraction:

* OpenAI (default) via `OpenAIChatClient.as_agent(...)`
* Anthropic Claude (via the Claude Code CLI / Claude Agent SDK) via
  `ClaudeAgent(...)`

Provider selection is per-role and driven by env vars (see `.env.example`).
By default we route the **SecurityReviewer** to Claude — Claude tends to
produce stronger code-review and security-audit reasoning — and the rest
to OpenAI. Set `CODE_FORGE_PROVIDERS=all-openai` (or `all-claude`) to override.

Reference:
  https://devblogs.microsoft.com/agent-framework/build-ai-agents-with-claude-agent-sdk-and-microsoft-agent-framework/
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_framework import BaseAgent

# ---------------------------------------------------------------------------
# Instructions (unchanged — same role definitions regardless of provider)
# ---------------------------------------------------------------------------

SPEC_ANALYST = """\
You are SpecAnalyst, a senior product engineer. Read the user's raw feature
request and produce a precise spec.

Output STRICTLY in this format:

# FEATURE
<one sentence summary>

## REQUIREMENTS
- bullet list of behavioural requirements (3-7 items)

## INTERFACE
<function signature(s) the implementer must provide, in Python>

## EDGE CASES
- bullet list of tricky inputs / failure modes (>=3 items)
"""

IMPLEMENTER = """\
You are Implementer, a careful Python engineer. You receive a spec (and
optionally reviewer feedback prefixed with "REVIEW FEEDBACK:"). Produce a
single Python module that fulfils the spec.

Rules:
- Pure stdlib unless the spec explicitly allows a dependency.
- Include type hints and a module docstring.
- No prose outside the code block.
- Return ONLY a single fenced ```python ... ``` block.
"""

TEST_WRITER = """\
You are TestWriter. Given a spec + implementation, produce pytest tests that
exercise the public interface, including the listed edge cases.

Return ONLY a single fenced ```python ... ``` block. The first line of the
file must be `# tests`. Use plain `assert` style and pytest parametrize
where it sharpens coverage.
"""

SECURITY_REVIEWER = """\
You are SecurityReviewer. Audit the implementation for security and
robustness issues: input validation, injection, path traversal, unsafe
deserialization, integer overflow, race conditions, secret handling,
denial-of-service via unbounded input.

Output STRICTLY:

VERDICT: APPROVED | CHANGES_REQUESTED

## FINDINGS
- bullet list. If APPROVED, write "- none".

## REQUIRED_FIXES
- bullet list of concrete diffs/changes to apply. If APPROVED, write "- none".
"""

DOC_WRITER = """\
You are DocWriter. Given the spec + implementation, write a tight README
section (Markdown) covering: what it does, install, quick example, API
reference table. No fluff. Around 150-250 words.
"""


# ---------------------------------------------------------------------------
# Per-provider agent factories
# ---------------------------------------------------------------------------


def _openai_client() -> Any:
    """Build an OpenAI / Azure OpenAI chat client based on env."""
    if os.getenv("AZURE_OPENAI_ENDPOINT"):
        from agent_framework.azure import AzureOpenAIChatClient  # type: ignore

        return AzureOpenAIChatClient()
    from agent_framework.openai import OpenAIChatClient

    return OpenAIChatClient(model=os.getenv("OPENAI_CHAT_MODEL_ID", "gpt-4o-mini"))


def _make_openai_agent(client: Any, name: str, instructions: str) -> BaseAgent:
    return client.as_agent(name=name, instructions=instructions)


def _make_claude_agent(
    name: str, instructions: str, sandbox_root: Path | None = None
) -> BaseAgent:
    """Build a ClaudeAgent backed by the Claude Code CLI / Claude Agent SDK.

    Two production-grade behaviours wired here:

    1. **Microsoft Foundry routing.** When ``CLAUDE_CODE_USE_FOUNDRY=1`` is set
       (with ``ANTHROPIC_FOUNDRY_RESOURCE`` or ``ANTHROPIC_FOUNDRY_BASE_URL``),
       the underlying ``claude`` CLI subprocess routes inference through your
       Foundry-deployed Claude models (East US 2 / Sweden Central) instead of
       Anthropic's public API. The SDK inherits the parent process env, so
       just exporting these vars before launching the workflow is enough — no
       code changes needed once that's in your shell. We also forward the
       three deployment-name vars so the CLI doesn't fall back to public-API
       defaults.
    2. **Per-agent sandbox.** Each Claude-backed role gets its own ``cwd``
       directory plus bash sandboxing (macOS/Linux). Without this, every
       Claude agent in the graph would share the runner's cwd and could
       overwrite each other's intermediate files when they invoke ``Write``,
       ``Edit``, or ``Bash``. With it, the worst a runaway agent can do is
       trash its own scratch dir.

    The agent is *not* started here — its async lifecycle (``start``/``stop``)
    is managed by the workflow runner via ``agent_lifecycle()``.
    """
    from agent_framework_claude import ClaudeAgent

    # --- Model selection -----------------------------------------------------
    # When routing through Foundry, the "model" is your Foundry deployment
    # name (e.g. "claude-sonnet-4-6"). Otherwise it's the SDK shorthand
    # ("sonnet" / "opus" / "haiku") which the CLI maps to public-API IDs.
    using_foundry = os.getenv("CLAUDE_CODE_USE_FOUNDRY") == "1"
    if using_foundry:
        model = (
            os.getenv("CODE_FORGE_CLAUDE_MODEL")
            or os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or "claude-sonnet-4-6"
        )
    else:
        model = os.getenv("CODE_FORGE_CLAUDE_MODEL", "sonnet")

    options: dict[str, Any] = {"model": model}

    # --- Per-agent sandbox dir ----------------------------------------------
    if sandbox_root is not None:
        agent_dir = sandbox_root / name.lower()
        agent_dir.mkdir(parents=True, exist_ok=True)
        options["cwd"] = str(agent_dir)

        # Enable Claude's built-in bash sandbox on macOS / Linux. This boxes
        # any `Bash` tool calls into a sandbox-exec / bwrap jail so an agent
        # can't reach outside its cwd or hit the network. `git` is in the
        # excluded list because the SDK's git helper needs real subprocess
        # access; everything else stays caged.
        options["sandbox"] = {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "excludedCommands": ["git"],
            "allowUnsandboxedCommands": False,
        }
        # Permission mode: auto-accept edits inside the agent's own sandbox
        # (otherwise every Write/Edit prompts for confirmation and the run
        # blocks indefinitely in headless mode).
        options["permission_mode"] = "acceptEdits"

    return ClaudeAgent(
        name=name,
        instructions=instructions,
        default_options=options,
    )


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

_ROLE_INSTRUCTIONS: dict[str, str] = {
    "spec_analyst": SPEC_ANALYST,
    "implementer": IMPLEMENTER,
    "test_writer": TEST_WRITER,
    "security_reviewer": SECURITY_REVIEWER,
    "doc_writer": DOC_WRITER,
}

_DEFAULT_ROUTING = {
    # Mixed-provider default: Claude reviews, OpenAI does the rest.
    "spec_analyst": "openai",
    "implementer": "openai",
    "test_writer": "openai",
    "security_reviewer": "claude",
    "doc_writer": "openai",
}


def _resolve_routing() -> dict[str, str]:
    mode = os.getenv("CODE_FORGE_PROVIDERS", "mixed").lower()
    if mode == "all-openai":
        return {role: "openai" for role in _ROLE_INSTRUCTIONS}
    if mode == "all-claude":
        return {role: "claude" for role in _ROLE_INSTRUCTIONS}
    if mode in {"mixed", "default"}:
        return dict(_DEFAULT_ROUTING)
    # Allow per-role overrides like "spec_analyst=claude,implementer=openai,..."
    routing = dict(_DEFAULT_ROUTING)
    for chunk in mode.split(","):
        if "=" in chunk:
            role, prov = chunk.split("=", 1)
            role = role.strip()
            prov = prov.strip().lower()
            if role in routing and prov in {"openai", "claude"}:
                routing[role] = prov
    return routing


def build_agents(
    sandbox_root: Path | None = None,
) -> tuple[dict[str, BaseAgent], dict[str, str]]:
    """Instantiate the five role agents and return (agents, routing).

    When ``sandbox_root`` is provided, every Claude-backed agent gets its own
    sub-directory (``sandbox_root/<rolename>/``) as its ``cwd``, and bash
    sandboxing is enabled. This isolates filesystem side-effects per agent
    so two agents writing to ``./scratch.txt`` won't clobber each other.
    """
    routing = _resolve_routing()

    # Lazily build the OpenAI client only if we actually need it.
    openai_client = _openai_client() if "openai" in routing.values() else None

    agents: dict[str, BaseAgent] = {}
    for role, instructions in _ROLE_INSTRUCTIONS.items():
        provider = routing[role]
        display_name = role.replace("_", " ").title().replace(" ", "")
        if provider == "claude":
            agents[role] = _make_claude_agent(
                display_name, instructions, sandbox_root=sandbox_root
            )
        else:
            assert openai_client is not None
            agents[role] = _make_openai_agent(openai_client, display_name, instructions)

    return agents, routing
