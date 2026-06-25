"""ApprovalAgent — decides whether the rollout needs a human sign-off.

The agent itself does not block; it only classifies. The *workflow*
(``ApprovalAgentWorkflow``) owns the durable wait for a human decision signal.
The activity injects the (env-derived) approval behaviour into ``details`` so
the deterministic workflow can read it without touching the environment.
"""

from __future__ import annotations

from shared.config import get_settings
from shared.contracts import (
    ACTION_ASK_HUMAN,
    ACTION_CONTINUE,
    STAGE_AKS,
    STAGE_APPROVAL,
    STATUS_NEEDS_APPROVAL,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
)

AGENT_NAME = "approval"

INSTRUCTIONS = """\
You are ApprovalAgent. Review the staged deployment (plan, PR, and AKS rollout)
and decide whether it can be promoted automatically or requires explicit human
approval. If approval is required, emit needs_approval/ask_human and stop — a
human (or an auto-approve policy in non-prod) resolves the gate.
"""


def mock(request: AgentRequest) -> AgentOutput:
    """Deterministic Phase-1 approval classification.

    Requires approval when the run asked for it or when the AKS stage staged a
    rollout pending promotion.
    """
    settings = get_settings()
    aks = request.upstream.get(STAGE_AKS)
    aks_pending = bool(aks and aks.details.get("promotion_pending"))
    needs_approval = request.approval_required or aks_pending

    # Behaviour the workflow uses to drive its durable wait. Carried in details
    # so the deterministic workflow never reads os.environ itself.
    behaviour = {
        "auto_approve": settings.approval_auto,
        "timeout_seconds": settings.approval_timeout_seconds,
        "environment": request.environment,
    }

    if not needs_approval:
        return AgentOutput(
            agent_name=AGENT_NAME,
            stage=STAGE_APPROVAL,
            status=STATUS_SUCCESS,
            retryable=False,
            summary="no approval required; auto-promoted",
            next_action=ACTION_CONTINUE,
            details=behaviour,
        )

    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_APPROVAL,
        status=STATUS_NEEDS_APPROVAL,
        retryable=False,
        summary=f"human approval required to promote to {request.environment}",
        next_action=ACTION_ASK_HUMAN,
        details=behaviour,
    )
