from __future__ import annotations

from shared.contracts import STATUS_NEEDS_APPROVAL, STATUS_SUCCESS, AgentRequest
from aks_agent_worker import agent
from aks_agent_worker.activities import run_aks_agent


def _req(approval_required=True):
    return AgentRequest(
        request_id="r1", goal="g", repo_url="https://github.com/o/r",
        environment="dev", stage="aks", approval_required=approval_required,
    )


def test_aks_module_has_no_live_wiring():
    # AKS must NOT expose a response model / to_output -> run_agent stays mock.
    assert not hasattr(agent, "RESPONSE_MODEL")
    assert not hasattr(agent, "to_output")


async def test_aks_activity_stays_mock_even_in_live_mode(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")
    out = await run_aks_agent(_req(approval_required=True))
    # Deterministic mock behaviour (needs_approval when approval_required).
    assert out.agent_name == "aks"
    assert out.status == STATUS_NEEDS_APPROVAL
