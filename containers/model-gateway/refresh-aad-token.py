#!/usr/bin/env python3
"""Refresh the Azure AD token used by LiteLLM to call Foundry.

Reads the federated token from AZURE_FEDERATED_TOKEN_FILE (injected by
azure-workload-identity webhook), exchanges it for a Cognitive Services
access token, and writes it to /etc/aad/token plus the AZURE_AD_TOKEN env file.
"""

from __future__ import annotations
import time
import pathlib
import sys
from azure.identity import DefaultAzureCredential

SCOPE = "https://cognitiveservices.azure.com/.default"
OUT_DIR = pathlib.Path("/etc/aad")
OUT_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = OUT_DIR / "token"
ENV_FILE = OUT_DIR / "env"


def refresh() -> int:
    cred = DefaultAzureCredential()
    tok = cred.get_token(SCOPE)
    TOKEN_FILE.write_text(tok.token)
    ENV_FILE.write_text(f"AZURE_AD_TOKEN={tok.token}\n")
    expires_in = max(60, tok.expires_on - int(time.time()) - 300)  # refresh 5 min early
    print(
        f"[refresh-aad] token len={len(tok.token)} expires_in={expires_in}s", flush=True
    )
    return expires_in


if __name__ == "__main__":
    while True:
        try:
            sleep_for = refresh()
        except Exception as e:
            print(f"[refresh-aad] error: {e}", file=sys.stderr, flush=True)
            sleep_for = 60
        time.sleep(sleep_for)
