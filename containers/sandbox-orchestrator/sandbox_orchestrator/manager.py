"""Lifecycle manager — the orchestration core.

The manager owns the request → sandbox → result → teardown lifecycle. It is the
sandbox-native replacement for the session-router's warm-pod claim/patch/reaper
loop. Responsibilities:

* **Admission control** — enforce global and per-dev concurrency caps before
  provisioning anything (carried over from the router's ``MAX_CONCURRENT_*``).
* **Lifecycle** — drive each request through the :class:`RequestState` machine,
  always terminating the sandbox in a ``finally`` so a crash mid-run cannot
  leak compute. The controller-side TTL is the second safety net.
* **Bookkeeping** — keep an in-memory record store (the reference impl; prod
  swaps in Redis + Cosmos) and a background reaper that evicts stale records.

Thread-safety: the manager is synchronous and guarded by a single re-entrant
lock. The FastAPI layer runs ``handle_request`` in a worker thread
(``run_in_threadpool``) so the blocking SDK calls don't stall the event loop,
and concurrent requests serialise only around the short critical sections
(admission + record mutation), not around the long sandbox run.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

from .backends import SandboxBackend, SandboxHandle, derive_command
from .config import Config
from .models import (
    ExecResult,
    RequestState,
    SandboxRecord,
    SandboxRequest,
    new_request_id,
)

logger = logging.getLogger("sandbox_orchestrator.manager")


class CapacityError(RuntimeError):
    """Raised when an admission cap would be exceeded. Maps to HTTP 429."""


class NotFoundError(KeyError):
    """Raised when a request_id is unknown. Maps to HTTP 404."""


class SandboxManager:
    """Coordinates request admission, sandbox lifecycle, and record-keeping."""

    def __init__(self, config: Config, backend: SandboxBackend):
        self._config = config
        self._backend = backend
        self._records: dict[str, SandboxRecord] = {}
        self._active_by_dev: dict[str, int] = {}
        self._active_total = 0
        self._lock = threading.RLock()
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    # -- lifecycle of the manager itself ----------------------------------

    def start_reaper(self) -> None:
        if self._reaper_thread is not None:
            return
        self._reaper_stop.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, name="record-reaper", daemon=True
        )
        self._reaper_thread.start()
        logger.info("record reaper started (interval=%ss)", self._config.reaper_interval_seconds)

    def stop_reaper(self) -> None:
        self._reaper_stop.set()
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=5)
            self._reaper_thread = None

    # -- admission control -------------------------------------------------

    def _admit(self, dev_id: str) -> None:
        """Reserve a concurrency slot or raise CapacityError. Caller holds lock."""
        if self._active_total >= self._config.max_concurrent_sandboxes:
            raise CapacityError(
                f"global concurrency cap reached "
                f"({self._config.max_concurrent_sandboxes})"
            )
        dev_active = self._active_by_dev.get(dev_id, 0)
        if dev_active >= self._config.max_concurrent_per_dev:
            raise CapacityError(
                f"per-dev concurrency cap reached for {dev_id!r} "
                f"({self._config.max_concurrent_per_dev})"
            )
        self._active_by_dev[dev_id] = dev_active + 1
        self._active_total += 1

    def _release(self, dev_id: str) -> None:
        """Release a previously reserved slot. Caller holds lock."""
        self._active_total = max(0, self._active_total - 1)
        remaining = self._active_by_dev.get(dev_id, 0) - 1
        if remaining <= 0:
            self._active_by_dev.pop(dev_id, None)
        else:
            self._active_by_dev[dev_id] = remaining

    # -- the main entrypoint ----------------------------------------------

    def handle_request(self, req: SandboxRequest) -> SandboxRecord:
        """Provision a sandbox, run the work, tear down, and return the record.

        Blocking and synchronous: intended to be called from a threadpool
        worker. Admission is checked up-front; the slot is held for the entire
        run and always released in ``finally``.
        """
        req.validate()
        warmpool = req.warmpool or self._config.default_warmpool
        record = SandboxRecord(
            request_id=new_request_id(),
            dev_id=req.dev_id,
            project_id=req.project_id,
            state=RequestState.PENDING,
            warmpool=warmpool,
            labels=dict(req.labels),
        )

        with self._lock:
            self._admit(req.dev_id)
            self._records[record.request_id] = record

        handle: SandboxHandle | None = None
        try:
            # --- provision ------------------------------------------------
            self._set_state(record, RequestState.PROVISIONING)
            labels = self._sandbox_labels(req, record)
            handle = self._backend.create_sandbox(
                warmpool=warmpool,
                labels=labels,
                ttl_seconds=req.ttl_seconds if req.ttl_seconds is not None
                else self._config.sandbox_ttl_seconds,
            )
            with self._lock:
                record.sandbox_id = handle.sandbox_id
                record.claim_name = handle.claim_name
                record.touch()

            # --- stage input files ---------------------------------------
            for path, content in req.files.items():
                self._backend.write_file(handle, path, content)

            # --- run ------------------------------------------------------
            command = derive_command(self._config, task=req.task, command=req.command)
            self._set_state(record, RequestState.RUNNING)
            result = self._backend.run(handle, command, timeout=self._config.command_timeout)

            with self._lock:
                record.result = result
                record.touch(
                    RequestState.SUCCEEDED if result.exit_code == 0 else RequestState.FAILED
                )
            if result.exit_code != 0:
                logger.warning(
                    "request %s command exited %s", record.request_id, result.exit_code
                )
            return record

        except CapacityError:
            raise
        except Exception as exc:  # noqa: BLE001 - record then surface
            logger.exception("request %s failed", record.request_id)
            with self._lock:
                record.error = str(exc)
                if record.result is None:
                    record.result = ExecResult(exit_code=-1, stderr=str(exc))
                record.touch(RequestState.FAILED)
            return record
        finally:
            # Always tear the sandbox down and free the admission slot.
            if handle is not None:
                try:
                    self._backend.terminate(handle)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "failed to terminate sandbox %s (TTL will reap it)",
                        getattr(handle, "sandbox_id", "?"),
                    )
            with self._lock:
                self._release(req.dev_id)

    def cancel(self, request_id: str) -> SandboxRecord:
        """Mark a request TERMINATED. Best-effort; the run's ``finally`` still
        owns sandbox teardown, so this is primarily a bookkeeping signal."""
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise NotFoundError(request_id)
            if not record.state.is_terminal:
                record.touch(RequestState.TERMINATED)
            return record

    # -- queries -----------------------------------------------------------

    def get(self, request_id: str) -> SandboxRecord:
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise NotFoundError(request_id)
            return record

    def list_records(self, *, dev_id: str | None = None) -> list[SandboxRecord]:
        with self._lock:
            records: Iterable[SandboxRecord] = list(self._records.values())
        if dev_id is not None:
            records = [r for r in records if r.dev_id == dev_id]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_total": self._active_total,
                "active_by_dev": dict(self._active_by_dev),
                "records": len(self._records),
                "max_concurrent_sandboxes": self._config.max_concurrent_sandboxes,
                "max_concurrent_per_dev": self._config.max_concurrent_per_dev,
            }

    # -- internals ---------------------------------------------------------

    def _set_state(self, record: SandboxRecord, state: RequestState) -> None:
        with self._lock:
            record.touch(state)

    def _sandbox_labels(self, req: SandboxRequest, record: SandboxRecord) -> dict[str, str]:
        labels = {
            "code-forge.io/request-id": record.request_id,
            "code-forge.io/dev-id": _sanitize_label(req.dev_id),
            "code-forge.io/managed-by": "sandbox-orchestrator",
        }
        if req.project_id:
            labels["code-forge.io/project-id"] = _sanitize_label(req.project_id)
        labels.update(req.labels)
        return labels

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(self._config.reaper_interval_seconds):
            try:
                self._evict_stale()
            except Exception:  # noqa: BLE001
                logger.exception("reaper sweep failed")

    def _evict_stale(self) -> None:
        cutoff = time.time() - self._config.record_retention_seconds
        with self._lock:
            stale = [
                rid for rid, rec in self._records.items()
                if rec.state.is_terminal and rec.updated_at < cutoff
            ]
            for rid in stale:
                del self._records[rid]
        if stale:
            logger.info("reaper evicted %d stale record(s)", len(stale))


def _sanitize_label(value: str) -> str:
    """Coerce an arbitrary id into a DNS-1123-ish label value (<=63 chars)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return safe[:63].strip("-_.") or "unknown"
