"""Regression coverage for detached gateway cancellation resilience on Windows."""

from __future__ import annotations

import asyncio

import pytest


def test_windows_detached_gateway_sets_detached_env_flag(monkeypatch, tmp_path):
    """Detached gateway launches should mark child env for cancel resilience."""
    import hermes_cli.gateway as gateway_cli
    import gateway.status as gateway_status

    captured_env: dict = {}

    class _DummyProc:
        pid = 12345

        def poll(self):
            return None

    def _fake_popen(*_args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _DummyProc()

    monkeypatch.setattr(gateway_status, "is_gateway_running", lambda: False)
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)
    monkeypatch.setattr(gateway_cli, "get_windows_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(
        gateway_cli,
        "reset_gateway_logs",
        lambda: (tmp_path / "gateway.log", tmp_path / "gateway-error.log"),
    )
    monkeypatch.setattr(gateway_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(gateway_cli.time, "sleep", lambda _secs: None)
    monkeypatch.setattr(gateway_cli, "stream_gateway_startup_logs", lambda: None)

    gateway_cli.windows_start_detached_gateway(stream_startup=False)

    assert captured_env.get("HERMES_GATEWAY_DETACHED") == "1"


@pytest.mark.asyncio
async def test_start_gateway_recovers_cancelled_wait_in_detached_mode(monkeypatch):
    """Detached mode should ignore stray CancelledError while still running."""
    import atexit
    import gateway.run as gateway_run
    import gateway.status as gateway_status

    monkeypatch.setenv("HERMES_GATEWAY_DETACHED", "1")
    monkeypatch.setattr(gateway_run, "_harden_windows_console_logging", lambda: None)
    monkeypatch.setattr(
        gateway_run,
        "_start_cron_ticker",
        lambda stop_event, adapters=None, interval=60: stop_event.wait(0.01),
    )
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)
    monkeypatch.setattr(gateway_status, "write_pid_file", lambda: None)
    monkeypatch.setattr(gateway_status, "remove_pid_file", lambda: None)
    monkeypatch.setattr(atexit, "register", lambda *_args, **_kwargs: None)

    class _FakeRunner:
        instance = None

        def __init__(self, _config):
            type(self).instance = self
            self.should_exit_cleanly = False
            self.exit_reason = None
            self.adapters = {}
            self._running = True
            self._shutdown_event = asyncio.Event()
            self._browser_bridge_tasks = {}
            self.wait_calls = 0
            self.stop_calls = 0

        async def start(self):
            return True

        async def wait_for_shutdown(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise asyncio.CancelledError()
            self._shutdown_event.set()

        async def stop(self):
            self.stop_calls += 1
            self._running = False
            self._shutdown_event.set()

    monkeypatch.setattr(gateway_run, "GatewayRunner", _FakeRunner)

    success = await gateway_run.start_gateway(config=None)

    runner = _FakeRunner.instance
    assert success is True
    assert runner is not None
    assert runner.wait_calls >= 2
    assert runner.stop_calls == 0

