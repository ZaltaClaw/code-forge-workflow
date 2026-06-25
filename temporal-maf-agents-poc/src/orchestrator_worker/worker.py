"""Entrypoint for the orchestrator worker.

    python -m orchestrator_worker.worker

Hosts only the parent workflow on ``orchestrator-tq``. It has no activities of
its own — all real work happens in the agent workers' activities.
"""

from __future__ import annotations

from shared import config
from shared.runtime import main
from orchestrator_worker.workflows import AgentOrchestratorWorkflow

if __name__ == "__main__":
    main(
        task_queue=config.ORCHESTRATOR_TASK_QUEUE,
        workflows=[AgentOrchestratorWorkflow],
        activities=[],
    )
