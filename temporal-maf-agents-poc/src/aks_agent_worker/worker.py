"""Entrypoint: ``python -m aks_agent_worker.worker`` (``aks-agent-tq``)."""

from __future__ import annotations

from shared import config
from shared.runtime import main
from aks_agent_worker.activities import run_aks_agent
from aks_agent_worker.workflows import AKSAgentWorkflow

if __name__ == "__main__":
    main(
        task_queue=config.AKS_TASK_QUEUE,
        workflows=[AKSAgentWorkflow],
        activities=[run_aks_agent],
    )
