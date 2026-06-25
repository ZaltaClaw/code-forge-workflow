"""GitHubAgent — executes the source-control part of the plan.

Phase 2 wires the real GitHub API (or a GitHub MCP server) as Agent Framework
tools *inside the activity*. Phase 1 returns a deterministic mock.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from shared import github as gh
from shared.config import get_settings
from shared.contracts import (
    ACTION_CONTINUE,
    ACTION_FAIL,
    STAGE_GITHUB,
    STAGE_PLANNING,
    STATUS_FAILED,
    STATUS_SUCCESS,
    AgentOutput,
    AgentRequest,
)

AGENT_NAME = "github"

INSTRUCTIONS = """\
You are GitHubAgent. Execute the source-control steps of the deployment plan:
create a feature branch, commit the required changes, and open a pull request
against the target repository. Use the provided GitHub tools. Report the branch
name and PR URL in your structured output.
"""


def mock(request: AgentRequest) -> AgentOutput:
    """Deterministic Phase-1 GitHub output, derived from the planner's plan."""
    planner = request.upstream.get(STAGE_PLANNING)
    branch = f"feat/{request.request_id}"
    pr_number = 1
    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_GITHUB,
        status=STATUS_SUCCESS,
        retryable=False,
        summary=f"opened pull request #{pr_number} on branch {branch}",
        next_action=ACTION_CONTINUE,
        details={
            "branch": branch,
            "pr_number": pr_number,
            "pr_url": f"{request.repo_url.rstrip('/')}/pull/{pr_number}",
            "based_on_plan": bool(planner),
        },
    )


class GitHubChange(BaseModel):
    """LLM-authored PR content (the git mechanics are owned by the activity)."""

    pr_title: str = Field(description="Concise PR title")
    pr_body_markdown: str = Field(description="PR description in markdown")
    plan_file_markdown: str = Field(description="Full content for the committed plan file")
    commit_message: str = Field(description="Commit message for the plan file")


RESPONSE_MODEL = GitHubChange


def build_prompt(request: AgentRequest) -> str:
    planner = request.upstream.get(STAGE_PLANNING)
    steps = planner.details.get("steps", []) if planner else []
    steps_text = "\n".join(f"- {s}" for s in steps) or "- (no upstream plan)"
    return (
        f"Engineering goal: {request.goal}\n"
        f"Target repository: {request.repo_url}\n"
        f"Environment: {request.environment}\n"
        f"Planner steps:\n{steps_text}\n\n"
        "Write the pull request title, a markdown PR body, the markdown content "
        "for a committed plan file documenting this change, and a commit message."
    )


async def to_output(request: AgentRequest, parsed: GitHubChange) -> AgentOutput:
    settings = get_settings()
    try:
        details = await asyncio.to_thread(
            gh.create_pr_with_plan,
            repo_url=request.repo_url,
            request_id=request.request_id,
            token=settings.github_token,
            allowed_owner=settings.github_allowed_owner,
            pr_title=parsed.pr_title,
            pr_body=parsed.pr_body_markdown,
            plan_markdown=parsed.plan_file_markdown,
            commit_message=parsed.commit_message,
        )
    except (gh.GitHubWriteNotAllowed, gh.PermanentGitHubError) as exc:
        return AgentOutput(
            agent_name=AGENT_NAME,
            stage=STAGE_GITHUB,
            status=STATUS_FAILED,
            retryable=False,
            summary=f"github write failed: {exc}",
            next_action=ACTION_FAIL,
            details={"error": str(exc), "error_type": type(exc).__name__},
        )

    return AgentOutput(
        agent_name=AGENT_NAME,
        stage=STAGE_GITHUB,
        status=STATUS_SUCCESS,
        retryable=False,
        summary=f"opened pull request #{details['pr_number']} on branch {details['branch']}",
        next_action=ACTION_CONTINUE,
        details=details,
    )
