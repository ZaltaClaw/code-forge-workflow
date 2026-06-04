"""Graph-build smoke tests.

These tests prove the workflow graph wires up correctly under each
provider mode without making any network calls. They do NOT exercise the
LLMs themselves — they assert structural correctness:

  * `build_workflow()` returns the (workflow, agents, routing) triple
  * Every documented executor ID is present in `Workflow.executors`
  * Provider routing honours `CODE_FORGE_PROVIDERS=all-openai`
  * Every executor is reachable from the start node via `edge_groups`

This is the layer of correctness CI can guarantee deterministically. Real
end-to-end runs against a live model belong in a manual smoke job, not on
every pull request.
"""

from __future__ import annotations

import os
from collections import deque

# CODE_FORGE_PROVIDERS=all-openai must be set BEFORE importing code_forge so
# the agent factory doesn't import the optional Claude path.
os.environ.setdefault("CODE_FORGE_PROVIDERS", "all-openai")
os.environ.setdefault("OPENAI_API_KEY", "sk-ci-fake-not-used")
os.environ.setdefault("OPENAI_CHAT_MODEL_ID", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------


def test_imports_succeed():
    """The package and its public surface import without optional deps."""
    from code_forge import agents, workflow  # noqa: F401
    from code_forge.workflow import (  # noqa: F401
        AggregatedReview,
        CodePackage,
        FinalArtifact,
        ReviewBundle,
        build_workflow,
    )


# ---------------------------------------------------------------------------
# 2. Builder
# ---------------------------------------------------------------------------


def test_build_workflow_returns_expected_triple():
    """Builder returns (Workflow, agents dict, routing dict)."""
    from agent_framework import Workflow

    from code_forge.workflow import build_workflow

    wf, agents, routing = build_workflow()

    assert isinstance(wf, Workflow)
    assert isinstance(agents, dict) and agents
    assert isinstance(routing, dict) and routing
    assert set(routing.keys()) == set(agents.keys())


# ---------------------------------------------------------------------------
# 3. Topology
# ---------------------------------------------------------------------------


EXPECTED_EXECUTOR_IDS = {
    "spec_analyst",
    "spec_to_request",
    "implementer",
    "fanout_for_review",
    "test_writer",
    "security_reviewer",
    "doc_writer",
    "tagged_tests",
    "tagged_security",
    "tagged_docs",
    "aggregator",
    "revision_loop",
    "finalize",
}


def test_executors_match_documented_topology():
    """`Workflow.executors` contains every documented node, no surprises."""
    from code_forge.workflow import build_workflow

    wf, _, _ = build_workflow()
    actual = set(wf.executors.keys())

    missing = EXPECTED_EXECUTOR_IDS - actual
    assert not missing, f"missing executors: {missing}"

    extra = actual - EXPECTED_EXECUTOR_IDS
    assert not extra, f"unexpected executors: {extra}"


def test_start_executor_is_spec_analyst():
    from code_forge.workflow import build_workflow

    wf, _, _ = build_workflow()
    assert wf.start_executor_id == "spec_analyst"


def test_every_executor_is_reachable_from_start():
    """Walk `edge_groups` BFS from the start node; everything must be reachable."""
    from code_forge.workflow import build_workflow

    wf, _, _ = build_workflow()

    adjacency: dict[str, set[str]] = {}
    for group in wf.edge_groups:
        for src in group.source_executor_ids:
            for tgt in group.target_executor_ids:
                adjacency.setdefault(src, set()).add(tgt)

    visited: set[str] = set()
    queue: deque[str] = deque([wf.start_executor_id])
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        queue.extend(adjacency.get(node, ()))

    # `internal:` prefixed pseudo-nodes from edge_groups don't appear in
    # `executors`; filter them out before asserting full coverage.
    visited_real = {n for n in visited if not n.startswith("internal:")}

    missing = EXPECTED_EXECUTOR_IDS - visited_real
    assert (
        not missing
    ), f"executors unreachable from start: {missing}; visited: {sorted(visited_real)}"


# ---------------------------------------------------------------------------
# 4. Provider routing
# ---------------------------------------------------------------------------


def test_provider_routing_all_openai():
    """In all-openai mode every role routes to OpenAI and no Claude objects exist."""
    from code_forge.workflow import build_workflow

    _, agents, routing = build_workflow()

    assert all(provider == "openai" for provider in routing.values()), (
        f"expected every role to resolve to openai under "
        f"CODE_FORGE_PROVIDERS=all-openai, got: {routing}"
    )

    for name, agent in agents.items():
        cls_name = type(agent).__name__
        assert (
            "Claude" not in cls_name
        ), f"agent {name!r} resolved to {cls_name} despite all-openai mode"


# ---------------------------------------------------------------------------
# 5. Typed-message dataclasses
# ---------------------------------------------------------------------------


def test_dataclass_message_types_are_well_formed():
    """The typed messages flowing through the graph behave as dataclasses."""
    from code_forge.workflow import (
        AggregatedReview,
        CodePackage,
        FinalArtifact,
        ReviewBundle,
    )

    pkg = CodePackage(spec="s", implementation="i")
    assert pkg.spec == "s"
    assert pkg.implementation == "i"

    rb = ReviewBundle(
        kind="security", content="ok", spec="s", implementation="i", revision=0
    )
    assert rb.kind == "security"

    agg = AggregatedReview(
        spec="s",
        implementation="i",
        tests="t",
        docs="d",
        security_report="r",
        verdict="APPROVED",
        revision=0,
    )
    assert agg.verdict == "APPROVED"
    assert agg.revision == 0

    fa = FinalArtifact(
        spec="s",
        implementation="i",
        tests="t",
        docs="d",
        security_report="r",
        revisions=0,
    )
    assert fa.revisions == 0
