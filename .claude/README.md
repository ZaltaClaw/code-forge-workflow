# .claude — Project-specific Claude Code config

This directory is automatically picked up by Claude Code when running in this
repo. It contains:

```
.claude/
├── README.md          ← you are here
└── commands/          ← project slash commands (auto-discovered as /name)
    ├── render-chart.md
    ├── lint-everything.md
    ├── build-images.md
    ├── security-review.md
    └── onboard-contributor.md
```

Type `/` in a Claude Code session to see the full list, then pick one.

## Why this matters for Code Forge

Most of the value of Claude Code in a complex repo comes from **routine tasks
being one keystroke away**. The commands above codify the chores we do every
day — render, lint, build, security-review, onboarding — so that:

- New contributors don't have to memorize the project's invariants.
- Returning contributors get a consistent loop regardless of who's pairing.
- Claude has a structured place to anchor multi-step work without you having to type the same scaffolding every time.

## How they relate to `CLAUDE.md`

`CLAUDE.md` is **passive context** — it loads on session start and grounds
Claude in what the project is. `.claude/commands/` are **active workflows** —
explicit invocations of repeatable procedures. Together they're the
project's procedural memory.
