"""Shared, deterministic-safe building blocks for the Temporal + MAF POC.

Everything in this package is importable from *both* Temporal workflow code
(which runs inside the deterministic sandbox) and from activity code.

Hard rule: nothing in here may import ``agent_framework``, the Azure SDK,
the Kubernetes client, the GitHub client, or perform any I/O at import time.
Workflow modules import :mod:`shared.contracts` and :mod:`shared.config`; if
either ever pulled in a non-deterministic dependency, the Temporal worker
sandbox would reject the workflow.
"""
