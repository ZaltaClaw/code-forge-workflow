"""Temporal activity for the planning stage.

The activity is the boundary where the deterministic Temporal world meets the
non-deterministic agent world. Everything external (LLM calls, tool calls)
happens here, never in workflow code.
"""

from __future__ import annotations

import os
import time

from temporalio import activity

from shared.contracts import AgentOutput, AgentRequest
from shared.logging import get_logger
from shared.maf import run_agent
from planner_agent_worker import agent

log = get_logger("planner.activity")


@activity.defn(name="RunPlannerAgentActivity")
async def run_planner_agent(request: AgentRequest) -> AgentOutput:
    started = time.monotonic()
    info = activity.info()

    # --- Demo hook: prove Temporal's activity-level retries (layer 1) --------
    # Set FORCE_TRANSIENT_ERROR=1 to make the activity raise on its first
    # attempt; Temporal's RetryPolicy then re-runs it (attempt 2 succeeds).
    if os.getenv("FORCE_TRANSIENT_ERROR") == "1" and info.attempt < 2:
        log.warning(
            "injected transient error to exercise retry policy",
            extra={"request_id": request.request_id, "stage": request.stage,
                   "agent_name": agent.AGENT_NAME, "error_type": "TransientError"},
        )
        raise RuntimeError("injected transient error (will be retried by Temporal)")

    output = await run_agent(
        agent_name=agent.AGENT_NAME,
        stage=request.stage,
        instructions=agent.INSTRUCTIONS,
        request=request,
        mock=agent.mock,
    )

    log.info(
        "planner agent finished",
        extra={
            "request_id": request.request_id,
            "agent_name": agent.AGENT_NAME,
            "stage": request.stage,
            "status": output.status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return output
