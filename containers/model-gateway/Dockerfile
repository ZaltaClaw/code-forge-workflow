# =============================================================================
# Model Gateway — LiteLLM proxy with AAD token sidecar for Foundry.
#
# LiteLLM upstream image already has the proxy. We add a small sidecar script
# that refreshes the AAD token via Workload Identity (federated token file)
# and writes it to AZURE_AD_TOKEN every 50 minutes.
# =============================================================================
FROM ghcr.io/berriai/litellm:main-stable AS base

USER root
RUN pip install --no-cache-dir azure-identity msal

COPY refresh-aad-token.py /usr/local/bin/refresh-aad-token.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/refresh-aad-token.py /usr/local/bin/entrypoint.sh

USER 1000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
