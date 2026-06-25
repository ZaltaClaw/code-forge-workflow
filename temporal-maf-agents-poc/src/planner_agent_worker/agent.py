"""PlannerAgent — the Microsoft Agent Framework agent for the planning stage.

Phase 1 ships a deterministic mock so the Temporal topology can be proven with
no cloud credentials. The ``INSTRUCTIONS`` below are the real system prompt the
live (Phase 2) agent would use.

Activity-only module — safe to import Agent Framework here (it never touches
workflow code).
"""

from __future__ import annotations

from shared.contracts import (
    ACTION_CONTINUE,
    STAGE_PLANNING,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
)

AGENT_NAME = "planner"

INSTRUCTIONS = """\
You are PlannerAgent. Given a high-level engineering goal and a target
repository, produce a concrete, ordered deployment plan: the code changes
required, the GitHub actions (branch, PR), the AKS resources to apply, and the
approval checkpoints. Output a structured plan the downstream GitHub, AKS, and
Approval agents can execute. Be explicit and deterministic.
"""


def mock(request: AgentRequest) -> AgentOutput:
    """Deterministic Phase-1 planning output."""
    plan = [
        f"Create feature branch for: {request.goal}",
        "Open a pull request against the target repository",
        f"Apply Kubernetes manifests to the '{request.environment}' environment",
        "Request human approval before promotion",
    ]
    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_PLANNING,
        status=STATUS_SUCCESS,
        retryable=False,
        summary="deployment plan created",
        next_action=ACTION_CONTINUE,
        details={
            "goal": request.goal,
            "repo_url": request.repo_url,
            "environment": request.environment,
            "steps": plan,
        },
    )
