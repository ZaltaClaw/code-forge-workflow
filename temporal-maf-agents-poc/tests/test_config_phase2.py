from __future__ import annotations

from shared.config import get_settings


def test_phase2_defaults(monkeypatch):
    for var in (
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_DEPLOYMENT",
        "AZURE_OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_ALLOWED_OWNER",
    ):
        monkeypatch.delenv(var, raising=False)
    s = get_settings()
    assert s.azure_openai_endpoint is None
    assert s.azure_openai_deployment is None
    assert s.azure_openai_api_key is None
    assert s.github_token is None
    assert s.github_allowed_owner is None
    assert s.azure_openai_api_version  # has a non-empty default


def test_phase2_reads_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ALLOWED_OWNER", "example-org")
    s = get_settings()
    assert s.azure_openai_endpoint == "https://x.openai.azure.com"
    assert s.azure_openai_deployment == "gpt-4o"
    assert s.azure_openai_api_key == "k"
    assert s.github_token == "t"
    assert s.github_allowed_owner == "example-org"
