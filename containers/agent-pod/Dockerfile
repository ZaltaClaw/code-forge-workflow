# =============================================================================
# Code Forge Agent Pod — Claude Code on Microsoft Foundry
#
# Built from the official devcontainer base + Anthropic's Claude Code Dev
# Container Feature. Runs as non-root with a writable /workspace mount.
# Foundry credentials come from Azure Workload Identity (no static keys).
# =============================================================================
FROM mcr.microsoft.com/devcontainers/base:ubuntu-22.04 AS base

ARG TARGETARCH=amd64

# ---- Toolchain ---------------------------------------------------------------
# Install language runtimes that Claude Code typically needs in a generic agent
# container. Trim to what your fleet actually uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq python3 python3-venv python3-pip \
        build-essential ripgrep fd-find tini sudo \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (Claude Code is npm-distributed)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Azure CLI — used by Claude Code for DefaultAzureCredential fallback paths
# and by the agent for `az` workflow tasks.
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
 && rm -rf /var/lib/apt/lists/*

# kubectl — agents in the pool may operate on other namespaces (k8s-as-tools).
RUN curl -sLO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl" \
 && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
 && rm kubectl

# ---- Claude Code (via the official Dev Container Feature install path) -------
# The feature script installs the latest claude CLI globally and wires up shell
# completions. We invoke the same logic the devcontainer Feature uses so the
# install path stays consistent with the documented one.
RUN npm install -g @anthropic-ai/claude-code@latest \
 && npm cache clean --force

# Idle-detect / shutdown helper. Watches for inactivity, flushes the workspace,
# and exits cleanly so the kubelet can recycle the pod.
COPY agent-shutdown /usr/local/bin/agent-shutdown
COPY agent-entrypoint /usr/local/bin/agent-entrypoint
COPY healthz.py /usr/local/bin/healthz.py
RUN chmod +x /usr/local/bin/agent-shutdown /usr/local/bin/agent-entrypoint /usr/local/bin/healthz.py

# ---- User & filesystem -------------------------------------------------------
RUN useradd -m -u 1000 -s /bin/bash agent \
 && mkdir -p /workspace /workspace/.claude \
 && chown -R agent:agent /workspace

USER agent
WORKDIR /workspace

ENV CLAUDE_CONFIG_DIR=/workspace/.claude \
    CLAUDE_CODE_USE_FOUNDRY=1 \
    PATH=/usr/local/bin:/usr/bin:/bin:/home/agent/.local/bin

# Pre-pull the Claude Code config dir so first-run is faster.
RUN claude --version || true

EXPOSE 8081
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-entrypoint"]
