"""The parent orchestration workflow.

``AgentOrchestratorWorkflow`` is the single orchestration authority. It runs
the four agent **child workflows** in sequence, each on its own task queue,
passing the structured output of every stage forward to the next.

Determinism rules (enforced by the Temporal sandbox):
  * No LLM / Azure / Kubernetes / GitHub / Agent Framework calls here.
  * No wall-clock, no randomness, no network, no file I/O.
  * All external work happens inside activities, reached via the child
    workflows. This module only orchestrates.

Only deterministic, sandbox-safe imports are allowed below.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# These imports are pure dataclasses / constants — safe inside the sandbox.
from shared import config
from shared.contracts import (
    STAGE_AKS,
    STAGE_APPROVAL,
    STAGE_GITHUB,
    STAGE_PLANNING,
    STATUS_FAILED,
    STATUS_SUCCESS,
    AgentOutput,
    OrchestrationRequest,
    OrchestrationResult,
)

# Child workflows are started by *name* (strings from config) so the
# orchestrator never has to import the agent workflow modules.
_CHILD_RETRY = RetryPolicy(maximum_attempts=3)


@workflow.defn(name=config.ORCHESTRATOR_WORKFLOW)
class AgentOrchestratorWorkflow:
    """User request -> Planner -> GitHub -> AKS -> Approval -> final result."""

    def __init__(self) -> None:
        self._stage: str = "init"

    @workflow.query
    def current_stage(self) -> str:
        """Live progress query — handy from the Temporal UI / CLI."""
        return self._stage

    @workflow.run
    async def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        workflow.logger.info(
            "orchestration started",
            extra={"request_id": request.request_id, "stage": "orchestrator"},
        )

        stages: list[AgentOutput] = []
        upstream: dict[str, AgentOutput] = {}

        # The fixed pipeline: (stage name, child workflow name, task queue).
        pipeline = [
            (STAGE_PLANNING, config.PLANNER_WORKFLOW, config.PLANNER_TASK_QUEUE),
            (STAGE_GITHUB, config.GITHUB_WORKFLOW, config.GITHUB_TASK_QUEUE),
            (STAGE_AKS, config.AKS_WORKFLOW, config.AKS_TASK_QUEUE),
            (STAGE_APPROVAL, config.APPROVAL_WORKFLOW, config.APPROVAL_TASK_QUEUE),
        ]

        for stage, wf_name, task_queue in pipeline:
            self._stage = stage
            # Build the per-agent payload. We pass a dict for `upstream` so the
            # child can rebuild typed AgentOutput objects on its side.
            child_input = {
                "request_id": request.request_id,
                "goal": request.goal,
                "repo_url": request.repo_url,
                "environment": request.environment,
                "approval_required": request.approval_required,
                "stage": stage,
                "upstream": {k: _as_dict(v) for k, v in upstream.items()},
            }

            workflow.logger.info(
                f"executing child workflow for stage={stage}",
                extra={"request_id": request.request_id, "stage": stage},
            )

            output: AgentOutput = await workflow.execute_child_workflow(
                wf_name,
                child_input,
                id=f"{request.request_id}-{stage}",
                task_queue=task_queue,
                retry_policy=_CHILD_RETRY,
                result_type=AgentOutput,
            )

            stages.append(output)
            upstream[stage] = output

            # A child workflow only returns a terminal AgentOutput. If a stage
            # could not be salvaged it raises (failing this workflow) rather
            # than returning. A clean needs_approval reaching the *final*
            # result is surfaced, not treated as failure.
            if output.status == STATUS_FAILED:
                self._stage = "failed"
                raise ApplicationError(
                    f"stage {stage} failed and was not retryable: {output.summary}",
                    type="StageFailed",
                    non_retryable=True,
                )

        self._stage = "done"

        # Final status is governed by the *approval* (final) stage: an
        # intermediate needs_approval from the AKS stage is resolved by the
        # approval workflow, so only its terminal verdict matters here.
        final_status = stages[-1].status if stages else STATUS_SUCCESS

        result = OrchestrationResult(
            request_id=request.request_id,
            status=final_status,
            stages=stages,
            summary=f"orchestration complete for goal: {request.goal}",
        )
        workflow.logger.info(
            "orchestration finished",
            extra={"request_id": request.request_id, "status": final_status},
        )
        return result


def _as_dict(output: AgentOutput) -> dict:
    """Deterministic dataclass->dict (no stdlib `asdict`, to stay obvious)."""
    return {
        "agent_name": output.agent_name,
        "stage": output.stage,
        "status": output.status,
        "retryable": output.retryable,
        "summary": output.summary,
        "next_action": output.next_action,
        "details": output.details,
    }
