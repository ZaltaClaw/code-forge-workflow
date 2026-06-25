"""ApprovalAgentWorkflow — durable human-in-the-loop gate (``approval-agent-tq``).

Unlike the other agent workflows, this one does more than schedule an activity:
after the agent classifies the rollout, the workflow **durably waits** for a
human decision signal (or auto-resolves after a timeout in non-prod). This is
the canonical Temporal pattern for ``needs_approval`` — the wait survives worker
restarts because it is part of workflow state, not an in-memory timer.

Resolve the gate from the Temporal CLI::

    temporal workflow signal \\
      --workflow-id <request_id>-approval \\
      --name submit_decision \\
      --input '{"approved": true, "reason": "LGTM"}'

Still deterministic: the wait, the timeout, and the decision logic are all
Temporal-managed. The only environment-derived inputs (auto-approve, timeout)
arrive via the activity's structured ``details``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow

from shared import config
from shared.child import ACTIVITY_RETRY_POLICY, ACTIVITY_START_TO_CLOSE
from shared.contracts import (
    ACTION_CONTINUE,
    ACTION_FAIL,
    ACTION_ASK_HUMAN,
    STATUS_FAILED,
    STATUS_NEEDS_APPROVAL,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
)


@workflow.defn(name=config.APPROVAL_WORKFLOW)
class ApprovalAgentWorkflow:
    def __init__(self) -> None:
        self._decision: tuple[bool, str] | None = None

    @workflow.signal
    def submit_decision(self, approved: bool, reason: str = "") -> None:
        """Human (or external system) approves/rejects the rollout."""
        self._decision = (bool(approved), str(reason))

    @workflow.query
    def is_pending(self) -> bool:
        """True while the workflow is still waiting on a human decision."""
        return self._decision is None

    @workflow.run
    async def run(self, payload: dict) -> AgentOutput:
        request = AgentRequest.from_payload(payload)

        classification: AgentOutput = await workflow.execute_activity(
            "RunApprovalAgentActivity",
            request,
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
            retry_policy=ACTIVITY_RETRY_POLICY,
            result_type=AgentOutput,
        )

        # Nothing to gate — auto-promoted.
        if classification.status != STATUS_NEEDS_APPROVAL:
            return classification

        auto_approve = bool(classification.details.get("auto_approve", True))
        timeout_seconds = int(classification.details.get("timeout_seconds", 30))

        workflow.logger.info(
            "awaiting human approval",
            extra={"request_id": request.request_id, "stage": request.stage,
                   "status": STATUS_NEEDS_APPROVAL},
        )

        # Durable wait for the signal, bounded by a timer.
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=timeout_seconds),
            )
        except asyncio.TimeoutError:
            pass

        if self._decision is None:
            if auto_approve:
                self._decision = (True, "auto-approved after timeout (non-prod POC policy)")
            else:
                # Still pending: surface needs_approval back to the orchestrator.
                return AgentOutput(
                    agent_name=classification.agent_name,
                    stage=request.stage,
                    status=STATUS_NEEDS_APPROVAL,
                    retryable=False,
                    summary="approval timed out; rollout still pending human decision",
                    next_action=ACTION_ASK_HUMAN,
                    details={**classification.details, "resolved": False},
                )

        approved, reason = self._decision
        if approved:
            return AgentOutput(
                agent_name=classification.agent_name,
                stage=request.stage,
                status=STATUS_SUCCESS,
                retryable=False,
                summary="approved — promotion authorised",
                next_action=ACTION_CONTINUE,
                details={**classification.details, "resolved": True, "approved": True,
                         "reason": reason},
            )

        return AgentOutput(
            agent_name=classification.agent_name,
            stage=request.stage,
            status=STATUS_FAILED,
            retryable=False,
            summary="rejected — promotion denied by human",
            next_action=ACTION_FAIL,
            details={**classification.details, "resolved": True, "approved": False,
                     "reason": reason},
        )
