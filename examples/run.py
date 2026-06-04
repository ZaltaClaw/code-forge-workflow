"""Run the Code Forge graph workflow on a feature request.

Usage:
    python examples/run.py "build a stable URL slugifier"
    python examples/run.py            # uses a default demo task

Outputs:
    runs/<timestamp>/spec.md
    runs/<timestamp>/implementation.py
    runs/<timestamp>/tests.py
    runs/<timestamp>/security_report.md
    runs/<timestamp>/README.md
    runs/<timestamp>/trace.txt
"""

from __future__ import annotations

import asyncio
import datetime as dt
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


async def main() -> None:
    load_dotenv()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK

    out_root = Path("runs") / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root.mkdir(parents=True, exist_ok=True)
    trace = out_root / "trace.txt"

    workflow = build_workflow()

    print(f"▶ Task: {task}\n")
    print(f"▶ Output dir: {out_root}\n")

    final: FinalArtifact | None = None

    stream = await workflow.run(task, stream=True)
    with trace.open("w") as tf:
        async for event in stream:
            line = f"{type(event).__name__}: {getattr(event, 'executor_id', '')}"
            print(line)
            tf.write(line + "\n")
            payload = getattr(event, "data", None)
            if isinstance(payload, FinalArtifact):
                final = payload

    if final is None:
        # Fallback: scan the final result's outputs
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

    print(
        f"\n✓ Done in {final.revisions + 1} implementation pass(es). "
        f"Artifacts in {out_root}"
    )


if __name__ == "__main__":
    asyncio.run(main())
