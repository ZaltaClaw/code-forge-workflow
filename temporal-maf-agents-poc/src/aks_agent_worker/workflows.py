"""AKSAgentWorkflow — child workflow on ``aks-agent-tq``."""

from __future__ import annotations

from temporalio import workflow

from shared import config
from shared.child import run_agent_stage
from shared.contracts import AgentOutput


@workflow.defn(name=config.AKS_WORKFLOW)
class AKSAgentWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> AgentOutput:
        return await run_agent_stage(
            activity_name="RunAKSAgentActivity",
            payload=payload,
        )
