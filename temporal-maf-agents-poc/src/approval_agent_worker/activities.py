"""Temporal activity for the approval classification stage."""

from __future__ import annotations

import time

from temporalio import activity

from shared.contracts import AgentOutput, AgentRequest
from shared.logging import get_logger
from shared.maf import run_agent
from approval_agent_worker import agent

log = get_logger("approval.activity")


@activity.defn(name="RunApprovalAgentActivity")
async def run_approval_agent(request: AgentRequest) -> AgentOutput:
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
        "approval agent classified",
        extra={
            "request_id": request.request_id,
            "agent_name": agent.AGENT_NAME,
            "stage": request.stage,
            "status": output.status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return output
