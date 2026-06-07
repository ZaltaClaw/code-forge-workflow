"""Sandbox backend abstraction.

The orchestrator talks to sandboxes exclusively through :class:`SandboxBackend`.
This keeps the lifecycle manager (``manager.py``) free of SDK details and lets
the test-suite swap in :class:`FakeSandboxBackend` to exercise the full request
lifecycle with no Kubernetes cluster, no port-forwards, and no network.

Two implementations:

* :class:`SdkSandboxBackend` — thin adapter over the upstream
  ``k8s-agent-sandbox`` ``SandboxClient``. Imported lazily so this module (and
  the tests) load even when the SDK isn't installed.
* :class:`FakeSandboxBackend` — deterministic in-memory simulation.

The contract is deliberately tiny — claim, run, write files, terminate — which
is exactly the slice of the SDK the orchestrator needs.
"""

from __future__ import annotations

import abc
import shlex
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .models import ExecResult


@dataclass
class SandboxHandle:
    """Opaque reference to a provisioned sandbox.

    ``sandbox_id`` / ``claim_name`` are surfaced for audit + correlation;
    ``_native`` carries the backend-specific object (a real SDK ``Sandbox`` or
    a fake) and must not be inspected outside the backend that created it.
    """

    sandbox_id: str
    claim_name: str
    # Backend-specific object (real SDK ``Sandbox`` or ``_FakeSandbox``).
    # Typed ``Any`` on purpose — only the owning backend may touch it.
    _native: Any = None


class SandboxBackend(abc.ABC):
    """Provision, drive, and tear down agent sandboxes."""

    @abc.abstractmethod
    def create_sandbox(
        self,
        *,
        warmpool: str,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        """Claim a sandbox from ``warmpool`` and block until it is Ready.

        Raises on timeout or provisioning failure; the caller is responsible
        for marking the request FAILED.
        """

    @abc.abstractmethod
    def write_file(self, handle: SandboxHandle, path: str, content: str) -> None:
        """Write ``content`` to ``path`` inside the sandbox before the run."""

    @abc.abstractmethod
    def run(self, handle: SandboxHandle, command: str, timeout: int) -> ExecResult:
        """Execute ``command`` inside the sandbox and return its result."""

    @abc.abstractmethod
    def terminate(self, handle: SandboxHandle) -> None:
        """Permanently delete the sandbox. Must be idempotent."""


# ---------------------------------------------------------------------------
# Real SDK adapter
# ---------------------------------------------------------------------------


class SdkSandboxBackend(SandboxBackend):
    """Adapter over the upstream ``k8s-agent-sandbox`` SandboxClient.

    The connection config is derived from :class:`Config.connection_mode` to
    match the SDK's four documented architectures. In production the
    orchestrator runs *inside* the cluster, so ``in-cluster`` (direct
    pod-to-pod) is the default and avoids the router hop entirely.
    """

    def __init__(self, config: Config):
        self._config = config
        self._client = self._build_client(config)

    @staticmethod
    def _build_client(config: Config):
        # Imported lazily so the module loads without the SDK present.
        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import (
            SandboxDirectConnectionConfig,
            SandboxGatewayConnectionConfig,
            SandboxInClusterConnectionConfig,
            SandboxLocalTunnelConnectionConfig,
        )

        mode = config.connection_mode
        if mode == "in-cluster":
            conn = SandboxInClusterConnectionConfig(server_port=config.sandbox_server_port)
        elif mode == "gateway":
            conn = SandboxGatewayConnectionConfig(
                gateway_name=config.gateway_name,
                gateway_namespace=config.gateway_namespace,
                server_port=config.sandbox_server_port,
            )
        elif mode == "direct":
            conn = SandboxDirectConnectionConfig(
                api_url=config.router_api_url,
                server_port=config.sandbox_server_port,
            )
        else:  # local-tunnel
            conn = SandboxLocalTunnelConnectionConfig(
                server_port=config.sandbox_server_port,
                router_namespace=config.router_namespace,
            )
        # cleanup=False: the orchestrator owns teardown explicitly per request
        # rather than relying on the atexit hook (it's a long-running service).
        return SandboxClient(connection_config=conn, cleanup=False)

    def create_sandbox(
        self,
        *,
        warmpool: str,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        kwargs: dict = {
            "warmpool": warmpool,
            "namespace": self._config.sandbox_namespace,
            "sandbox_ready_timeout": self._config.sandbox_ready_timeout,
        }
        if labels:
            kwargs["labels"] = labels
        ttl = ttl_seconds if ttl_seconds is not None else self._config.sandbox_ttl_seconds
        if ttl is not None and ttl > 0:
            # SDK keyword-only arg: controller auto-deletes the claim on expiry,
            # so a crashed orchestrator can never leak a sandbox.
            kwargs["shutdown_after_seconds"] = ttl

        sandbox = self._client.create_sandbox(**kwargs)
        return SandboxHandle(
            sandbox_id=getattr(sandbox, "sandbox_id", ""),
            claim_name=getattr(sandbox, "claim_name", ""),
            _native=sandbox,
        )

    def write_file(self, handle: SandboxHandle, path: str, content: str) -> None:
        sandbox = handle._native
        # The SDK exposes a Filesystem engine via ``sandbox.files``.
        sandbox.files.write(path, content)

    def run(self, handle: SandboxHandle, command: str, timeout: int) -> ExecResult:
        sandbox = handle._native
        result = sandbox.commands.run(command, timeout=timeout)
        return ExecResult(
            stdout=getattr(result, "stdout", "") or "",
            stderr=getattr(result, "stderr", "") or "",
            exit_code=int(getattr(result, "exit_code", -1)),
        )

    def terminate(self, handle: SandboxHandle) -> None:
        sandbox = handle._native
        if sandbox is None:
            return
        # SDK terminate() is idempotent and swallows 404s.
        sandbox.terminate()


# ---------------------------------------------------------------------------
# In-memory fake (tests + cluster-free smoke)
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """A simulated sandbox with a virtual filesystem and a command hook."""

    def __init__(self, sandbox_id: str, claim_name: str, runner: Callable[[str, dict[str, str]], ExecResult]):
        self.sandbox_id = sandbox_id
        self.claim_name = claim_name
        self.files: dict[str, str] = {}
        self.terminated = False
        self._runner = runner

    def run(self, command: str, timeout: int) -> ExecResult:
        if self.terminated:
            raise RuntimeError("sandbox already terminated")
        return self._runner(command, self.files)


class FakeSandboxBackend(SandboxBackend):
    """Deterministic in-memory backend.

    By default every command "succeeds" with an echo of itself. Tests can
    inject a custom ``runner`` to simulate non-zero exits, specific stdout, or
    provisioning failures (via ``fail_on_create`` / ``fail_warmpools``).
    """

    def __init__(
        self,
        *,
        runner: Callable[[str, dict[str, str]], ExecResult] | None = None,
        fail_warmpools: set[str] | None = None,
    ):
        self.created: list[_FakeSandbox] = []
        self.terminated_ids: list[str] = []
        self._runner = runner or self._default_runner
        self._fail_warmpools = fail_warmpools or set()

    @staticmethod
    def _default_runner(command: str, files: dict[str, str]) -> ExecResult:
        return ExecResult(stdout=f"ran: {command}\n", stderr="", exit_code=0)

    def create_sandbox(
        self,
        *,
        warmpool: str,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        if warmpool in self._fail_warmpools:
            raise RuntimeError(f"warmpool {warmpool!r} has no ready sandboxes")
        sid = f"sbx-{uuid.uuid4().hex[:8]}"
        claim = f"sandbox-claim-{uuid.uuid4().hex[:8]}"
        sandbox = _FakeSandbox(sid, claim, self._runner)
        self.created.append(sandbox)
        return SandboxHandle(sandbox_id=sid, claim_name=claim, _native=sandbox)

    def write_file(self, handle: SandboxHandle, path: str, content: str) -> None:
        handle._native.files[path] = content

    def run(self, handle: SandboxHandle, command: str, timeout: int) -> ExecResult:
        return handle._native.run(command, timeout)

    def terminate(self, handle: SandboxHandle) -> None:
        sandbox = handle._native
        if sandbox is None or sandbox.terminated:
            return
        sandbox.terminated = True
        self.terminated_ids.append(sandbox.sandbox_id)


def build_backend(config: Config) -> SandboxBackend:
    """Factory: pick the backend named by ``config.backend``."""
    if config.backend == "fake":
        return FakeSandboxBackend()
    return SdkSandboxBackend(config)


def derive_command(config: Config, *, task: str, command: str) -> str:
    """Resolve the shell command to run inside the sandbox.

    Explicit ``command`` wins; otherwise the natural-language ``task`` is
    shell-quoted and substituted into ``config.agent_command_template``.
    """
    if command.strip():
        return command
    safe_task = shlex.quote(task)
    return config.agent_command_template.format(task=safe_task)
