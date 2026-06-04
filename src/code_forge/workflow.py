"""Graph workflow that wires the five coding agents together.

Topology
========

    [user task: str]
          │
          ▼
    SpecAnalyst (agent)
          │
          ▼
    spec_to_request (custom executor: AgentExecutorResponse → AgentExecutorRequest)
          │
          ▼
    Implementer (agent)  ◄────────── revision_loop ◄──┐
          │                                            │
          ▼                                            │
    fanout_for_review (custom executor)                │
       │       │       │                               │
       ▼       ▼       ▼                               │
    Test    Security  Doc                              │
    Writer  Reviewer  Writer                           │
       │       │       │                               │
       └───────┴───────┴──► aggregator (fan-in)        │
                                  │                    │
                                  ▼                    │
                          switch_case_edge_group       │
                          ┌───────┴───────┐            │
                       APPROVED      CHANGES_REQUESTED │
                          │                 │          │
                          ▼                 └──────────┘
                       finalize (yields output)

Demonstrates:
  • Agents wrapped as AgentExecutors via add_edge with AgentProtocol
  • Custom Executor classes with @handler
  • Fan-out via multiple add_edge calls from one source
  • Fan-in via add_fan_in_edge
  • Switch-case routing (Case / Default)
  • Conditional loop back to a previous executor (revision cycle)
  • Type-safe message dataclasses flowing between executors
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_framework import (
    AgentExecutorRequest,
    AgentExecutorResponse,
    Case,
    Default,
    Executor,
    Message,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from .agents import build_agents

# ---------------------------------------------------------------------------
# Message types flowing through the graph
# ---------------------------------------------------------------------------


@dataclass
class CodePackage:
    """The Implementer's output, fanned out to reviewers."""

    spec: str
    implementation: str
    revision: int = 0


@dataclass
class ReviewBundle:
    """One reviewer's contribution flowing into the aggregator."""

    kind: str  # "tests" | "security" | "docs"
    content: str
    spec: str
    implementation: str
    revision: int


@dataclass
class AggregatedReview:
    """Output of the fan-in step: everything the switch-case needs."""

    spec: str
    implementation: str
    tests: str
    docs: str
    security_report: str
    verdict: str  # "APPROVED" | "CHANGES_REQUESTED"
    revision: int


@dataclass
class FinalArtifact:
    """The terminal output of the workflow."""

    spec: str
    implementation: str
    tests: str
    docs: str
    security_report: str
    revisions: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(resp: AgentExecutorResponse) -> str:
    """Pull the assistant text out of an AgentExecutorResponse."""
    return resp.agent_response.text or ""


def _strip_code_fence(text: str) -> str:
    """Return the inside of the first ```...``` block, or text as-is."""
    m = re.search(r"```(?:python|md|markdown)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _parse_verdict(text: str) -> str:
    """Find VERDICT: APPROVED|CHANGES_REQUESTED in the security report."""
    m = re.search(
        r"VERDICT\s*:\s*(APPROVED|CHANGES_REQUESTED)", text, re.IGNORECASE
    )
    return m.group(1).upper() if m else "CHANGES_REQUESTED"


def _as_request(text: str) -> AgentExecutorRequest:
    """Wrap a string as a fresh single-turn user request to the next agent."""
    return AgentExecutorRequest(
        messages=[Message(role="user", contents=[text])],
        should_respond=True,
    )


# ---------------------------------------------------------------------------
# Custom executors
# ---------------------------------------------------------------------------


class SpecToRequest(Executor):
    """SpecAnalyst response → Implementer request (revision 0, no feedback)."""

    @handler
    async def run(
        self,
        resp: AgentExecutorResponse,
        ctx: WorkflowContext[AgentExecutorRequest],
    ) -> None:
        spec = _extract_text(resp)
        # Persist the spec on shared state so downstream nodes can read it.
        ctx.set_state("spec", spec)
        ctx.set_state("revision", 0)
        prompt = (
            "Implement the following spec. Return ONLY a fenced python code "
            f"block.\n\n{spec}"
        )
        await ctx.send_message(_as_request(prompt))


class FanoutForReview(Executor):
    """Take the Implementer's code and fan out 3 review tasks in parallel."""

    @handler
    async def run(
        self,
        resp: AgentExecutorResponse,
        ctx: WorkflowContext[AgentExecutorRequest],
    ) -> None:
        impl_text = _extract_text(resp)
        implementation = _strip_code_fence(impl_text)
        spec = ctx.get_state("spec") or ""
        revision = (ctx.get_state("revision")) or 0

        ctx.set_state("implementation", implementation)
        ctx.set_state("revision", revision)

        package = (
            f"# SPEC\n{spec}\n\n# IMPLEMENTATION\n```python\n{implementation}\n```"
        )

        # One outbound message per branch — fan-out via multiple edges.
        await ctx.send_message(
            _as_request(
                "Write pytest tests for the following.\n\n" + package
            ),
            target_id="test_writer",
        )
        await ctx.send_message(
            _as_request(
                "Audit the following for security/robustness issues. "
                "Output the strict VERDICT/FINDINGS/REQUIRED_FIXES format.\n\n"
                + package
            ),
            target_id="security_reviewer",
        )
        await ctx.send_message(
            _as_request(
                "Write a README section for the following.\n\n" + package
            ),
            target_id="doc_writer",
        )


class TaggedTests(Executor):
    """Tag the TestWriter response and forward to aggregator."""

    @handler
    async def run(
        self, resp: AgentExecutorResponse, ctx: WorkflowContext[ReviewBundle]
    ) -> None:
        spec = ctx.get_state("spec") or ""
        impl = ctx.get_state("implementation") or ""
        rev = (ctx.get_state("revision")) or 0
        await ctx.send_message(
            ReviewBundle(
                kind="tests",
                content=_strip_code_fence(_extract_text(resp)),
                spec=spec,
                implementation=impl,
                revision=rev,
            )
        )


class TaggedSecurity(Executor):
    @handler
    async def run(
        self, resp: AgentExecutorResponse, ctx: WorkflowContext[ReviewBundle]
    ) -> None:
        spec = ctx.get_state("spec") or ""
        impl = ctx.get_state("implementation") or ""
        rev = (ctx.get_state("revision")) or 0
        await ctx.send_message(
            ReviewBundle(
                kind="security",
                content=_extract_text(resp),
                spec=spec,
                implementation=impl,
                revision=rev,
            )
        )


class TaggedDocs(Executor):
    @handler
    async def run(
        self, resp: AgentExecutorResponse, ctx: WorkflowContext[ReviewBundle]
    ) -> None:
        spec = ctx.get_state("spec") or ""
        impl = ctx.get_state("implementation") or ""
        rev = (ctx.get_state("revision")) or 0
        await ctx.send_message(
            ReviewBundle(
                kind="docs",
                content=_extract_text(resp),
                spec=spec,
                implementation=impl,
                revision=rev,
            )
        )


class Aggregator(Executor):
    """Fan-in: collect one ReviewBundle of each kind, then emit AggregatedReview."""

    @handler
    async def run(
        self,
        bundles: list[ReviewBundle],
        ctx: WorkflowContext[AggregatedReview],
    ) -> None:
        by_kind = {b.kind: b for b in bundles}
        tests = by_kind.get("tests")
        security = by_kind.get("security")
        docs = by_kind.get("docs")
        if not (tests and security and docs):
            missing = {"tests", "security", "docs"} - by_kind.keys()
            raise RuntimeError(f"Aggregator missing bundles: {missing}")

        verdict = _parse_verdict(security.content)
        await ctx.send_message(
            AggregatedReview(
                spec=security.spec,
                implementation=security.implementation,
                tests=tests.content,
                docs=docs.content,
                security_report=security.content,
                verdict=verdict,
                revision=security.revision,
            )
        )


MAX_REVISIONS = 1  # cap the loop so a stubborn reviewer can't spin forever


class RevisionLoop(Executor):
    """Build a fresh Implementer request that includes review feedback."""

    @handler
    async def run(
        self,
        review: AggregatedReview,
        ctx: WorkflowContext[AgentExecutorRequest],
    ) -> None:
        next_rev = review.revision + 1
        ctx.set_state("revision", next_rev)
        prompt = (
            "Revise the implementation to address the security review.\n\n"
            f"# SPEC\n{review.spec}\n\n"
            f"# CURRENT IMPLEMENTATION\n```python\n{review.implementation}\n```\n\n"
            f"# REVIEW FEEDBACK\n{review.security_report}\n\n"
            "Return ONLY a fenced python code block with the fixed module."
        )
        await ctx.send_message(_as_request(prompt))


class Finalize(Executor):
    """Terminal node — yield the completed FinalArtifact as workflow output."""

    @handler
    async def run(
        self,
        review: AggregatedReview,
        ctx: WorkflowContext[Any, FinalArtifact],
    ) -> None:
        await ctx.yield_output(
            FinalArtifact(
                spec=review.spec,
                implementation=review.implementation,
                tests=review.tests,
                docs=review.docs,
                security_report=review.security_report,
                revisions=review.revision,
            )
        )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_workflow() -> tuple[Workflow, dict[str, "BaseAgent"], dict[str, str]]:
    """Build the workflow.

    Returns ``(workflow, agents, routing)``. The runner needs ``agents`` so
    it can manage the async lifecycle of provider-backed agents (e.g.
    ``ClaudeAgent`` requires explicit ``start()``/``stop()``), and
    ``routing`` so it can print which provider served each role.
    """
    from agent_framework import BaseAgent  # noqa: F401  (for the type hint above)

    agents, routing = build_agents()

    # Custom executors
    spec_to_request = SpecToRequest(id="spec_to_request")
    fanout = FanoutForReview(id="fanout_for_review")
    tagged_tests = TaggedTests(id="tagged_tests")
    tagged_security = TaggedSecurity(id="tagged_security")
    tagged_docs = TaggedDocs(id="tagged_docs")
    aggregator = Aggregator(id="aggregator")
    revision_loop = RevisionLoop(id="revision_loop")
    finalize = Finalize(id="finalize")

    # Wrap agents with stable ids that match the target_ids used in fanout.
    from agent_framework import AgentExecutor

    spec_analyst = AgentExecutor(agents["spec_analyst"], id="spec_analyst")
    implementer = AgentExecutor(agents["implementer"], id="implementer")
    test_writer = AgentExecutor(agents["test_writer"], id="test_writer")
    security_reviewer = AgentExecutor(
        agents["security_reviewer"], id="security_reviewer"
    )
    doc_writer = AgentExecutor(agents["doc_writer"], id="doc_writer")

    builder = WorkflowBuilder(
        name="code-forge",
        description="Multi-agent coding pipeline with fan-out review and revision loop.",
        start_executor=spec_analyst,
    )

    # Spec → Implementer
    builder.add_edge(spec_analyst, spec_to_request)
    builder.add_edge(spec_to_request, implementer)

    # Implementer → fan-out
    builder.add_edge(implementer, fanout)
    builder.add_edge(fanout, test_writer)
    builder.add_edge(fanout, security_reviewer)
    builder.add_edge(fanout, doc_writer)

    # Each reviewer → its tagger → aggregator (fan-in)
    builder.add_edge(test_writer, tagged_tests)
    builder.add_edge(security_reviewer, tagged_security)
    builder.add_edge(doc_writer, tagged_docs)
    builder.add_fan_in_edges(
        [tagged_tests, tagged_security, tagged_docs], aggregator
    )

    # Switch-case on verdict + revision count.
    def _changes_and_under_cap(r: AggregatedReview) -> bool:
        return r.verdict == "CHANGES_REQUESTED" and r.revision < MAX_REVISIONS

    builder.add_switch_case_edge_group(
        aggregator,
        [
            Case(condition=_changes_and_under_cap, target=revision_loop),
            Default(target=finalize),
        ],
    )

    # Loop back: revision_loop → Implementer (creates the cycle)
    builder.add_edge(revision_loop, implementer)

    return builder.build(), agents, routing
