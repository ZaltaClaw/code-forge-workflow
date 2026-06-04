---
description: Build all three container images locally
---

Build:

1. `docker build -t code-forge/agent-pod:dev containers/agent-pod`
2. `docker build -t code-forge/session-router:dev containers/session-router`
3. `docker build -t code-forge/model-gateway:dev containers/model-gateway`

Show the size of each resulting image with `docker images | grep code-forge`.

If Docker daemon isn't running, suggest `colima start` or starting Docker Desktop.
