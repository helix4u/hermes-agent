"""Tests for topic-aware gateway progress updates."""

import importlib
import json
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource


class ProgressCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.sent = []
        self.edits = []
        self.typing = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class TransientEditFailureAdapter(ProgressCaptureAdapter):
    def __init__(self):
        super().__init__()
        self._edit_attempts = 0

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self._edit_attempts += 1
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        if self._edit_attempts == 1:
            return SendResult(
                success=False,
                message_id=message_id,
                error=(
                    "503 Service Unavailable (error code: 0): upstream connect error "
                    "or disconnect/reset before headers"
                ),
            )
        return SendResult(success=True, message_id=message_id)


class FakeAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.thinking_callback = kwargs.get("thinking_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.tool_progress_callback("terminal", "pwd")
        time.sleep(0.35)
        self.tool_progress_callback("browser_navigate", "https://example.com")
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DiscordThinkingFakeAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs["tool_progress_callback"]
        self.thinking_callback = kwargs.get("thinking_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.thinking_callback:
            self.thinking_callback("(^_^) planning next step...")
            time.sleep(0.35)
            self.thinking_callback("")
        self.tool_progress_callback(
            "terminal",
            "git status",
            {"command": "git status", "workdir": "c:/Users/btgil/.hermes/hermes-agent"},
        )
        time.sleep(0.35)
        self.tool_progress_callback(
            "_tool_result",
            "terminal",
            {
                "tool": "terminal",
                "duration_seconds": 1.25,
                "is_error": True,
                "status_suffix": "[exit 1]",
                "result": json.dumps(
                    {"status": "error", "exit_code": 1, "stderr": "fatal: not a git repository"}
                ),
            },
        )
        time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter, platform=Platform.TELEGRAM):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    return runner


@pytest.mark.asyncio
async def test_run_agent_progress_stays_in_originating_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    first = adapter.sent[0]
    assert first["chat_id"] == "-1001"
    assert 'terminal: "pwd"' in first["content"]
    assert first["reply_to"] is None
    assert first["metadata"] == {"tool_progress": True, "thread_id": "17585"}
    assert adapter.edits
    assert all(call["metadata"] == {"tool_progress": True, "thread_id": "17585"} for call in adapter.typing)


@pytest.mark.asyncio
async def test_run_agent_progress_keeps_single_message_on_transient_edit_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = TransientEditFailureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    assert len(adapter.edits) >= 2
    assert all(edit["message_id"] == "progress-1" for edit in adapter.edits)


@pytest.mark.asyncio
async def test_discord_progress_includes_thinking_and_error_details(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DiscordThinkingFakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1234",
        chat_type="group",
        thread_id="7777",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:discord:group:1234:7777",
    )

    assert result["final_response"] == "done"
    rendered = [entry["content"] for entry in adapter.sent] + [entry["content"] for entry in adapter.edits]
    assert any("planning next step" in content for content in rendered)
    assert any("terminal finished in 1.25s [exit 1]" in content for content in rendered)
    assert any("status=error" in content for content in rendered)
    assert any("stderr=fatal: not a git repository" in content for content in rendered)


@pytest.mark.asyncio
async def test_gateway_terminal_logs_progress_updates(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DiscordThinkingFakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    console_lines = []
    monkeypatch.setattr(gateway_run, "_write_gateway_console_progress_line", console_lines.append)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1234",
        chat_type="group",
        thread_id="7777",
    )

    caplog.set_level("INFO", logger="gateway.run")

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:discord:group:1234:7777",
    )

    assert result["final_response"] == "done"
    logged = "\n".join(record.getMessage() for record in caplog.records if "gateway-progress" in record.getMessage())
    console_text = "\n".join(console_lines)
    assert "planning next step" in console_text
    assert 'terminal: "git status"' in console_text
    assert "terminal finished in 1.25s [exit 1]" in console_text
    assert "planning next step" in logged
    assert 'terminal: "git status"' in logged
    assert "terminal finished in 1.25s [exit 1]" in logged


@pytest.mark.asyncio
async def test_cron_run_command_starts_interactive_runner(monkeypatch):
    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    runner._run_cron_job_interactive = AsyncMock()
    event = MessageEvent(
        text="/cron run 20e9d96e4430",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="1234",
            chat_type="group",
            thread_id="7777",
        ),
    )
    job = {"id": "20e9d96e4430", "name": "Run heartbeat"}
    created_tasks = []

    def capture_task(coro, *args, **kwargs):
        created_tasks.append(coro)
        coro.close()
        return MagicMock()

    with patch("cron.jobs.get_job", return_value=job), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_cron_command(event)

    assert "▶️ Running cron job `20e9d96e4430`" in result
    assert "I’ll stream progress here and post the result when it finishes." in result
    runner._run_cron_job_interactive.assert_called_once_with(job, event.source)
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_interactive_cron_run_streams_progress_and_completion(monkeypatch):
    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    gateway_run = importlib.import_module("gateway.run")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1234",
        chat_type="group",
        thread_id="7777",
    )
    job = {
        "id": "20e9d96e4430",
        "name": "Run the heartbeat workflow",
        "deliver": "origin",
        "origin": {"platform": "discord", "chat_id": "1234", "thread_id": "7777"},
    }

    def fake_run_job(job_arg, **kwargs):
        assert job_arg is job
        kwargs["thinking_callback"]("planning heartbeat update...")
        time.sleep(0.30)
        kwargs["tool_progress_callback"]("terminal", "python heartbeat.py")
        time.sleep(0.30)
        kwargs["tool_progress_callback"](
            "_tool_result",
            "terminal",
            {
                "tool": "terminal",
                "duration_seconds": 0.42,
                "is_error": False,
                "status_suffix": "[ok]",
                "result": json.dumps({"status": "ok", "details": "heartbeat refreshed"}),
            },
        )
        time.sleep(0.30)
        return True, "# output", "Heartbeat complete.", None

    with patch("cron.jobs.save_job_output", return_value="out.md") as save_output_mock, \
         patch("cron.jobs.mark_job_run") as mark_job_run_mock, \
         patch("cron.scheduler.run_job", side_effect=fake_run_job), \
         patch("cron.scheduler._resolve_delivery_target", return_value=None), \
         patch("cron.scheduler._deliver_result") as deliver_result_mock:
        await runner._run_cron_job_interactive(job, source)

    rendered = [entry["content"] for entry in adapter.sent] + [entry["content"] for entry in adapter.edits]
    assert any("Starting cron job Run the heartbeat workflow" in content for content in rendered)
    assert any("planning heartbeat update" in content for content in rendered)
    assert any('terminal: "python heartbeat.py"' in content for content in rendered)
    assert any("terminal finished in 0.42s [ok]" in content for content in rendered)
    assert any("heartbeat refreshed" in content for content in rendered)
    assert adapter.sent[-1]["content"] == "Heartbeat complete."
    assert adapter.sent[-1]["metadata"] == {"thread_id": "7777"}
    save_output_mock.assert_called_once()
    mark_job_run_mock.assert_called_once_with("20e9d96e4430", True, None)
    deliver_result_mock.assert_not_called()
