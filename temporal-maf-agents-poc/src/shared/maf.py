"""Microsoft Agent Framework integration seam.

**This is the only place the POC touches Microsoft Agent Framework**, and it is
only ever called from inside Temporal *activities* — never from workflow code.
Temporal stays the single orchestration authority; Agent Framework is just the
agent runtime that an activity drives.

Phase 1 (default, ``AGENT_MODE=mock``): no network. Each agent returns a
deterministic, structured ``AgentOutput`` so the whole Temporal topology
(parent workflow, child workflows, task queues, retries, KEDA scaling) can be
exercised end-to-end with zero cloud credentials.

Phase 2 (``AGENT_MODE=live``): replace the TODO stub in :func:`run_live_agent`
with a real Agent Framework agent backed by Azure OpenAI + MCP tools. The
construction pattern mirrors the sibling ``code_forge`` project::

    from agent_framework.azure import AzureOpenAIChatClient
    client = AzureOpenAIChatClient()                     # reads AZURE_OPENAI_* env
    agent = client.as_agent(name=name, instructions=instructions)
    result = await agent.run(prompt)                     # result.text holds output

Tool calls (GitHub API, Kubernetes API, Azure) are wired as MAF tools / MCP
servers *here*, so the workflow never sees them.
"""

from __future__ import annotations

from typing import Any, Callable

from shared.config import get_settings
from shared.contracts import AgentOutput, AgentRequest

# A "mock factory" produces the canned AgentOutput for an agent in Phase 1.
MockFactory = Callable[[AgentRequest], AgentOutput]


async def run_agent(
    *,
    agent_name: str,
    stage: str,
    instructions: str,
    request: AgentRequest,
    mock: MockFactory,
    build_prompt: Callable[[AgentRequest], str] | None = None,
) -> AgentOutput:
    """Run one agent and return its structured output.

    Dispatches to the deterministic mock (Phase 1) or the live MAF agent
    (Phase 2) based on ``AGENT_MODE``.
    """
    settings = get_settings()
    if settings.agent_mode == "live":
        prompt = (build_prompt or _default_prompt)(request)
        return await run_live_agent(
            agent_name=agent_name,
            stage=stage,
            instructions=instructions,
            prompt=prompt,
            request=request,
        )
    return mock(request).validate()


def _default_prompt(request: AgentRequest) -> str:
    return (
        f"Goal: {request.goal}\n"
        f"Repository: {request.repo_url}\n"
        f"Environment: {request.environment}\n"
        f"Stage: {request.stage}\n"
        f"Upstream results: {request.upstream}"
    )


async def run_live_agent(
    *,
    agent_name: str,
    stage: str,
    instructions: str,
    prompt: str,
    request: AgentRequest,
) -> AgentOutput:
    """Phase 2: real Microsoft Agent Framework agent. TODO — wire this up.

    Replace the body below with the real implementation. Keep it inside this
    function so workflow code stays clean. Suggested skeleton::

        from agent_framework.azure import AzureOpenAIChatClient
        client = AzureOpenAIChatClient()
        agent = client.as_agent(name=agent_name, instructions=instructions)
        # TODO: register tools / MCP servers for this agent's stage
        result = await agent.run(prompt)
        parsed = _parse_structured_output(result.text)   # enforce the contract
        return AgentOutput(agent_name=agent_name, stage=stage, **parsed).validate()
    """
    raise NotImplementedError(
        "AGENT_MODE=live is a Phase 2 TODO. Wire Microsoft Agent Framework here "
        "(Azure OpenAI + MCP tools), inside this activity-only seam. "
        "Phase 1 uses AGENT_MODE=mock."
    )


# Convenience used by Phase 2 once wired (left here so the seam is obvious).
def build_chat_client() -> Any:  # pragma: no cover - Phase 2
    """Construct an Azure OpenAI chat client for Agent Framework. TODO Phase 2."""
    from agent_framework.azure import AzureOpenAIChatClient  # type: ignore

    return AzureOpenAIChatClient()
