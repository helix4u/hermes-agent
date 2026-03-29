import json
from datetime import datetime

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.config import get_env_value


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


def test_browser_bridge_progress_snapshot_includes_activity_log(tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    sk = "test-session-key"
    runner._browser_bridge_progress[sk] = {
        "running": False,
        "detail": "Reply ready.",
        "recent_events": ["[10:00:00] Line one", "[10:00:01] Line two"],
    }
    snap = runner._get_browser_bridge_progress_snapshot(sk)
    assert snap["recent_events"] == ["[10:00:00] Line one", "[10:00:01] Line two"]
    assert snap["activity_log"] == snap["recent_events"]


def test_append_browser_bridge_progress_default_cap(tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    sk = "cap-key"
    last: list = []
    for i in range(200):
        last = runner._append_browser_bridge_progress_event(sk, f"event-{i}")
        runner._set_browser_bridge_progress(sk, running=True, detail=f"e{i}", recent_events=last)
    stored = runner._browser_bridge_progress[sk]["recent_events"]
    assert len(stored) == 128
    assert "event-199" in stored[-1]


@pytest.mark.asyncio
async def test_browser_bridge_inspect_returns_session_log_and_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "session-store"))

    browser_source = runner._build_browser_bridge_source("Chrome Extension", "panel-123")
    browser_entry = runner.session_store.get_or_create_session(browser_source)
    _append_exchange(runner, browser_entry.session_id, "Browser question", "Browser answer")

    session_log_path = tmp_path / "sessions" / f"session_{browser_entry.session_id}.json"
    session_log_path.write_text(
        json.dumps(
            {
                "session_id": browser_entry.session_id,
                "system_prompt": "You are Hermes.",
                "tools": [{"name": "terminal"}],
                "messages": [
                    {"role": "system", "content": "You are Hermes."},
                    {
                        "role": "assistant",
                        "content": "Running terminal tool.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "terminal", "arguments": "{\"cmd\":\"dir\"}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_name": "terminal",
                        "content": "{\"success\": true}",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = await runner._handle_browser_bridge_session(
        {
            "action": "inspect",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
            "sessionKey": browser_entry.session_key,
        }
    )

    assert result["ok"] is True
    assert result["session_id"] == browser_entry.session_id
    assert result["session_log_available"] is True
    assert result["stats"]["tool_call_count"] == 1
    assert result["stats"]["tool_result_count"] == 1
    assert "terminal" in result["stats"]["tool_names"]
    assert result["configured_tools"] == ["terminal"]
    assert result["paths"]["session_log_path"] == str(session_log_path)
    assert result["transcript"][0]["content_text"] == "Browser question"


@pytest.mark.asyncio
async def test_browser_bridge_inspect_allows_non_browser_session(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    cli_entry = runner.session_store.get_or_create_session(SessionSource.local_cli())
    _append_exchange(runner, cli_entry.session_id, "Resume me later", "Here is the saved context")

    result = await runner._handle_browser_bridge_session(
        {
            "action": "inspect",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
            "sessionKey": cli_entry.session_key,
        }
    )

    assert result["ok"] is True
    assert result["can_send"] is False
    assert result["session_log_available"] is False
    assert result["source"] == "local"
    assert result["browser_label"] == "CLI terminal"
    assert [message["content_text"] for message in result["transcript"]] == [
        "Resume me later",
        "Here is the saved context",
    ]


@pytest.mark.asyncio
async def test_browser_bridge_inspect_includes_audit_and_delegate_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    browser_source = runner._build_browser_bridge_source("Chrome Extension", "panel-123")
    browser_entry = runner.session_store.get_or_create_session(browser_source)
    _append_exchange(runner, browser_entry.session_id, "Benchmark this", "Sure.")

    runner.session_store._db.append_event(
        browser_entry.session_id,
        kind="tool_finish",
        phase="tool",
        tool_name="delegate_task",
        status="ok",
        duration_ms=2500,
        title="delegate_task finished",
        preview="delegate_task finished",
        payload={
            "result": json.dumps(
                {
                    "results": [
                        {
                            "task_index": 0,
                            "status": "completed",
                            "summary": "Branch summary",
                            "duration_seconds": 1.2,
                        }
                    ]
                }
            )
        },
    )

    result = await runner._handle_browser_bridge_session(
        {
            "action": "inspect",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
            "sessionKey": browser_entry.session_key,
        }
    )

    assert result["ok"] is True
    assert result["stats"]["audit_event_count"] == 1
    assert result["stats"]["delegate_call_count"] == 1
    assert result["delegate_summary"]["total_branches"] == 1
    assert result["benchmark_summary"]["tool_count"] == 1
    assert result["audit_events"][0]["tool_name"] == "delegate_task"


@pytest.mark.asyncio
async def test_browser_bridge_runtime_config_save_and_recall_search(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    browser_source = runner._build_browser_bridge_source("Chrome Extension", "panel-123")
    browser_entry = runner.session_store.get_or_create_session(browser_source)
    _append_exchange(runner, browser_entry.session_id, "We changed it today", "Done today.")

    config_result = await runner._handle_browser_bridge_session(
        {
            "action": "runtime_config_save",
            "selected_provider": "openrouter",
            "config_patch": {
                "control_room": {"date_awareness_mode": "off"},
                "web": {"archive_fallback": {"enabled": True, "service": "archive.is"}},
            },
            "env_updates": {"OPENROUTER_API_KEY": "test-openrouter-key"},
        }
    )

    assert config_result["ok"] is True
    assert config_result["config"]["control_room"]["date_awareness_mode"] == "off"
    assert config_result["config"]["web"]["archive_fallback"]["enabled"] is True
    assert get_env_value("OPENROUTER_API_KEY") == "test-openrouter-key"

    recall_result = await runner._handle_browser_bridge_session(
        {
            "action": "recall_search",
            "browserLabel": "Chrome Extension",
            "clientSessionId": "panel-123",
            "sessionKey": browser_entry.session_key,
            "query": "today",
            "date_awareness": "off",
        }
    )

    assert recall_result["ok"] is True
    assert recall_result["date_awareness"] == "off"


@pytest.mark.asyncio
async def test_browser_bridge_runtime_config_get_survives_codex_status_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))

    result = await runner._handle_browser_bridge_session(
        {
            "action": "runtime_config_get",
            "selected_provider": "nous",
        }
    )

    provider_status = {
        entry["id"]: entry.get("status", {})
        for entry in result["providers"]
        if isinstance(entry, dict) and entry.get("id")
    }

    assert result["ok"] is True
    assert "openai-codex" in provider_status
