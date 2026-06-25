"""Entrypoint: ``python -m planner_agent_worker.worker`` (``planner-agent-tq``)."""

from __future__ import annotations

from shared import config
from shared.runtime import main
from planner_agent_worker.activities import run_planner_agent
from planner_agent_worker.workflows import PlannerAgentWorkflow

if __name__ == "__main__":
    main(
        task_queue=config.PLANNER_TASK_QUEUE,
        workflows=[PlannerAgentWorkflow],
        activities=[run_planner_agent],
    )
