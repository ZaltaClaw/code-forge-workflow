"""Kick off an ``AgentOrchestratorWorkflow`` and print the result.

Usage:
    python -m starter                       # uses sample-input.json
    python -m starter path/to/input.json
    python -m starter '{"request_id": "...", "goal": "..."}'

Connects to the Temporal frontend (``TEMPORAL_ADDRESS``) in the
``agent-platform`` namespace and starts the parent workflow on
``orchestrator-tq``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from temporalio.client import Client

from shared import config
from shared.contracts import OrchestrationRequest, OrchestrationResult
from orchestrator_worker.workflows import AgentOrchestratorWorkflow

_DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "sample-input.json"


def _load_request(arg: str | None) -> OrchestrationRequest:
    if arg is None:
        data = json.loads(_DEFAULT_INPUT.read_text())
    elif arg.strip().startswith("{"):
        data = json.loads(arg)
    else:
        data = json.loads(Path(arg).read_text())
    return OrchestrationRequest.from_dict(data)


async def main(arg: str | None) -> None:
    settings = config.get_settings()
    request = _load_request(arg)

    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )

    workflow_id = f"orchestration-{request.request_id}"
    print(f"starting {config.ORCHESTRATOR_WORKFLOW} id={workflow_id} on {config.ORCHESTRATOR_TASK_QUEUE}")

    result: OrchestrationResult = await client.execute_workflow(
        AgentOrchestratorWorkflow.run,
        request,
        id=workflow_id,
        task_queue=config.ORCHESTRATOR_TASK_QUEUE,
    )

    print(json.dumps(dataclasses.asdict(result), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
