# =============================================================================
# Sandbox Orchestrator — request-per-sandbox front door for Code Forge.
#
# Provisions an agent sandbox per request via the upstream agent-sandbox Python
# SDK (k8s-agent-sandbox), runs the work inside it, tears it down. Sandbox-native
# successor to the Go session-router.
#
# Multi-stage: build a wheelhouse in a fat layer, install into a slim runtime.
# Runs as non-root (UID 1000) with a read-only root FS in-cluster — matches the
# securityContext the Helm chart enforces.
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
# Pre-build wheels so the runtime stage stays free of build toolchains.
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim AS runtime

# OCI labels for provenance.
LABEL org.opencontainers.image.title="code-forge/sandbox-orchestrator" \
      org.opencontainers.image.description="Request-per-sandbox orchestrator built on the agent-sandbox SDK." \
      org.opencontainers.image.source="https://github.com/ZaltaClaw/code-forge-workflow"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps from the prebuilt wheelhouse (no network, no compilers).
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# App code.
COPY sandbox_orchestrator ./sandbox_orchestrator

# Drop to an unprivileged user. The chart additionally enforces
# readOnlyRootFilesystem + drop ALL caps; nothing here writes to disk.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin orchestrator
USER 1000

EXPOSE 8080

# Liveness/readiness target for the kubelet probes is GET /healthz (see chart).
ENTRYPOINT ["python", "-m", "sandbox_orchestrator"]
