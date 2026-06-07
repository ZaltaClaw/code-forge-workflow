"""Domain models for the sandbox orchestrator.

Plain dataclasses (no pydantic dependency) so the core is import-light and the
fake backend / unit tests run with zero third-party installs. The HTTP layer
serialises these to/from JSON by hand in ``api.py``.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


class RequestState(str, enum.Enum):
    """Lifecycle of a single user request → sandbox run.

    ::

        PENDING ──▶ PROVISIONING ──▶ RUNNING ──▶ SUCCEEDED
                          │              │            │
                          └──────────────┴────────────┴──▶ FAILED
                                                       └──▶ TERMINATED
    """

    PENDING = "pending"            # accepted, not yet acted on
    PROVISIONING = "provisioning"  # claiming + waiting for sandbox Ready
    RUNNING = "running"            # command executing inside the sandbox
    SUCCEEDED = "succeeded"        # command finished, exit_code == 0
    FAILED = "failed"              # provisioning error or non-zero exit
    TERMINATED = "terminated"      # explicitly cancelled / sandbox torn down

    @property
    def is_terminal(self) -> bool:
        return self in {RequestState.SUCCEEDED, RequestState.FAILED, RequestState.TERMINATED}


@dataclass
class SandboxRequest:
    """An inbound user request for a one-shot agent sandbox run.

    Exactly one of ``task`` or ``command`` should be set by the caller:

    * ``task``    — natural-language work; the orchestrator derives the agent
                    command from ``Config.agent_command_template``.
    * ``command`` — an explicit shell command to run verbatim in the sandbox.
    """

    dev_id: str
    project_id: str = ""
    task: str = ""
    command: str = ""
    warmpool: str = ""              # overrides Config.default_warmpool when set
    ttl_seconds: int | None = None  # overrides Config.sandbox_ttl_seconds when set
    files: dict[str, str] = field(default_factory=dict)  # path → content, written pre-run
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.dev_id or not self.dev_id.strip():
            raise ValueError("dev_id is required")
        if not (self.task.strip() or self.command.strip()):
            raise ValueError("one of 'task' or 'command' is required")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SandboxRequest":
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        allowed = {
            "dev_id", "project_id", "task", "command",
            "warmpool", "ttl_seconds", "files", "labels", "metadata",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        req = cls(
            dev_id=str(data.get("dev_id", "")),
            project_id=str(data.get("project_id", "")),
            task=str(data.get("task", "")),
            command=str(data.get("command", "")),
            warmpool=str(data.get("warmpool", "")),
            ttl_seconds=data.get("ttl_seconds"),
            files=dict(data.get("files", {}) or {}),
            labels=dict(data.get("labels", {}) or {}),
            metadata=dict(data.get("metadata", {}) or {}),
        )
        req.validate()
        return req


@dataclass
class ExecResult:
    """Result of running the agent command inside the sandbox."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1


@dataclass
class SandboxRecord:
    """Server-side state for one request through its full lifecycle.

    In production this would be persisted to Redis (hot) + Cosmos (audit),
    mirroring the session-router. The in-memory store is the reference
    implementation and what the tests drive.
    """

    request_id: str
    dev_id: str
    project_id: str
    state: RequestState
    warmpool: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Populated once the backend hands us a sandbox handle.
    sandbox_id: str = ""
    claim_name: str = ""
    # Populated once the command has run.
    result: ExecResult | None = None
    error: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    def touch(self, state: RequestState | None = None) -> None:
        if state is not None:
            self.state = state
        self.updated_at = time.time()

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe view returned to API callers."""
        out: dict[str, Any] = {
            "request_id": self.request_id,
            "dev_id": self.dev_id,
            "project_id": self.project_id,
            "state": self.state.value,
            "warmpool": self.warmpool,
            "sandbox_id": self.sandbox_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.result is not None:
            out["result"] = asdict(self.result)
        if self.error:
            out["error"] = self.error
        return out


def new_request_id() -> str:
    """Short, collision-resistant id used in URLs and sandbox labels."""
    return f"req-{uuid.uuid4().hex[:12]}"
