"""Opt-in live smoke test. Skipped unless real creds + RUN_LIVE_SMOKE=1.

Run with:
    RUN_LIVE_SMOKE=1 AGENT_MODE=live \
    AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_CHAT_DEPLOYMENT=... \
    GITHUB_TOKEN=... GITHUB_ALLOWED_OWNER=<owner> \
    PYTHONPATH=src pytest tests/test_live_smoke.py -v
"""

from __future__ import annotations

import os

import pytest

from shared.contracts import STATUS_SUCCESS, AgentRequest
from planner_agent_worker import agent as planner

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_SMOKE") != "1"
    or not os.getenv("AZURE_OPENAI_ENDPOINT")
    or not os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
    reason="live smoke disabled (set RUN_LIVE_SMOKE=1 + Azure env to enable)",
)


async def test_planner_round_trip_against_azure_openai(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")
    from shared.maf import run_agent

    req = AgentRequest(
        request_id="smoke-1", goal="add a /healthz endpoint",
        repo_url=f"https://github.com/{os.getenv('GITHUB_ALLOWED_OWNER','example-org')}/svc",
        environment="dev", stage="planning",
    )
    out = await run_agent(
        agent_name=planner.AGENT_NAME, stage="planning", instructions=planner.INSTRUCTIONS,
        request=req, mock=planner.mock, build_prompt=planner.build_prompt,
        response_model=planner.RESPONSE_MODEL, to_output=planner.to_output,
    )
    assert out.status == STATUS_SUCCESS
    assert out.details["steps"]
