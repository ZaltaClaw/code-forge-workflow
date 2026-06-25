from __future__ import annotations

from shared.contracts import STATUS_SUCCESS, ACTION_CONTINUE, AgentRequest
from planner_agent_worker import agent


def _req():
    return AgentRequest(
        request_id="r1", goal="add healthz", repo_url="https://github.com/o/r",
        environment="dev", stage="planning",
    )


async def test_planner_to_output_maps_to_contract():
    parsed = agent.PlannerResult(
        summary="plan ready", steps=["a", "b"], risk_level="low", rationale="because",
    )
    out = (await agent.to_output(_req(), parsed)).validate()
    assert out.agent_name == "planner"
    assert out.status == STATUS_SUCCESS
    assert out.next_action == ACTION_CONTINUE
    assert out.summary == "plan ready"
    assert out.details["steps"] == ["a", "b"]
    assert out.details["risk_level"] == "low"


def test_planner_build_prompt_includes_goal_and_repo():
    p = agent.build_prompt(_req())
    assert "add healthz" in p
    assert "https://github.com/o/r" in p
