"""Regression tests for sidecar queued-turn sync on slash-command turns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner._browser_bridge_progress = {}
    runner._browser_bridge_tasks = {}
    runner._browser_bridge_pending_interrupts = set()
    runner._running_agents = {}
    runner.session_store = MagicMock()
    runner._extract_browser_bridge_image_attachments = MagicMock(return_value=([], []))
    runner._get_browser_bridge_session_snapshot = MagicMock(
        return_value={"progress": {"running": False}, "messages": []}
    )
    return runner


def _make_source():
    return SessionSource(
        platform=Platform.LOCAL,
        chat_id="browser-bridge:test",
        chat_type="dm",
        user_id="local-user",
        user_name="Chrome Extension",
    )


@pytest.mark.asyncio
async def test_sidecar_slash_turn_persists_transcript_when_handler_does_not():
    runner = _make_runner()
    runner._handle_message = AsyncMock(return_value="🌐 Browser connected to live CDP.")
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key="browser-bridge:test",
        session_id="session-1",
    )
    runner.session_store.load_transcript.side_effect = [[], []]

    await runner._handle_browser_bridge_send(
        payload={"message": "/browser connect ws://localhost:9222"},
        source=_make_source(),
        async_mode=False,
    )

    assert runner.session_store.append_to_transcript.call_count == 2
    user_call = runner.session_store.append_to_transcript.call_args_list[0]
    assistant_call = runner.session_store.append_to_transcript.call_args_list[1]
    assert user_call.args[0] == "session-1"
    assert user_call.args[1]["role"] == "user"
    assert user_call.args[1]["content"].startswith("/browser connect")
    assert assistant_call.args[0] == "session-1"
    assert assistant_call.args[1]["role"] == "assistant"
    assert "connected to live CDP" in assistant_call.args[1]["content"]


@pytest.mark.asyncio
async def test_sidecar_slash_turn_skips_manual_persist_when_handler_already_updated():
    runner = _make_runner()
    runner._handle_message = AsyncMock(return_value="done")
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key="browser-bridge:test",
        session_id="session-2",
    )
    runner.session_store.load_transcript.side_effect = [
        [],
        [{"role": "user", "content": "/status"}],
    ]

    await runner._handle_browser_bridge_send(
        payload={"message": "/status"},
        source=_make_source(),
        async_mode=False,
    )

    runner.session_store.append_to_transcript.assert_not_called()

