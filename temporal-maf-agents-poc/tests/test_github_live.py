from __future__ import annotations

import pytest

from shared.contracts import (
    ACTION_CONTINUE, ACTION_FAIL, STATUS_FAILED, STATUS_SUCCESS,
    STAGE_PLANNING, AgentOutput, AgentRequest,
)
from shared import github as gh
from github_agent_worker import agent


def _req():
    return AgentRequest(
        request_id="req-1", goal="add healthz",
        repo_url="https://github.com/example-org/svc", environment="dev",
        stage="github",
        upstream={STAGE_PLANNING: AgentOutput(
            agent_name="planner", stage="planning", status=STATUS_SUCCESS,
            retryable=False, summary="s", next_action=ACTION_CONTINUE,
            details={"steps": ["x"]},
        )},
    )


def _change():
    return agent.GitHubChange(
        pr_title="Add healthz", pr_body_markdown="body",
        plan_file_markdown="# plan", commit_message="add plan",
    )


async def test_github_to_output_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")

    def fake_create(**kwargs):
        assert kwargs["request_id"] == "req-1"
        return {"branch": "feat/req-1", "pr_number": 5,
                "pr_url": "https://github.com/example-org/svc/pull/5",
                "plan_file": "docs/agent-plan-req-1.md", "created_or_existed": "created"}

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_SUCCESS
    assert out.next_action == ACTION_CONTINUE
    assert out.details["pr_number"] == 5
    assert "#5" in out.summary


async def test_github_to_output_guard_violation_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_ALLOWED_OWNER", raising=False)

    def fake_create(**kwargs):
        raise gh.GitHubWriteNotAllowed("fail-closed")

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_FAILED
    assert out.retryable is False
    assert out.next_action == ACTION_FAIL


async def test_github_to_output_permanent_error_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")

    def fake_create(**kwargs):
        raise gh.PermanentGitHubError("404")

    monkeypatch.setattr(gh, "create_pr_with_plan", fake_create)
    out = (await agent.to_output(_req(), _change())).validate()
    assert out.status == STATUS_FAILED
    assert out.next_action == ACTION_FAIL
