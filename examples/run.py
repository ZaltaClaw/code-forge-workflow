"""Run the Code Forge graph workflow on a feature request.

Usage:
    python examples/run.py "build a stable URL slugifier"
    python examples/run.py            # uses a default demo task

Provider selection (Microsoft Agent Framework + Claude Agent SDK):
    CODE_FORGE_PROVIDERS=mixed       # default: Claude reviews, OpenAI everything else
    CODE_FORGE_PROVIDERS=all-openai
    CODE_FORGE_PROVIDERS=all-claude
    CODE_FORGE_PROVIDERS="security_reviewer=claude,implementer=claude"   # per-role
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from code_forge.workflow import FinalArtifact, build_workflow

DEFAULT_TASK = (
    "Build a `slugify(text: str, max_length: int = 80) -> str` function that "
    "produces stable, URL-safe slugs from arbitrary input. It should lowercase, "
    "strip diacritics, replace whitespace and punctuation with single hyphens, "
    "collapse consecutive hyphens, trim leading/trailing hyphens, and respect "
    "max_length on a word boundary when possible. Reject non-string input with "
    "TypeError."
)


@contextlib.asynccontextmanager
async def agent_lifecycle(agents: dict):
    """Stop any agents that need explicit teardown after the workflow ends.

    `ClaudeAgent` lazily initializes its Claude Code CLI subprocess on first
    `run()` call (via `_ensure_session`). Calling `start()` ourselves from a
    different asyncio task than the one the workflow uses to drive the agent
    triggers anyio cancel-scope errors. So we let it self-init, and only
    handle teardown here.
    """
    try:
        yield
    finally:
        for agent in agents.values():
            stop = getattr(agent, "stop", None)
            if callable(stop):
                try:
                    result = stop()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:  # noqa: BLE001
                    print(f"⚠ stop({getattr(agent,'name','?')}) raised: {e}")


async def main() -> None:
    load_dotenv()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK

    out_root = Path("runs") / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root.mkdir(parents=True, exist_ok=True)
    trace = out_root / "trace.txt"

    # Per-agent sandbox dirs live under each run, so every Claude-backed
    # role gets a fresh isolated workspace. The agents.py builder creates
    # one sub-dir per role automatically.
    sandboxes = out_root / "sandboxes"
    sandboxes.mkdir(exist_ok=True)

    workflow, agents, routing = build_workflow(sandbox_root=sandboxes)

    print(f"▶ Task: {task}\n")
    print(f"▶ Output dir: {out_root}")
    print(f"▶ Sandbox root: {sandboxes}")
    foundry = os.getenv("CLAUDE_CODE_USE_FOUNDRY") == "1"
    print(
        "▶ Claude backend: "
        + (f"Microsoft Foundry ({os.getenv('ANTHROPIC_FOUNDRY_RESOURCE','?')})"
           if foundry else "Anthropic public API")
    )
    print("▶ Provider routing:")
    for role, provider in routing.items():
        print(f"    {role:<18s} → {provider}")
    print()

    final: FinalArtifact | None = None

    async with agent_lifecycle(agents):
        stream = await workflow.run(task, stream=True)
        with trace.open("w") as tf:
            async for event in stream:
                kind = type(event).__name__
                exec_id = getattr(event, "executor_id", "")
                # Only treat as error if the attribute is actually set data,
                # not the inherited WorkflowEvent.error class method.
                err_attr = event.__dict__.get("error") or event.__dict__.get("exception")
                if err_attr is not None:
                    line = f"{kind}: {exec_id}  ERROR={err_attr!r}"
                    print(f"!! {line}")
                elif "Failed" in kind or kind.endswith("Error"):
                    line = f"{kind}: {exec_id}  details={vars(event)!r}"
                    print(f"!! {line}")
                else:
                    line = f"{kind}: {exec_id}"
                    print(line)
                tf.write(line + "\n")
                payload = getattr(event, "data", None)
                if isinstance(payload, FinalArtifact):
                    final = payload

        if final is None:
            result = stream.get_final_response()
            for out in getattr(result, "outputs", []) or []:
                if isinstance(out, FinalArtifact):
                    final = out
                    break

    if final is None:
        print("\n⚠ No FinalArtifact emitted. See trace.txt.")
        sys.exit(1)

    (out_root / "spec.md").write_text(final.spec)
    (out_root / "implementation.py").write_text(final.implementation + "\n")
    (out_root / "tests.py").write_text(final.tests + "\n")
    (out_root / "security_report.md").write_text(final.security_report)
    (out_root / "README.md").write_text(final.docs)
    (out_root / "routing.txt").write_text(
        "\n".join(f"{r}={p}" for r, p in routing.items()) + "\n"
    )

    print(
        f"\n✓ Done in {final.revisions + 1} implementation pass(es). "
        f"Artifacts in {out_root}"
    )


if __name__ == "__main__":
    asyncio.run(main())
