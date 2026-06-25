"""Microsoft Agent Framework integration seam.

**This is the only place the POC touches Microsoft Agent Framework**, and it is
only ever called from inside Temporal *activities* — never from workflow code.
Temporal stays the single orchestration authority; Agent Framework is just the
agent runtime that an activity drives.

Phase 1 (default, ``AGENT_MODE=mock``): no network. Each agent returns a
deterministic, structured ``AgentOutput`` so the whole Temporal topology
(parent workflow, child workflows, task queues, retries, KEDA scaling) can be
exercised end-to-end with zero cloud credentials.

Phase 2 (``AGENT_MODE=live``): agents that supply both a ``response_model`` and
a ``to_output`` mapper are driven by a real Azure OpenAI MAF agent. Agents
without live wiring (e.g. AKS) stay on the deterministic mock even when
``AGENT_MODE=live``.

Tool calls (GitHub API, Kubernetes API, Azure) are wired as MAF tools / MCP
servers *here*, so the workflow never sees them.
"""

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
