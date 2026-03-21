from datetime import datetime

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _append_exchange(runner: GatewayRunner, session_id: str, user_text: str, assistant_text: str) -> None:
    ts = datetime.now().isoformat()
    runner.session_store.append_to_transcript(
        session_id,
        {"role": "user", "content": user_text, "timestamp": ts},
    )
    runner.session_store.append_to_transcript(
        session_id,
        {"role": "assistant", "content": assistant_text, "timestamp": ts},
    )


@pytest.mark.asyncio
async def test_browser_bridge_list_includes_non_browser_sessions(tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    browser_source = runner._build_browser_bridge_source("Chrome Extension", "panel-123")
    browser_entry = runner.session_store.get_or_create_session(browser_source)
    _append_exchange(runner, browser_entry.session_id, "Browser question", "Browser answer")

    cli_entry = runner.session_store.get_or_create_session(SessionSource.local_cli())
    _append_exchange(runner, cli_entry.session_id, "CLI question", "CLI answer")

    result = await runner._handle_browser_bridge_session(
        {
            "action": "list",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
        }
    )

    sessions_by_id = {session["session_id"]: session for session in result["sessions"]}

    assert result["active_session_key"] == browser_entry.session_key
    assert sessions_by_id[browser_entry.session_id]["can_send"] is True
    assert sessions_by_id[cli_entry.session_id]["can_send"] is False
    assert sessions_by_id[cli_entry.session_id]["browser_label"] == "CLI terminal"


@pytest.mark.asyncio
async def test_browser_bridge_state_returns_read_only_snapshot_for_cli_session(tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    cli_entry = runner.session_store.get_or_create_session(SessionSource.local_cli())
    _append_exchange(runner, cli_entry.session_id, "Resume me later", "Here is the saved context")

    result = await runner._handle_browser_bridge_session(
        {
            "action": "state",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
            "sessionKey": cli_entry.session_key,
        }
    )

    assert result["session_key"] == cli_entry.session_key
    assert result["session_id"] == cli_entry.session_id
    assert result["can_send"] is False
    assert result["browser_label"] == "CLI terminal"
    assert [message["display_content"] for message in result["messages"]] == [
        "Resume me later",
        "Here is the saved context",
    ]


@pytest.mark.asyncio
async def test_browser_bridge_send_rejects_non_browser_session(tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    cli_entry = runner.session_store.get_or_create_session(SessionSource.local_cli())
    _append_exchange(runner, cli_entry.session_id, "Old CLI question", "Old CLI answer")

    with pytest.raises(ValueError, match="read-only in the browser side panel"):
        await runner._handle_browser_bridge_session(
            {
                "action": "send",
                "browserLabel": "Chrome Extension",
                "clientSessionId": "panel-123",
                "sessionKey": cli_entry.session_key,
                "message": "Try sending from the browser anyway",
            }
        )
