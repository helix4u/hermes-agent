"""Tests for gateway /model command provider synchronization."""

import os
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/model", platform=Platform.DISCORD, user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


@pytest.mark.asyncio
async def test_model_command_syncs_provider_env_with_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "model:\n  default: gpt-5.4\n  provider: openai-codex\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_MODEL", "openai/gpt-5.4")
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None: {
            "api_key": "test-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *args, **kwargs: {
            "accepted": True,
            "persist": True,
            "recognized": True,
            "message": None,
        },
    )

    runner = _make_runner()

    result = await runner._handle_model_command(_make_event("/model gpt-5.4"))

    assert "Model changed to `gpt-5.4`" in result
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "openai-codex"


def test_resolve_gateway_model_strips_openai_prefix_for_codex(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: openai/gpt-5.4\n  provider: openai-codex\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)

    assert gateway_run._resolve_gateway_model() == "gpt-5.4"


@pytest.mark.asyncio
async def test_model_command_normalizes_prefixed_codex_slug(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "model:\n  default: gpt-5.4\n  provider: openai-codex\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None: {
            "api_key": "test-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *args, **kwargs: {
            "accepted": True,
            "persist": True,
            "recognized": True,
            "message": None,
        },
    )

    runner = _make_runner()

    result = await runner._handle_model_command(_make_event("/model openai/gpt-5.4"))

    assert "Model changed to `gpt-5.4`" in result
    assert os.getenv("HERMES_MODEL") == "gpt-5.4"
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "openai-codex"
