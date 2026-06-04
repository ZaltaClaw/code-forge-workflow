---
description: Run every linter we have, in parallel where possible
---

Run all of these and report a single PASS/FAIL summary at the end:

- `make chart-lint` (helm)
- `cd containers/session-router && go build ./... && go vet ./...`
- `bicep build infra/main.bicep` (if files exist)
- `hadolint containers/*/Dockerfile` (if hadolint is installed; otherwise note "skipped")

If any FAIL, surface the first error from each.
