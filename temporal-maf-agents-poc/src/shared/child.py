"""Reusable child-workflow body shared by every agent worker.

Each ``*_agent_worker/workflows.py`` defines its own ``@workflow.defn`` class
(Temporal needs distinct workflow types per task queue) but delegates the
actual control flow to :func:`run_agent_stage` here so the retry / fail /
approval policy lives in exactly one place.

This module is imported by workflow code, so it stays deterministic: it only
uses ``temporalio.workflow`` APIs plus the pure helpers in
:mod:`shared.contracts`. No SDKs, clocks, randomness, or I/O.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from shared.contracts import (
    DECISION_APPROVE,
    DECISION_OK,
    DECISION_RETRY,
    AgentOutput,
    AgentRequest,
    decide,
)

# Layer 1: Temporal's own activity retries. Fires when an activity *raises*
# (transient infra errors: network blips, throttling, etc.). Mirrors the
# spec's "Retry Policy > Activities" section exactly.
ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=120),
    maximum_attempts=3,
)

# How long a single agent activity may run before Temporal times it out.
ACTIVITY_START_TO_CLOSE = timedelta(minutes=5)


async def run_agent_stage(
    *,
    activity_name: str,
    payload: dict,
    soft_retry_limit: int = 2,
) -> AgentOutput:
    """Run an agent activity and apply the business-level decision policy.

    Two retry layers:
      1. ``ACTIVITY_RETRY_POLICY`` — Temporal retries the activity if it raises.
      2. Soft retries here — when the activity *returns* a structured
         ``failed + retryable`` output (a business failure, not an exception),
         we re-invoke up to ``soft_retry_limit`` times before failing the
         workflow. ``needs_approval`` is returned as-is for the caller (the
         approval workflow) to gate on.
    """
    request = AgentRequest.from_payload(payload)

    attempt = 0
    while True:
        output: AgentOutput = await workflow.execute_activity(
            activity_name,
            request,
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
            retry_policy=ACTIVITY_RETRY_POLICY,
            result_type=AgentOutput,
        )

        decision = decide(output)
        workflow.logger.info(
            f"activity {activity_name} -> status={output.status} decision={decision}",
            extra={
                "request_id": request.request_id,
                "stage": request.stage,
                "status": output.status,
            },
        )

        if decision in (DECISION_OK, DECISION_APPROVE):
            return output

        if decision == DECISION_RETRY and attempt < soft_retry_limit:
            attempt += 1
            continue

        # DECISION_FAIL, or soft retries exhausted -> fail the workflow.
        raise ApplicationError(
            f"{activity_name} failed: {output.summary}",
            output,  # surfaced as ApplicationError detail for debugging
            type="AgentFailed",
            non_retryable=True,
        )
