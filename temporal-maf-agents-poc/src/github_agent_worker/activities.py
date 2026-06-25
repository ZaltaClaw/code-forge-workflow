"""Temporal activity for the GitHub stage (real GitHub API lives here in Phase 2)."""

from __future__ import annotations

import time

from temporalio import activity

from shared.contracts import AgentOutput, AgentRequest
from shared.logging import get_logger
from shared.maf import run_agent
from github_agent_worker import agent

log = get_logger("github.activity")


@activity.defn(name="RunGitHubAgentActivity")
async def run_github_agent(request: AgentRequest) -> AgentOutput:
    started = time.monotonic()
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
    log.info(
        "github agent finished",
        extra={
            "request_id": request.request_id,
            "agent_name": agent.AGENT_NAME,
            "stage": request.stage,
            "status": output.status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return output
