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


def _make_claude_agent(name: str, instructions: str) -> BaseAgent:
    """Build a ClaudeAgent backed by the Claude Code CLI / Claude Agent SDK.

    The agent is *not* started here — its async lifecycle (`start`/`stop`)
    is managed by the workflow runner via `agent_lifecycle()`.
    """
    from agent_framework_claude import ClaudeAgent

    model = os.getenv("CODE_FORGE_CLAUDE_MODEL", "sonnet")
    return ClaudeAgent(
        name=name,
        instructions=instructions,
        default_options={"model": model},
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


def build_agents() -> tuple[dict[str, BaseAgent], dict[str, str]]:
    """Instantiate the five role agents and return (agents, routing).

    The routing dict is returned so the runner can print which provider
    served each role — useful when verifying mixed-provider runs.
    """
    routing = _resolve_routing()

    # Lazily build the OpenAI client only if we actually need it.
    openai_client = _openai_client() if "openai" in routing.values() else None

    agents: dict[str, BaseAgent] = {}
    for role, instructions in _ROLE_INSTRUCTIONS.items():
        provider = routing[role]
        display_name = role.replace("_", " ").title().replace(" ", "")
        if provider == "claude":
            agents[role] = _make_claude_agent(display_name, instructions)
        else:
            assert openai_client is not None
            agents[role] = _make_openai_agent(openai_client, display_name, instructions)

    return agents, routing
