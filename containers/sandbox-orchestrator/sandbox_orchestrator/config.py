"""Environment-driven configuration for the sandbox orchestrator.

Every knob is an env var so the service stays 12-factor and the Helm chart can
drive it via a ConfigMap (mirroring ``session-router``'s ``router-config``).
Nothing here reads secrets — credentials come from Azure Workload Identity at
runtime, never from env (see ``docs/SECURITY.md``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Connection modes mirror the SDK's four architectures (see the client README).
#   in-cluster   → connect straight to the sandbox pod (orchestrator runs in-cluster)
#   local-tunnel → kubectl port-forward to the sandbox-router (local dev / CI)
#   gateway      → production GKE/AKS Gateway discovery
#   direct       → explicit router api_url
VALID_CONNECTION_MODES = {"in-cluster", "local-tunnel", "gateway", "direct"}

# Backends:
#   direct → create Sandbox CRs directly + drive them via pod exec. Matches the
#            minimal agent-sandbox controller installed here (only the Sandbox
#            CRD exists — no SandboxClaim/Template/WarmPool CRDs).
#   sdk    → upstream k8s-agent-sandbox SandboxClient (needs the claim/template/
#            warmpool CRDs, which this cluster does NOT have).
#   fake   → in-memory simulation (tests, local smoke without a cluster).
VALID_BACKENDS = {"direct", "sdk", "fake"}


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration. Build with :meth:`from_env`."""

    # --- HTTP -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Backend selection -----------------------------------------------
    backend: str = "direct"
    connection_mode: str = "in-cluster"

    # --- Direct backend: sandbox pod shape -------------------------------
    # Container image each sandbox pod runs. For real agent work this is the
    # agent-pod image (ships Claude Code); for smoke tests any shell image works.
    sandbox_image: str = "mcr.microsoft.com/cbl-mariner/busybox:2.0"
    # The sandbox namespace enforces PodSecurity "restricted"; pods must run as
    # a non-root UID. Mirrors the agent-pod's UID 1000.
    sandbox_run_as_user: int = 1000
    # Resource envelope stamped onto the sandbox container.
    sandbox_cpu_request: str = "250m"
    sandbox_cpu_limit: str = "1000m"
    sandbox_memory_request: str = "256Mi"
    sandbox_memory_limit: str = "1Gi"

    # --- Sandbox provisioning --------------------------------------------
    # Namespace the SandboxClaim / SandboxWarmPool live in.
    sandbox_namespace: str = "agent-sandboxes"
    # SandboxTemplate the "sdk" backend builds claims from (and the warm pool
    # is stamped from). Required by the SDK's create_sandbox(template=...).
    sandbox_template: str = "python-sandbox-template"
    # When True, the "direct" backend provisions by creating a SandboxClaim that
    # adopts a pre-warmed pod from the warm pool (fast, session-style), then
    # drives it via pod exec. When False it cold-creates a bare Sandbox CR from
    # the Config pod shape (the original cluster-minimal smoke path).
    sandbox_use_warmpool: bool = True
    # Default warm pool to claim from when a request doesn't name one.
    default_warmpool: str = "python-sandbox-warmpool"
    # How long to wait for a claimed sandbox to report Ready.
    sandbox_ready_timeout: int = 180
    # Server port the sandbox runtime container listens on (SDK default 8888).
    sandbox_server_port: int = 8888
    # Gateway-mode discovery (only used when connection_mode == "gateway").
    gateway_name: str = "external-http-gateway"
    gateway_namespace: str = "default"
    # Direct-mode router URL (only used when connection_mode == "direct").
    router_api_url: str = ""
    # Local-tunnel router namespace (only used when connection_mode == "local-tunnel").
    router_namespace: str = "agent-sandbox-system"

    # --- Lifecycle / safety nets -----------------------------------------
    # TTL handed to the controller via SandboxClaim.spec.lifecycle.shutdownTime.
    # The controller auto-deletes the claim on expiry even if we crash mid-run.
    sandbox_ttl_seconds: int = 3600
    # Per-command execution timeout inside the sandbox.
    command_timeout: int = 300
    # Reaper sweep interval — belt-and-suspenders cleanup of expired records.
    reaper_interval_seconds: int = 30

    # --- Concurrency governance (carried over from session-router) --------
    max_concurrent_sandboxes: int = 100
    max_concurrent_per_dev: int = 3

    # --- Agent command derivation ----------------------------------------
    # When a request supplies a ``task`` but no explicit ``command``, the
    # orchestrator derives the command from this template. ``{task}`` is
    # shell-quoted before substitution. The default runs Claude Code headless
    # inside the sandbox — the same agent Code Forge fronts everywhere else.
    agent_command_template: str = "claude -p {task}"

    # Retention window for finished request records before the reaper evicts
    # them from the in-memory store (production would persist to Redis/Cosmos).
    record_retention_seconds: int = 3600

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        backend = os.getenv("ORCHESTRATOR_BACKEND", "direct").strip().lower()
        if backend not in VALID_BACKENDS:
            raise ValueError(
                f"ORCHESTRATOR_BACKEND must be one of {sorted(VALID_BACKENDS)}, "
                f"got {backend!r}"
            )

        connection_mode = os.getenv("SANDBOX_CONNECTION_MODE", "in-cluster").strip().lower()
        if connection_mode not in VALID_CONNECTION_MODES:
            raise ValueError(
                f"SANDBOX_CONNECTION_MODE must be one of "
                f"{sorted(VALID_CONNECTION_MODES)}, got {connection_mode!r}"
            )

        cfg = cls(
            host=os.getenv("ORCHESTRATOR_HOST", "0.0.0.0"),
            port=_env_int("ORCHESTRATOR_PORT", 8080),
            backend=backend,
            connection_mode=connection_mode,
            sandbox_image=os.getenv("SANDBOX_IMAGE", "mcr.microsoft.com/cbl-mariner/busybox:2.0"),
            sandbox_run_as_user=_env_int("SANDBOX_RUN_AS_USER", 1000),
            sandbox_cpu_request=os.getenv("SANDBOX_CPU_REQUEST", "250m"),
            sandbox_cpu_limit=os.getenv("SANDBOX_CPU_LIMIT", "1000m"),
            sandbox_memory_request=os.getenv("SANDBOX_MEMORY_REQUEST", "256Mi"),
            sandbox_memory_limit=os.getenv("SANDBOX_MEMORY_LIMIT", "1Gi"),
            sandbox_namespace=os.getenv("SANDBOX_NAMESPACE", "agent-sandboxes"),
            sandbox_template=os.getenv("SANDBOX_TEMPLATE", "python-sandbox-template"),
            sandbox_use_warmpool=_env_bool("SANDBOX_USE_WARMPOOL", True),
            default_warmpool=os.getenv("SANDBOX_WARMPOOL", "python-sandbox-warmpool"),
            sandbox_ready_timeout=_env_int("SANDBOX_READY_TIMEOUT", 180),
            sandbox_server_port=_env_int("SANDBOX_SERVER_PORT", 8888),
            gateway_name=os.getenv("SANDBOX_GATEWAY_NAME", "external-http-gateway"),
            gateway_namespace=os.getenv("SANDBOX_GATEWAY_NAMESPACE", "default"),
            router_api_url=os.getenv("SANDBOX_ROUTER_API_URL", ""),
            router_namespace=os.getenv("SANDBOX_ROUTER_NAMESPACE", "agent-sandbox-system"),
            sandbox_ttl_seconds=_env_int("SANDBOX_TTL_SECONDS", 3600),
            command_timeout=_env_int("SANDBOX_COMMAND_TIMEOUT", 300),
            reaper_interval_seconds=_env_int("REAPER_INTERVAL_SECONDS", 30),
            max_concurrent_sandboxes=_env_int("MAX_CONCURRENT_SANDBOXES", 100),
            max_concurrent_per_dev=_env_int("MAX_CONCURRENT_PER_DEV", 3),
            agent_command_template=os.getenv("AGENT_COMMAND_TEMPLATE", "claude -p {task}"),
            record_retention_seconds=_env_int("RECORD_RETENTION_SECONDS", 3600),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.port <= 0 or self.port > 65535:
            raise ValueError(f"port out of range: {self.port}")
        if self.sandbox_ready_timeout <= 0:
            raise ValueError("sandbox_ready_timeout must be positive")
        if self.sandbox_ttl_seconds <= 0:
            raise ValueError("sandbox_ttl_seconds must be positive")
        if self.command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        if self.max_concurrent_sandboxes <= 0:
            raise ValueError("max_concurrent_sandboxes must be positive")
        if self.max_concurrent_per_dev <= 0:
            raise ValueError("max_concurrent_per_dev must be positive")
        if "{task}" not in self.agent_command_template:
            raise ValueError("agent_command_template must contain '{task}'")
        if self.connection_mode == "direct" and not self.router_api_url:
            raise ValueError("SANDBOX_ROUTER_API_URL is required when connection_mode=direct")
