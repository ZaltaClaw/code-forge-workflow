"""Worker bootstrap helpers shared by every ``*_worker/worker.py``.

Activity/worker-side only (does real network I/O) — never imported by
workflow code.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from shared.config import Settings, get_settings
from shared.logging import get_logger, start_health_server

log = get_logger("worker")


async def connect(settings: Settings | None = None) -> Client:
    """Connect a Temporal client to the configured frontend + namespace."""
    settings = settings or get_settings()
    log.info(
        "connecting to Temporal",
        extra={"status": "connecting"},
    )
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    return client


async def run_worker(
    task_queue: str,
    workflows: Sequence[type],
    activities: Sequence[Any] = (),
) -> None:
    """Connect, start the health server, and run a Worker until cancelled.

    One call per worker process. ``activities`` is empty for the orchestrator
    worker (it only hosts the parent workflow).
    """
    settings = get_settings()
    health = start_health_server(settings.health_port)
    client = await connect(settings)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=list(activities),
    )
    log.info(
        "worker started",
        extra={"status": "ready", "stage": task_queue},
    )
    try:
        await worker.run()
    finally:
        health.shutdown()


def main(task_queue: str, workflows: Sequence[type], activities: Sequence[Any] = ()) -> None:
    """Synchronous entrypoint used by ``python -m <pkg>.worker``."""
    try:
        asyncio.run(run_worker(task_queue, workflows, activities))
    except KeyboardInterrupt:
        log.info("worker stopped", extra={"status": "stopped", "stage": task_queue})
