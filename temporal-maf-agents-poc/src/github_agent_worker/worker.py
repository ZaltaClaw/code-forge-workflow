"""Entrypoint: ``python -m github_agent_worker.worker`` (``github-agent-tq``)."""

from __future__ import annotations

from shared import config
from shared.runtime import main
from github_agent_worker.activities import run_github_agent
from github_agent_worker.workflows import GitHubAgentWorkflow

if __name__ == "__main__":
    main(
        task_queue=config.GITHUB_TASK_QUEUE,
        workflows=[GitHubAgentWorkflow],
        activities=[run_github_agent],
    )
