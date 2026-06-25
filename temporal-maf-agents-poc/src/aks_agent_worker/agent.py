"""AKSAgent — applies the Kubernetes/AKS part of the plan.

Phase 2 wires the real Kubernetes API (or an AKS MCP server) as Agent
Framework tools inside the activity. ``approval_required`` flows through to a
``needs_approval`` signal so the Approval stage can gate promotion.
"""

from __future__ import annotations

from shared.contracts import (
    ACTION_ASK_HUMAN,
    ACTION_CONTINUE,
    STAGE_AKS,
    STAGE_GITHUB,
    STATUS_NEEDS_APPROVAL,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
)

AGENT_NAME = "aks"

INSTRUCTIONS = """\
You are AKSAgent. Apply the Kubernetes manifests for this change to the target
AKS cluster/namespace for the requested environment. Use the provided
Kubernetes tools. If the change targets a protected environment and approval is
required, do NOT promote — return needs_approval and let the Approval agent
gate it. Report the resources applied in your structured output.
"""


def mock(request: AgentRequest) -> AgentOutput:
    """Deterministic Phase-1 AKS output.

    Stages the rollout and, when approval is required, hands off to the
    approval gate via ``needs_approval`` / ``ask_human``.
    """
    github = request.upstream.get(STAGE_GITHUB)
    applied = [
        f"namespace/{request.environment}",
        "deployment/agent-app",
        "service/agent-app",
    ]
    needs_approval = request.approval_required

    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_AKS,
        status=STATUS_NEEDS_APPROVAL if needs_approval else STATUS_SUCCESS,
        retryable=False,
        summary=(
            "manifests staged; awaiting approval before promotion"
            if needs_approval
            else "manifests applied"
        ),
        next_action=ACTION_ASK_HUMAN if needs_approval else ACTION_CONTINUE,
        details={
            "environment": request.environment,
            "applied": applied,
            "from_pr": github.details.get("pr_number") if github else None,
            "promotion_pending": needs_approval,
        },
    )
