"""Sandbox Orchestrator — request-per-sandbox front door for Code Forge.

This service is the sandbox-native successor to the Go ``session-router``.
Where the router maps ``(dev_id, project_id)`` onto a *warm pod* and patches its
labels (``warm → bound → cooldown``), the orchestrator instead provisions a
fresh **agent sandbox per request** via the upstream
`agent-sandbox <https://agent-sandbox.sigs.k8s.io>`_ project's Python SDK
(``k8s-agent-sandbox``), runs the requested work inside it, and tears it down.

Why sandboxes instead of label-patched pods:

* **Strong per-request isolation.** Every request gets its own pod-backed
  sandbox with its own filesystem and lifecycle. No shared mutable workspace,
  no label race between concurrent binds.
* **Native lifecycle.** The agent-sandbox controller owns warm pooling,
  readiness, TTL shutdown, and cleanup. The orchestrator just claims, runs,
  and terminates — no bespoke reaper logic patching ``state=cooldown``.
* **Same guarantees, less surface.** Budgets, per-dev caps, and the
  default-deny network posture from the router carry over; the warm-pod state
  machine does not.

The package is intentionally split so the HTTP layer, the lifecycle manager,
and the sandbox backend are independently testable. The backend is an
interface (:class:`sandbox_orchestrator.backends.SandboxBackend`) with two
implementations: a thin adapter over the real SDK and an in-memory fake used by
the test-suite, so the full request lifecycle can be exercised without a live
Kubernetes cluster.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
