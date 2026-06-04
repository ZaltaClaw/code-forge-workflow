---
description: Render the chart with synthetic IDs and show a summary
---

Run `make chart-template` and:

1. Count the rendered Kubernetes resources by `kind`.
2. Highlight any that look suspicious (no labels, missing `securityContext`, hardcoded namespaces).
3. Save the full render to `/tmp/code-forge-render.yaml` for inspection.
