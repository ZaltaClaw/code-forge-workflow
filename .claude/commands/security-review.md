---
description: Red-team a recent change against the threat model
---

1. Read `docs/SECURITY.md` to load the threat model.
2. Ask the user which file or PR they want reviewed.
3. For each change, walk through:
   - Does this introduce a new trust boundary crossing?
   - Does it widen any NetworkPolicy?
   - Does it add a static credential or persist one to disk?
   - Does it bypass PSA (`runAsNonRoot`, `readOnlyRootFilesystem`, dropped caps)?
   - Does it expand the agent pod's egress reach?
   - Does it leak data across `(dev_id, project_id)` boundaries?
4. Report each finding with severity (LOW / MED / HIGH) and a concrete fix.
