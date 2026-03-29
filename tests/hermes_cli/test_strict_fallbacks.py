import pytest

from hermes_cli import auth
from hermes_cli import runtime_provider as rp


def test_resolve_provider_auto_raises_when_fallbacks_disabled(monkeypatch):
    monkeypatch.setattr(auth, "_load_auth_store", lambda: {"active_provider": None})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr("hermes_cli.config.fallbacks_enabled", lambda config=None: False)

    with pytest.raises(auth.AuthError, match="Implicit provider fallbacks are disabled"):
        auth.resolve_provider("auto")


def test_resolve_runtime_provider_custom_without_config_raises(monkeypatch):
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "custom")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {})

    with pytest.raises(auth.AuthError, match="Custom endpoint requested but no custom base_url/api_key is configured"):
        rp.resolve_runtime_provider(requested="custom")
