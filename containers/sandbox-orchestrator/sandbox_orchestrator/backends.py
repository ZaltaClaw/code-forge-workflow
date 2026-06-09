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
import os
import shlex
import time
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
        template: str,
        warmpool: str | None = None,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        """Provision a sandbox from ``template`` and block until it is Ready.

        When ``warmpool`` is set the claim binds to a pre-warmed sandbox from
        that pool (fast path); otherwise it cold-creates one from ``template``.
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
        template: str,
        warmpool: str | None = None,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        # ``template`` is the SDK's only required arg: the claim sets
        # ``spec.sandboxTemplateRef.name=template``. ``warmpool`` is optional and
        # binds the claim to a pre-warmed sandbox from that pool when supplied.
        kwargs: dict = {
            "template": template,
            "namespace": self._config.sandbox_namespace,
            "sandbox_ready_timeout": self._config.sandbox_ready_timeout,
        }
        if warmpool:
            kwargs["warmpool"] = warmpool
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
# Direct backend — create Sandbox CRs ourselves, drive them via pod exec
# ---------------------------------------------------------------------------


# API coordinates of the agent-sandbox ``Sandbox`` CRD installed on the cluster.
_SANDBOX_GROUP = "agents.x-k8s.io"
_SANDBOX_VERSION = "v1alpha1"
_SANDBOX_PLURAL = "sandboxes"
_SANDBOX_CONTAINER = "sandbox"


class DirectSandboxBackend(SandboxBackend):
    """Provision sandboxes by writing ``Sandbox`` CRs straight to the cluster.

    The upstream ``SandboxClient`` (see :class:`SdkSandboxBackend`) drives a
    ``SandboxClaim`` → ``SandboxTemplate`` → ``SandboxWarmPool`` pipeline. This
    cluster only has the bare ``Sandbox`` CRD installed, so we talk to it
    directly: create a ``Sandbox`` whose ``spec.podTemplate`` describes the
    agent container, wait for the controller to flip its ``Ready`` condition,
    then exec into the resulting pod (which is named after the Sandbox) for
    file staging and command execution. Teardown deletes the CR; the controller
    garbage-collects the pod.
    """

    def __init__(self, config: Config):
        self._config = config
        # Imported lazily so the module loads without the k8s client present.
        from kubernetes import client, config as kconfig

        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        self._k8s = client
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        # Lazily-built SDK cluster helper for the warm-pool claim lifecycle.
        self._claim_helper = None

    # -- provisioning ------------------------------------------------------

    def create_sandbox(
        self,
        *,
        template: str,
        warmpool: str | None = None,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        # Warm-pool path: create a SandboxClaim that adopts a pre-warmed pod from
        # the pool (sub-2s, session-style), then drive it via pod exec. Falls
        # back to cold-creating a bare Sandbox CR when warm pools are disabled.
        if self._config.sandbox_use_warmpool and template:
            return self._provision_via_claim(template, warmpool, labels, ttl_seconds)
        return self._provision_direct_cr(labels)

    def _provision_direct_cr(self, labels: dict[str, str] | None) -> SandboxHandle:
        name = f"cf-sbx-{uuid.uuid4().hex[:10]}"
        namespace = self._config.sandbox_namespace
        manifest = self._build_manifest(name, labels)

        self._custom.create_namespaced_custom_object(
            group=_SANDBOX_GROUP,
            version=_SANDBOX_VERSION,
            namespace=namespace,
            plural=_SANDBOX_PLURAL,
            body=manifest,
        )
        try:
            self._wait_ready(name, namespace, self._config.sandbox_ready_timeout)
        except Exception:
            # Don't leak an orphaned Sandbox if it never came up.
            self._delete(name, namespace)
            raise
        # The controller names the pod identically to the Sandbox CR.
        return SandboxHandle(sandbox_id=name, claim_name=name, _native={"name": name, "namespace": namespace})

    def _provision_via_claim(
        self,
        template: str,
        warmpool: str | None,
        labels: dict[str, str] | None,
        ttl_seconds: int | None,
    ) -> SandboxHandle:
        # Reuse the upstream SDK's cluster helper for the claim lifecycle so we
        # track exactly the SandboxClaim → Sandbox resolution the controller
        # implements (incl. warm-pool adoption, where the sandbox name differs
        # from the claim name). I/O still goes over pod exec, not the SDK's HTTP
        # transport (the agent-pod image doesn't run the sandbox runtime server).
        from k8s_agent_sandbox.k8s_helper import K8sHelper
        from k8s_agent_sandbox.utils import construct_sandbox_claim_lifecycle_spec

        helper = self._claim_helper or K8sHelper()
        self._claim_helper = helper

        namespace = self._config.sandbox_namespace
        timeout = self._config.sandbox_ready_timeout
        claim_name = f"cf-claim-{uuid.uuid4().hex[:10]}"

        ttl = ttl_seconds if ttl_seconds is not None else self._config.sandbox_ttl_seconds
        lifecycle = (
            construct_sandbox_claim_lifecycle_spec(int(ttl)) if ttl and ttl > 0 else None
        )

        helper.create_sandbox_claim(
            claim_name,
            template,
            namespace,
            labels=labels,
            lifecycle=lifecycle,
            warmpool=warmpool,
        )
        try:
            start = time.monotonic()
            sandbox_name = helper.resolve_sandbox_name(claim_name, namespace, timeout)
            remaining = max(1, int(timeout - (time.monotonic() - start)))
            helper.wait_for_sandbox_ready(sandbox_name, namespace, remaining)
        except Exception:
            # Deleting the claim returns/recycles any adopted warm sandbox.
            helper.delete_sandbox_claim(claim_name, namespace)
            raise
        # The controller names the pod identically to the resolved Sandbox CR.
        return SandboxHandle(
            sandbox_id=sandbox_name,
            claim_name=claim_name,
            _native={"name": sandbox_name, "namespace": namespace, "claim": claim_name},
        )


    def _build_manifest(self, name: str, labels: dict[str, str] | None) -> dict:
        cfg = self._config
        metadata: dict[str, Any] = {"name": name}
        if labels:
            metadata["labels"] = labels
        # The sandbox namespace enforces PodSecurity "restricted": every pod must
        # run as non-root, drop all capabilities, forbid privilege escalation,
        # and set a seccomp profile.
        pod_security = {
            "runAsNonRoot": True,
            "runAsUser": cfg.sandbox_run_as_user,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        container_security = {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        container = {
            "name": _SANDBOX_CONTAINER,
            "image": cfg.sandbox_image,
            # Keep the container idle; the orchestrator execs work into it.
            "command": ["/bin/sh", "-c", "sleep infinity"],
            "securityContext": container_security,
            "resources": {
                "requests": {"cpu": cfg.sandbox_cpu_request, "memory": cfg.sandbox_memory_request},
                "limits": {"cpu": cfg.sandbox_cpu_limit, "memory": cfg.sandbox_memory_limit},
            },
        }
        return {
            "apiVersion": f"{_SANDBOX_GROUP}/{_SANDBOX_VERSION}",
            "kind": "Sandbox",
            "metadata": metadata,
            "spec": {
                "podTemplate": {
                    "spec": {
                        "securityContext": pod_security,
                        "containers": [container],
                    }
                }
            },
        }

    def _wait_ready(self, name: str, namespace: str, timeout: int) -> None:
        """Block until the Sandbox reports ``Ready=True`` or the timeout lapses."""
        from kubernetes import watch

        deadline = time.monotonic() + timeout
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError(
                    f"sandbox {name!r} did not become ready within {timeout}s"
                )
            w = watch.Watch()
            try:
                for event in w.stream(
                    self._custom.list_namespaced_custom_object,
                    group=_SANDBOX_GROUP,
                    version=_SANDBOX_VERSION,
                    namespace=namespace,
                    plural=_SANDBOX_PLURAL,
                    field_selector=f"metadata.name={name}",
                    timeout_seconds=remaining,
                ):
                    etype = event.get("type")
                    if etype == "DELETED":
                        w.stop()
                        raise RuntimeError(f"sandbox {name!r} was deleted before ready")
                    obj = event.get("object") or {}
                    status = obj.get("status") or {}
                    for cond in status.get("conditions", []):
                        if cond.get("type") == "Ready" and cond.get("status") == "True":
                            w.stop()
                            return
            finally:
                w.stop()

    # -- driving the sandbox ----------------------------------------------

    def write_file(self, handle: SandboxHandle, path: str, content: str) -> None:
        import base64

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        directory = os.path.dirname(path) or "."
        # base64 in argv avoids any stdin/EOF dance over the exec websocket.
        script = (
            f"mkdir -p {shlex.quote(directory)} && "
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        )
        result = self._exec(handle, ["/bin/sh", "-c", script], timeout=self._config.command_timeout)
        if result.exit_code != 0:
            raise RuntimeError(
                f"failed to write {path!r} into sandbox "
                f"{handle.sandbox_id} (exit {result.exit_code}): {result.stderr.strip()}"
            )

    def run(self, handle: SandboxHandle, command: str, timeout: int) -> ExecResult:
        return self._exec(handle, ["/bin/sh", "-c", command], timeout=timeout)

    def terminate(self, handle: SandboxHandle) -> None:
        native = handle._native
        if not native:
            return
        # Warm-pool sandboxes are owned by their SandboxClaim — deleting the
        # claim recycles the adopted pod. Direct CRs are deleted outright.
        claim = native.get("claim")
        if claim:
            helper = self._claim_helper
            if helper is None:
                from k8s_agent_sandbox.k8s_helper import K8sHelper

                helper = K8sHelper()
                self._claim_helper = helper
            try:
                helper.delete_sandbox_claim(claim, native["namespace"])
            except self._k8s.exceptions.ApiException as exc:
                if exc.status != 404:
                    raise
            return
        self._delete(native["name"], native["namespace"])

    # -- internals ---------------------------------------------------------

    def _delete(self, name: str, namespace: str) -> None:
        try:
            self._custom.delete_namespaced_custom_object(
                group=_SANDBOX_GROUP,
                version=_SANDBOX_VERSION,
                namespace=namespace,
                plural=_SANDBOX_PLURAL,
                name=name,
            )
        except self._k8s.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    def _exec(self, handle: SandboxHandle, argv: list[str], *, timeout: int) -> ExecResult:
        from kubernetes.stream import stream

        native = handle._native
        resp = stream(
            self._core.connect_get_namespaced_pod_exec,
            native["name"],
            native["namespace"],
            container=_SANDBOX_CONTAINER,
            command=argv,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            while resp.is_open():
                if time.monotonic() > deadline:
                    resp.close()
                    raise TimeoutError(f"command timed out after {timeout}s")
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_chunks.append(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_chunks.append(resp.read_stderr())
            rc = resp.returncode
        finally:
            resp.close()
        return ExecResult(
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            exit_code=int(rc) if rc is not None else 0,
        )


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
        template: str,
        warmpool: str | None = None,
        labels: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> SandboxHandle:
        if warmpool and warmpool in self._fail_warmpools:
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
    if config.backend == "sdk":
        return SdkSandboxBackend(config)
    return DirectSandboxBackend(config)


def derive_command(config: Config, *, task: str, command: str) -> str:
    """Resolve the shell command to run inside the sandbox.

    Explicit ``command`` wins; otherwise the natural-language ``task`` is
    shell-quoted and substituted into ``config.agent_command_template``.
    """
    if command.strip():
        return command
    safe_task = shlex.quote(task)
    return config.agent_command_template.format(task=safe_task)
