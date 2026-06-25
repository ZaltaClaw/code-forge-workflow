"""End-to-end Temporal test: parent workflow drives all four child workflows.

Uses Temporal's in-memory time-skipping test environment, so the approval
timer fires instantly and no external Temporal server is needed. The very
first run downloads the test-server binary; if that download is unavailable
the test skips rather than failing.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("temporalio")

from temporalio.worker import Worker

from shared import config
from shared.contracts import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    OrchestrationRequest,
    OrchestrationResult,
)

from orchestrator_worker.workflows import AgentOrchestratorWorkflow
from planner_agent_worker.workflows import PlannerAgentWorkflow
from planner_agent_worker.activities import run_planner_agent
from github_agent_worker.workflows import GitHubAgentWorkflow
from github_agent_worker.activities import run_github_agent
from aks_agent_worker.workflows import AKSAgentWorkflow
from aks_agent_worker.activities import run_aks_agent
from approval_agent_worker.workflows import ApprovalAgentWorkflow
from approval_agent_worker.activities import run_approval_agent


def _workers(client):
    """One Worker per task queue, sharing the test client."""
    return [
        Worker(
            client,
            task_queue=config.ORCHESTRATOR_TASK_QUEUE,
            workflows=[AgentOrchestratorWorkflow],
        ),
        Worker(
            client,
            task_queue=config.PLANNER_TASK_QUEUE,
            workflows=[PlannerAgentWorkflow],
            activities=[run_planner_agent],
        ),
        Worker(
            client,
            task_queue=config.GITHUB_TASK_QUEUE,
            workflows=[GitHubAgentWorkflow],
            activities=[run_github_agent],
        ),
        Worker(
            client,
            task_queue=config.AKS_TASK_QUEUE,
            workflows=[AKSAgentWorkflow],
            activities=[run_aks_agent],
        ),
        Worker(
            client,
            task_queue=config.APPROVAL_TASK_QUEUE,
            workflows=[ApprovalAgentWorkflow],
            activities=[run_approval_agent],
        ),
    ]


async def _run(client, request: OrchestrationRequest) -> OrchestrationResult:
    import contextlib

    workers = _workers(client)
    async with contextlib.AsyncExitStack() as stack:
        for w in workers:
            await stack.enter_async_context(w)
        return await client.execute_workflow(
            AgentOrchestratorWorkflow.run,
            request,
            id=f"test-{uuid.uuid4()}",
            task_queue=config.ORCHESTRATOR_TASK_QUEUE,
        )


@pytest.fixture()
async def env():
    from temporalio.testing import WorkflowEnvironment

    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # pragma: no cover - offline / no binary
        pytest.skip(f"time-skipping test server unavailable: {exc}")
    async with environment:
        yield environment


async def test_full_pipeline_auto_approves(env, monkeypatch):
    # Auto-approve so the approval gate resolves under time-skipping.
    monkeypatch.setenv("TEMPORAL_APPROVAL_AUTO", "true")
    monkeypatch.setenv("AGENT_MODE", "mock")

    result = await _run(
        env.client,
        OrchestrationRequest(
            request_id="itest-1",
            goal="add a healthcheck endpoint",
            repo_url="https://github.com/example-org/svc",
            environment="dev",
            approval_required=True,
        ),
    )

    assert result.status == STATUS_SUCCESS
    stages = {s.stage: s for s in result.stages}
    assert set(stages) == {"planning", "github", "aks", "approval"}
    assert stages["aks"].status == "needs_approval"  # gated mid-pipeline
    assert stages["approval"].status == STATUS_SUCCESS  # then auto-approved


async def test_approval_rejection_returns_failed(env, monkeypatch):
    # Require a real human signal (no auto-approve), then reject it. Driving the
    # ApprovalAgentWorkflow directly with a start-signal makes this fully
    # deterministic under time-skipping (no signal/timer race).
    monkeypatch.setenv("TEMPORAL_APPROVAL_AUTO", "false")
    monkeypatch.setenv("AGENT_MODE", "mock")

    import contextlib

    client = env.client
    payload = {
        "request_id": "itest-2",
        "goal": "risky prod change",
        "repo_url": "https://github.com/example-org/svc",
        "environment": "prod",
        "approval_required": True,
        "stage": "approval",
        "upstream": {},
    }

    async with contextlib.AsyncExitStack() as stack:
        for w in _workers(client):
            await stack.enter_async_context(w)

        # The reject signal is buffered and applied before the durable wait, so
        # the gate resolves immediately as a rejection.
        result = await client.execute_workflow(
            ApprovalAgentWorkflow.run,
            payload,
            id=f"test-reject-{uuid.uuid4()}",
            task_queue=config.APPROVAL_TASK_QUEUE,
            start_signal="submit_decision",
            start_signal_args=[False, "no go"],
        )

    assert result.status == STATUS_FAILED
    assert result.details["approved"] is False
    assert result.next_action == "fail"
