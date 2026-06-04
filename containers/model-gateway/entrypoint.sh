#!/bin/bash
# Bootstrap: refresh AAD token in background, source env, then exec LiteLLM.
set -euo pipefail

/usr/local/bin/refresh-aad-token.py &
REFRESHER_PID=$!

# Wait for the first token to land.
for i in {1..30}; do
  [[ -s /etc/aad/env ]] && break
  sleep 1
done

# shellcheck disable=SC1091
[[ -f /etc/aad/env ]] && set -a && source /etc/aad/env && set +a

trap 'kill $REFRESHER_PID 2>/dev/null || true' EXIT

# Hand off to LiteLLM — args (--config, --port, --num_workers) come from the Deployment.
exec litellm "$@"
