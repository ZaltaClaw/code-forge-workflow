"""PlannerAgentWorkflow — child workflow on ``planner-agent-tq``.

Deterministic: it only schedules the planning activity (via the shared child
helper) and returns its structured output. No external calls here.
"""

from __future__ import annotations

from temporalio import workflow

from shared import config
from shared.child import run_agent_stage
from shared.contracts import AgentOutput


@workflow.defn(name=config.PLANNER_WORKFLOW)
class PlannerAgentWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> AgentOutput:
        return await run_agent_stage(
            activity_name="RunPlannerAgentActivity",
            payload=payload,
        )
