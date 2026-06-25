from __future__ import annotations

import pytest

from shared.contracts import (
    ACTION_CONTINUE, STATUS_SUCCESS, AgentOutput, AgentRequest,
)
from shared import maf


def _req(stage="planning"):
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="dev", stage=stage,
    )


def _mock_output(req):
    return AgentOutput(
        agent_name="x", stage=req.stage, status=STATUS_SUCCESS,
        retryable=False, summary="mock", next_action=ACTION_CONTINUE,
    )


async def test_run_agent_uses_mock_when_mode_mock(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "mock")
    out = await maf.run_agent(
        agent_name="x", stage="planning", request=_req(), mock=_mock_output,
    )
    assert out.summary == "mock"


async def test_run_agent_falls_back_to_mock_when_live_wiring_absent(monkeypatch):
    # AKS-style: live mode but no response_model/to_output -> stays mock.
    monkeypatch.setenv("AGENT_MODE", "live")
    out = await maf.run_agent(
        agent_name="aks", stage="aks", request=_req("aks"), mock=_mock_output,
    )
    assert out.summary == "mock"


async def test_run_agent_live_path_calls_to_output(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")

    class Parsed:
        value = 42

    async def fake_live(*, agent_name, instructions, prompt, response_model):
        return Parsed()

    captured = {}

    async def to_output(req, parsed):
        captured["parsed"] = parsed
        return AgentOutput(
            agent_name="x", stage=req.stage, status=STATUS_SUCCESS,
            retryable=False, summary="live", next_action=ACTION_CONTINUE,
        )

    monkeypatch.setattr(maf, "run_live_agent", fake_live)
    out = await maf.run_agent(
        agent_name="x", stage="planning", request=_req(), mock=_mock_output,
        instructions="i", build_prompt=lambda r: "p",
        response_model=Parsed, to_output=to_output,
    )
    assert out.summary == "live"
    assert captured["parsed"].value == 42


async def test_run_live_agent_raises_on_none_value(monkeypatch):
    class FakeResult:
        value = None
        text = "garbage"

    class FakeAgent:
        async def run(self, prompt, options=None):
            return FakeResult()

    class FakeClient:
        def as_agent(self, **kwargs):
            return FakeAgent()

    monkeypatch.setattr(maf, "build_chat_client", lambda settings: FakeClient())
    with pytest.raises(Exception):
        await maf.run_live_agent(
            agent_name="x", instructions="i", prompt="p", response_model=object,
        )
