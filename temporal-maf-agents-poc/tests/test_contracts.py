"""Unit tests for the deterministic decision policy and the agent contract.

No Temporal server required — these exercise the pure helpers.
"""

from __future__ import annotations

import pytest

from shared.contracts import (
    ACTION_ASK_HUMAN,
    ACTION_CONTINUE,
    ACTION_FAIL,
    ACTION_RETRY,
    DECISION_APPROVE,
    DECISION_FAIL,
    DECISION_OK,
    DECISION_RETRY,
    STATUS_FAILED,
    STATUS_NEEDS_APPROVAL,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
    decide,
)


def _out(status: str, retryable: bool, action: str) -> AgentOutput:
    return AgentOutput(
        agent_name="t",
        stage="planning",
        status=status,
        retryable=retryable,
        summary="x",
        next_action=action,
    )


@pytest.mark.parametrize(
    "status,retryable,action,expected",
    [
        (STATUS_SUCCESS, False, ACTION_CONTINUE, DECISION_OK),
        (STATUS_FAILED, True, ACTION_RETRY, DECISION_RETRY),
        (STATUS_FAILED, False, ACTION_FAIL, DECISION_FAIL),
        (STATUS_FAILED, True, ACTION_FAIL, DECISION_FAIL),  # retryable but action=fail
        (STATUS_NEEDS_APPROVAL, False, ACTION_ASK_HUMAN, DECISION_APPROVE),
        (STATUS_SUCCESS, False, ACTION_ASK_HUMAN, DECISION_APPROVE),  # ask_human wins
    ],
)
def test_decide(status, retryable, action, expected):
    assert decide(_out(status, retryable, action)) == expected


def test_validate_rejects_bad_status():
    with pytest.raises(ValueError):
        _out("bogus", False, ACTION_CONTINUE).validate()


def test_validate_rejects_bad_action():
    with pytest.raises(ValueError):
        _out(STATUS_SUCCESS, False, "teleport").validate()


def test_agent_request_roundtrip_through_payload():
    upstream = {"planning": _out(STATUS_SUCCESS, False, ACTION_CONTINUE)}
    payload = {
        "request_id": "r1",
        "goal": "g",
        "repo_url": "https://example.com/repo",
        "environment": "dev",
        "stage": "github",
        "approval_required": True,
        "upstream": {
            k: {
                "agent_name": v.agent_name,
                "stage": v.stage,
                "status": v.status,
                "retryable": v.retryable,
                "summary": v.summary,
                "next_action": v.next_action,
                "details": v.details,
            }
            for k, v in upstream.items()
        },
    }
    req = AgentRequest.from_payload(payload)
    assert req.request_id == "r1"
    assert req.stage == "github"
    assert req.upstream["planning"].status == STATUS_SUCCESS
