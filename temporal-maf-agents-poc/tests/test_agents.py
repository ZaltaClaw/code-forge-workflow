"""Each agent's Phase-1 mock must emit a valid AgentOutput contract."""

from __future__ import annotations

from shared.contracts import (
    STAGE_AKS,
    STAGE_GITHUB,
    STAGE_PLANNING,
    STATUS_NEEDS_APPROVAL,
    STATUS_SUCCESS,
    AgentRequest,
)

from planner_agent_worker import agent as planner
from github_agent_worker import agent as github
from aks_agent_worker import agent as aks
from approval_agent_worker import agent as approval


def _req(stage: str, approval_required: bool = True, upstream=None) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        goal="add healthcheck",
        repo_url="https://github.com/example-org/svc",
        environment="dev",
        stage=stage,
        approval_required=approval_required,
        upstream=upstream or {},
    )


def test_planner_mock_is_valid():
    out = planner.mock(_req(STAGE_PLANNING)).validate()
    assert out.agent_name == "planner"
    assert out.status == STATUS_SUCCESS
    assert out.details["steps"]


def test_github_mock_uses_plan():
    plan = planner.mock(_req(STAGE_PLANNING))
    out = github.mock(_req(STAGE_GITHUB, upstream={STAGE_PLANNING: plan})).validate()
    assert out.details["based_on_plan"] is True
    assert out.details["pr_url"].endswith("/pull/1")


def test_aks_mock_requires_approval_when_asked():
    out = aks.mock(_req(STAGE_AKS, approval_required=True)).validate()
    assert out.status == STATUS_NEEDS_APPROVAL
    assert out.details["promotion_pending"] is True


def test_aks_mock_autopromotes_when_not_required():
    out = aks.mock(_req(STAGE_AKS, approval_required=False)).validate()
    assert out.status == STATUS_SUCCESS


def test_approval_mock_flags_when_required():
    out = approval.mock(_req("approval", approval_required=True)).validate()
    assert out.status == STATUS_NEEDS_APPROVAL
    assert "auto_approve" in out.details
