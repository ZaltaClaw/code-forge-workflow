"""GitHubAgent — executes the source-control part of the plan.

Phase 2 wires the real GitHub API (or a GitHub MCP server) as Agent Framework
tools *inside the activity*. Phase 1 returns a deterministic mock.
"""

from __future__ import annotations

from shared.contracts import (
    ACTION_CONTINUE,
    STAGE_GITHUB,
    STAGE_PLANNING,
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
