"""Entrypoint: ``python -m approval_agent_worker.worker`` (``approval-agent-tq``)."""

from __future__ import annotations

from shared import config
from shared.runtime import main
from approval_agent_worker.activities import run_approval_agent
from approval_agent_worker.workflows import ApprovalAgentWorkflow

if __name__ == "__main__":
    main(
        task_queue=config.APPROVAL_TASK_QUEUE,
        workflows=[ApprovalAgentWorkflow],
        activities=[run_approval_agent],
    )
