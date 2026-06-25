"""Typed contracts shared across the orchestrator and every agent worker.

These dataclasses are the *wire format* between Temporal workflows and
activities. Temporal's default (Pydantic-free) data converter serialises
dataclasses to JSON out of the box, so keep every field JSON-native
(str / int / bool / float / list / dict / nested dataclass).

This module is deterministic and dependency-free on purpose — it is imported
by workflow code that runs inside the Temporal sandbox. Do **not** add I/O,
clocks, randomness, or third-party SDK imports here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Enumerated string values (kept as plain str constants so they serialise
# cleanly and stay comparable inside deterministic workflow code).
# ---------------------------------------------------------------------------

# Agent status values
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_NEEDS_APPROVAL = "needs_approval"
VALID_STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAILED, STATUS_NEEDS_APPROVAL})

# Agent next_action values
ACTION_CONTINUE = "continue"
ACTION_RETRY = "retry"
ACTION_FAIL = "fail"
ACTION_ROLLBACK = "rollback"
ACTION_ASK_HUMAN = "ask_human"
VALID_ACTIONS = frozenset(
    {ACTION_CONTINUE, ACTION_RETRY, ACTION_FAIL, ACTION_ROLLBACK, ACTION_ASK_HUMAN}
)

# Pipeline stages (one per child workflow)
STAGE_PLANNING = "planning"
STAGE_GITHUB = "github"
STAGE_AKS = "aks"
STAGE_APPROVAL = "approval"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationRequest:
    """Top-level input to :class:`AgentOrchestratorWorkflow`.

    Mirrors ``sample-input.json``.
    """

    request_id: str
    goal: str
    repo_url: str
    environment: str = "dev"
    approval_required: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrchestrationRequest":
        return cls(
            request_id=str(data["request_id"]),
            goal=str(data["goal"]),
            repo_url=str(data.get("repo_url", "")),
            environment=str(data.get("environment", "dev")),
            approval_required=bool(data.get("approval_required", True)),
        )


@dataclass
class AgentRequest:
    """What a single agent activity receives.

    Carries the original orchestration context plus the structured outputs of
    every upstream stage, so e.g. the GitHub agent can read the planner's plan.
    """

    request_id: str
    goal: str
    repo_url: str
    environment: str
    stage: str
    # Whether a human approval gate is required for this run.
    approval_required: bool = True
    # Outputs of prior stages, keyed by stage name. Lets each agent build on
    # the work of the agents before it without re-deriving anything.
    upstream: dict[str, "AgentOutput"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # When Temporal's data converter rebuilds this dataclass on the activity
        # side, the nested `upstream` values may arrive as plain dicts. Coerce
        # them to AgentOutput so every agent can rely on attribute access.
        if self.upstream:
            self.upstream = {
                key: value if isinstance(value, AgentOutput) else AgentOutput.from_dict(value)
                for key, value in self.upstream.items()
            }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AgentRequest":
        """Rebuild an AgentRequest from the dict a child workflow receives."""
        upstream_raw = payload.get("upstream", {}) or {}
        return cls(
            request_id=str(payload["request_id"]),
            goal=str(payload.get("goal", "")),
            repo_url=str(payload.get("repo_url", "")),
            environment=str(payload.get("environment", "dev")),
            stage=str(payload["stage"]),
            approval_required=bool(payload.get("approval_required", True)),
            upstream={k: AgentOutput.from_dict(v) for k, v in upstream_raw.items()},
        )


# ---------------------------------------------------------------------------
# Output contract (the structured result every agent must return)
# ---------------------------------------------------------------------------


@dataclass
class AgentOutput:
    """Structured output contract returned by every agent.

    Matches the "Agent Output Contract" in the spec::

        {
          "agent_name": "planner",
          "stage": "planning",
          "status": "success",
          "retryable": false,
          "summary": "deployment plan created",
          "details": {},
          "next_action": "continue"
        }
    """

    agent_name: str
    stage: str
    status: str  # one of VALID_STATUSES
    retryable: bool
    summary: str
    next_action: str  # one of VALID_ACTIONS
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentOutput":
        return cls(
            agent_name=str(data["agent_name"]),
            stage=str(data["stage"]),
            status=str(data["status"]),
            retryable=bool(data.get("retryable", False)),
            summary=str(data.get("summary", "")),
            next_action=str(data.get("next_action", ACTION_CONTINUE)),
            details=dict(data.get("details", {})),
        )

    def validate(self) -> "AgentOutput":
        """Cheap, deterministic sanity check. Raises ValueError on a bad contract."""
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid status {self.status!r}; expected one of {sorted(VALID_STATUSES)}")
        if self.next_action not in VALID_ACTIONS:
            raise ValueError(
                f"invalid next_action {self.next_action!r}; expected one of {sorted(VALID_ACTIONS)}"
            )
        return self


@dataclass
class OrchestrationResult:
    """Final return value of the parent workflow. Mirrors ``sample-output.json``."""

    request_id: str
    status: str  # "success" | "failed" | "needs_approval"
    stages: list[AgentOutput] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Deterministic decision helper used by *workflow* code.
# ---------------------------------------------------------------------------

# Decisions the orchestration/child workflows act on after each agent runs.
DECISION_OK = "ok"  # status success -> move on
DECISION_RETRY = "retry"  # failed + retryable -> run the activity again
DECISION_FAIL = "fail"  # failed + not retryable -> fail the workflow
DECISION_APPROVE = "approve"  # needs_approval -> wait for a human decision


def decide(output: AgentOutput) -> str:
    """Map an :class:`AgentOutput` to the next control-flow decision.

    Pure function — safe to call from inside deterministic workflow code.
    This is the single source of truth for the retry/fail/approval policy
    described in the spec's "Workflow Behavior" section.
    """
    if output.status == STATUS_NEEDS_APPROVAL or output.next_action == ACTION_ASK_HUMAN:
        return DECISION_APPROVE
    if output.status == STATUS_SUCCESS:
        return DECISION_OK
    # status == failed (or anything unexpected) -> honour the retryable flag
    if output.retryable and output.next_action == ACTION_RETRY:
        return DECISION_RETRY
    return DECISION_FAIL
