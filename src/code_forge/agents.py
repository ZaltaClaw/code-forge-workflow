"""Agent definitions for the Code Forge workflow.

Each agent is a focused specialist. They are wired together as a graph by
`workflow.py`. Agents are created from a single `OpenAIChatClient` (or Azure
variant) so a single API key powers the whole pipeline.
"""

from __future__ import annotations

import os
from typing import Any

from agent_framework import BaseAgent


def _build_client() -> Any:
    """Pick an Agent Framework chat client based on environment variables.

    Falls back to OpenAI by default; switches to Azure OpenAI if
    AZURE_OPENAI_ENDPOINT is set.
    """
    if os.getenv("AZURE_OPENAI_ENDPOINT"):
        from agent_framework.azure import AzureOpenAIChatClient  # type: ignore

        return AzureOpenAIChatClient()  # reads AZURE_OPENAI_* env vars

    from agent_framework.openai import OpenAIChatClient

    model = os.getenv("OPENAI_CHAT_MODEL_ID", "gpt-4o-mini")
    return OpenAIChatClient(model=model)


# ---------------------------------------------------------------------------
# Instructions for each role. Tuned to be terse and emit machine-friendly
# section headers so the aggregator/reviewer can pattern-match cheaply.
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


def build_agents() -> dict[str, BaseAgent]:
    """Instantiate the five role agents and return them keyed by role name."""
    client = _build_client()

    return {
        "spec_analyst": client.as_agent(
            name="SpecAnalyst", instructions=SPEC_ANALYST
        ),
        "implementer": client.as_agent(
            name="Implementer", instructions=IMPLEMENTER
        ),
        "test_writer": client.as_agent(
            name="TestWriter", instructions=TEST_WRITER
        ),
        "security_reviewer": client.as_agent(
            name="SecurityReviewer", instructions=SECURITY_REVIEWER
        ),
        "doc_writer": client.as_agent(name="DocWriter", instructions=DOC_WRITER),
    }
