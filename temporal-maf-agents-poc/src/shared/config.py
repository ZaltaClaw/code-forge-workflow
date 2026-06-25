"""Central configuration: Temporal connection details and task-queue names.

Read from environment variables so the same image runs locally
(docker-compose) and on AKS (Deployment env / ConfigMap) unchanged.

Importable from workflow code: it only reads ``os.environ`` at *call* time,
never performs I/O at import time, and exposes the task-queue names as plain
constants that workflows use to route child workflows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Namespace + task queues (constants — referenced from deterministic workflows)
# ---------------------------------------------------------------------------

NAMESPACE = "agent-platform"

ORCHESTRATOR_TASK_QUEUE = "orchestrator-tq"
PLANNER_TASK_QUEUE = "planner-agent-tq"
GITHUB_TASK_QUEUE = "github-agent-tq"
AKS_TASK_QUEUE = "aks-agent-tq"
APPROVAL_TASK_QUEUE = "approval-agent-tq"

ALL_AGENT_TASK_QUEUES = (
    PLANNER_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    AKS_TASK_QUEUE,
    APPROVAL_TASK_QUEUE,
)

# Workflow / activity type names (string identifiers used when starting
# child workflows by name and when registering with the worker).
ORCHESTRATOR_WORKFLOW = "AgentOrchestratorWorkflow"
PLANNER_WORKFLOW = "PlannerAgentWorkflow"
GITHUB_WORKFLOW = "GitHubAgentWorkflow"
AKS_WORKFLOW = "AKSAgentWorkflow"
APPROVAL_WORKFLOW = "ApprovalAgentWorkflow"


# ---------------------------------------------------------------------------
# Runtime settings (read lazily so importing this module is side-effect free)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    temporal_address: str
    temporal_namespace: str
    # Approval behaviour for the POC. With mocks there is no real human in the
    # loop, so the approval workflow can auto-resolve after a timeout instead
    # of blocking forever. Set TEMPORAL_APPROVAL_AUTO=false to require a signal.
    approval_auto: bool
    approval_timeout_seconds: int
    health_port: int
    # Phase toggle: "mock" (Phase 1, default) or "live" (Phase 2 — real
    # Azure/GitHub/Kubernetes/MCP calls inside activities only).
    agent_mode: str
    # Phase 2 — Azure OpenAI (read lazily; activity-side only)
    azure_openai_endpoint: str | None
    azure_openai_deployment: str | None
    azure_openai_api_version: str
    azure_openai_api_key: str | None
    # Phase 2 — GitHub
    github_token: str | None
    github_allowed_owner: str | None


def get_settings() -> Settings:
    """Build a :class:`Settings` from the current environment.

    Call this from worker / activity / starter code — never at module import
    time inside workflow modules.
    """
    return Settings(
        temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", NAMESPACE),
        approval_auto=os.getenv("TEMPORAL_APPROVAL_AUTO", "true").lower() != "false",
        approval_timeout_seconds=int(os.getenv("TEMPORAL_APPROVAL_TIMEOUT_SECONDS", "30")),
        health_port=int(os.getenv("HEALTH_PORT", "8080")),
        agent_mode=os.getenv("AGENT_MODE", "mock").lower(),
        azure_openai_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        azure_openai_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        azure_openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_openai_api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_allowed_owner=os.getenv("GITHUB_ALLOWED_OWNER"),
    )
