from __future__ import annotations

import pytest

from shared.contracts import (
    STATUS_NEEDS_APPROVAL, STATUS_SUCCESS, STAGE_AKS, AgentOutput, AgentRequest,
)
from approval_agent_worker import agent


def _req(approval_required, aks_pending=False):
    upstream = {}
    if aks_pending:
        upstream[STAGE_AKS] = AgentOutput(
            agent_name="aks", stage="aks", status=STATUS_NEEDS_APPROVAL,
            retryable=False, summary="staged", next_action="ask_human",
            details={"promotion_pending": True},
        )
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="prod" if approval_required else "dev", stage="approval",
        approval_required=approval_required, upstream=upstream,
    )


def _assess(recommendation, risk):
    return agent.ApprovalAssessment(
        recommendation=recommendation, risk_level=risk, reasons=["r"],
    )


@pytest.mark.parametrize("required,aks,reco,risk,expected", [
    (True,  False, "approve",     "low",  STATUS_NEEDS_APPROVAL),  # required by config
    (False, True,  "approve",     "low",  STATUS_NEEDS_APPROVAL),  # AKS staged a promotion
    (False, False, "needs_human", "low",  STATUS_NEEDS_APPROVAL),  # LLM asks for a human
    (False, False, "approve",     "high", STATUS_NEEDS_APPROVAL),  # LLM escalates on risk
    (False, False, "approve",     "low",  STATUS_SUCCESS),         # low-risk, not required
])
async def test_approval_escalation_matrix(monkeypatch, required, aks, reco, risk, expected):
    monkeypatch.setenv("TEMPORAL_APPROVAL_AUTO", "true")
    out = (await agent.to_output(_req(required, aks), _assess(reco, risk))).validate()
    assert out.status == expected
    # The workflow reads these regardless of branch:
    assert "auto_approve" in out.details
    assert "timeout_seconds" in out.details
    assert out.details["recommendation"] == reco
