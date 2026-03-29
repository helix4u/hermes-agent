"""Regression tests for Windows agent-browser stream port handling."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path, PosixPath
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

if "firecrawl" not in sys.modules:
    firecrawl_stub = types.ModuleType("firecrawl")
    firecrawl_stub.Firecrawl = object
    sys.modules["firecrawl"] = firecrawl_stub

if "fal_client" not in sys.modules:
    fal_client_stub = types.ModuleType("fal_client")
    sys.modules["fal_client"] = fal_client_stub


def _success_result():
    return subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=0,
        stdout=json.dumps({"success": True, "data": {"url": "https://example.com/"}}),
        stderr="",
    )


def test_windows_sets_stream_port_zero_when_missing():
    from tools import browser_tool

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        return _success_result()

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_sess"}),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-1", "open", ["https://example.com"])

    assert result.get("success") is True
    assert observed_envs
    assert observed_envs[0].get("AGENT_BROWSER_STREAM_PORT") == "0"


def test_windows_retries_bind_10013_with_safe_stream_port():
    from tools import browser_tool

    bind_failure = subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "success": False,
                "error": (
                    "Daemon process exited during startup:\n"
                    "Daemon error: Failed to bind TCP: "
                    "An attempt was made to access a socket in a way forbidden "
                    "by its access permissions. (os error 10013)"
                ),
            }
        ),
        stderr="",
    )
    success = _success_result()

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        if len(observed_envs) == 1:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_retry"}),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
                "AGENT_BROWSER_STREAM_PORT": "9223",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-2", "open", ["https://example.com"])

    assert result.get("success") is True
    assert len(observed_envs) == 2
    assert observed_envs[0].get("AGENT_BROWSER_STREAM_PORT") == "9223"
    assert observed_envs[1].get("AGENT_BROWSER_STREAM_PORT") == "0"


def test_windows_retries_bind_10048_with_safe_stream_port():
    from tools import browser_tool

    bind_failure = subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "success": False,
                "error": (
                    "Daemon process exited during startup:\n"
                    "Daemon error: Failed to bind TCP: "
                    "Only one usage of each socket address "
                    "(protocol/network address/port) is normally permitted. "
                    "(os error 10048)"
                ),
            }
        ),
        stderr="",
    )
    success = _success_result()

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        if len(observed_envs) == 1:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_retry_10048"}),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
                "AGENT_BROWSER_STREAM_PORT": "9223",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-10048", "scroll", ["down"])

    assert result.get("success") is True
    assert len(observed_envs) == 2
    assert observed_envs[0].get("AGENT_BROWSER_STREAM_PORT") == "9223"
    assert observed_envs[1].get("AGENT_BROWSER_STREAM_PORT") == "0"


def test_windows_retries_after_stale_daemon_cleanup_when_bind_persists():
    from tools import browser_tool

    bind_failure = subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "success": False,
                "error": (
                    "Daemon process exited during startup:\n"
                    "Daemon error: Failed to bind TCP: "
                    "An attempt was made to access a socket in a way forbidden "
                    "by its access permissions. (os error 10013)"
                ),
            }
        ),
        stderr="",
    )
    success = _success_result()

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        if len(observed_envs) < 3:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_retry_cleanup"}),
        patch("tools.browser_tool._kill_daemon_pid_from_socket_dir", return_value=True) as kill_pid,
        patch("tools.browser_tool._kill_windows_agent_browser_processes", return_value=1) as kill_taskkill,
        patch("tools.browser_tool._allocate_free_tcp_port", return_value="48231"),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
                "AGENT_BROWSER_STREAM_PORT": "9223",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-3", "open", ["https://example.com"])

    assert result.get("success") is True
    assert len(observed_envs) == 3
    assert observed_envs[0].get("AGENT_BROWSER_STREAM_PORT") == "9223"
    assert observed_envs[1].get("AGENT_BROWSER_STREAM_PORT") == "0"
    assert observed_envs[2].get("AGENT_BROWSER_STREAM_PORT") not in ("", "0", None)
    kill_pid.assert_called_once()
    kill_taskkill.assert_called_once()


def test_windows_retries_cleanup_after_persistent_bind_10048():
    from tools import browser_tool

    bind_failure = subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "success": False,
                "error": (
                    "Daemon process exited during startup:\n"
                    "Daemon error: Failed to bind TCP: "
                    "Only one usage of each socket address "
                    "(protocol/network address/port) is normally permitted. "
                    "(os error 10048)"
                ),
            }
        ),
        stderr="",
    )
    success = _success_result()

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        if len(observed_envs) < 3:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_retry_cleanup_10048"}),
        patch("tools.browser_tool._kill_daemon_pid_from_socket_dir", return_value=True) as kill_pid,
        patch("tools.browser_tool._kill_windows_agent_browser_processes", return_value=1) as kill_taskkill,
        patch("tools.browser_tool._allocate_free_tcp_port", return_value="48232"),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
                "AGENT_BROWSER_STREAM_PORT": "9223",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-10048-cleanup", "scroll", ["down"])

    assert result.get("success") is True
    assert len(observed_envs) == 3
    assert observed_envs[0].get("AGENT_BROWSER_STREAM_PORT") == "9223"
    assert observed_envs[1].get("AGENT_BROWSER_STREAM_PORT") == "0"
    assert observed_envs[2].get("AGENT_BROWSER_STREAM_PORT") not in ("", "0", None)
    kill_pid.assert_called_once()
    kill_taskkill.assert_called_once()


def test_windows_cdp_mode_includes_session_arg_for_stability():
    from tools import browser_tool

    observed_cmds = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_cmds.append(list(cmd_parts))
        return _success_result()

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch(
            "tools.browser_tool._get_session_info",
            return_value={"session_name": "cdp_win_sess", "cdp_url": "ws://localhost:9222"},
        ),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-cdp", "open", ["https://example.com"])

    assert result.get("success") is True
    assert observed_cmds
    cmd = observed_cmds[0]
    assert "--session" in cmd
    assert "cdp_win_sess" in cmd
    assert "--cdp" in cmd
    assert "9222" in cmd


def test_cdp_arg_normalization_localhost_ws_url_to_port():
    from tools import browser_tool

    assert browser_tool._normalize_agent_browser_cdp_arg("ws://localhost:9222") == "9222"
    assert browser_tool._normalize_agent_browser_cdp_arg("ws://127.0.0.1:9222/devtools/browser/abc") == "9222"


def test_cdp_arg_normalization_keeps_non_localhost_url():
    from tools import browser_tool

    remote = "wss://connect.browserbase.com/devtools/browser/abc"
    assert browser_tool._normalize_agent_browser_cdp_arg(remote) == remote


def test_windows_cdp_mode_skips_custom_socket_dir_env():
    from tools import browser_tool

    observed_envs = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_envs.append(dict(env))
        return _success_result()

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch(
            "tools.browser_tool._get_session_info",
            return_value={"session_name": "cdp_win_env", "cdp_url": "ws://localhost:9222"},
        ),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-cdp-env", "open", ["https://example.com"])

    assert result.get("success") is True
    assert observed_envs
    assert "AGENT_BROWSER_SOCKET_DIR" not in observed_envs[0]


def test_windows_cdp_mode_uses_isolated_session_recovery_after_persistent_bind():
    from tools import browser_tool

    bind_failure = subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "success": False,
                "error": (
                    "Daemon process exited during startup:\n"
                    "Daemon error: Failed to bind TCP: "
                    "An attempt was made to access a socket in a way forbidden "
                    "by its access permissions. (os error 10013)"
                ),
            }
        ),
        stderr="",
    )
    success = _success_result()

    observed_envs = []
    observed_cmds = []

    def _fake_run(*, cmd_parts, timeout, env):
        observed_cmds.append(list(cmd_parts))
        observed_envs.append(dict(env))
        if len(observed_envs) < 4:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch(
            "tools.browser_tool._get_session_info",
            return_value={"session_name": "cdp_bind_retry", "cdp_url": "ws://localhost:9222"},
        ),
        patch("tools.browser_tool._kill_windows_agent_browser_processes", return_value=1) as kill_taskkill,
        patch("tools.browser_tool._allocate_free_tcp_port", side_effect=["48233", "48234", "48235"]),
        patch("tools.browser_tool._run_agent_browser_subprocess", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path", PosixPath),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
            },
            clear=True,
        ),
    ):
        result = browser_tool._run_browser_command("task-cdp-bind", "open", ["https://example.com"])

    assert result.get("success") is True
    assert len(observed_envs) == 4
    assert "AGENT_BROWSER_SOCKET_DIR" not in observed_envs[0]
    assert "AGENT_BROWSER_SOCKET_DIR" not in observed_envs[1]
    assert "AGENT_BROWSER_SOCKET_DIR" not in observed_envs[2]
    assert observed_envs[3].get("AGENT_BROWSER_SOCKET_DIR")
    assert observed_envs[3].get("AGENT_BROWSER_STREAM_PORT") not in ("", "0", None)

    initial_cmd = observed_cmds[0]
    isolated_cmd = observed_cmds[3]
    assert "--session" in initial_cmd
    assert "cdp_bind_retry" in initial_cmd
    assert "--session" in isolated_cmd
    assert any(part.startswith("cdp_bind_retry-recovery-") for part in isolated_cmd)
    kill_taskkill.assert_called_once()


def test_cleanup_browser_handles_keyboard_interrupt_during_close():
    from tools import browser_tool

    with (
        patch.dict(browser_tool._active_sessions, {"task-kbi": {"session_name": "sess_kbi"}}, clear=True),
        patch.dict(browser_tool._session_last_activity, {"task-kbi": 123.0}, clear=True),
        patch("tools.browser_tool._maybe_stop_recording"),
        patch("tools.browser_tool._run_browser_command", side_effect=KeyboardInterrupt),
        patch("tools.browser_tool._is_local_mode", return_value=True),
        patch("tools.browser_tool.os.path.exists", return_value=False),
    ):
        browser_tool.cleanup_browser("task-kbi")

    assert "task-kbi" not in browser_tool._active_sessions


def test_stop_browser_cleanup_thread_handles_join_interrupt():
    from tools import browser_tool

    class _ThreadLike:
        def join(self, timeout=None):
            raise KeyboardInterrupt()

    original = browser_tool._cleanup_thread
    original_running = browser_tool._cleanup_running
    try:
        browser_tool._cleanup_thread = _ThreadLike()
        browser_tool._cleanup_running = True
        browser_tool._stop_browser_cleanup_thread()
        assert browser_tool._cleanup_running is False
    finally:
        browser_tool._cleanup_thread = original
        browser_tool._cleanup_running = original_running


def test_sidecar_blocks_live_actions_without_cdp_connection():
    from tools import browser_tool

    with (
        patch("tools.browser_tool._find_agent_browser") as find_browser,
        patch(
            "tools.browser_tool._get_session_info",
            return_value={"session_name": "sidecar_local", "features": {"local": True}},
        ),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
            },
            clear=False,
        ),
    ):
        result = browser_tool._run_browser_command("sidecar_test", "open", ["https://github.com"])

    assert result.get("success") is False
    assert result.get("requires_live_cdp") is True
    assert "no live CDP browser is connected" in (result.get("error") or "")
    find_browser.assert_not_called()


def test_get_cdp_override_reads_shared_runtime_state(tmp_path):
    from tools import browser_tool

    runtime_dir = tmp_path / ".hermes" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "browser_cdp_state.json").write_text(
        json.dumps({"cdp_url": "ws://localhost:9222"}),
        encoding="utf-8",
    )

    with patch.dict(
        "tools.browser_tool.os.environ",
        {
            "HERMES_HOME": str(tmp_path / ".hermes"),
            "BROWSER_CDP_URL": "",
        },
        clear=False,
    ):
        assert browser_tool._get_cdp_override() == "ws://localhost:9222"


def test_get_cdp_override_prefers_env_over_shared_runtime_state(tmp_path):
    from tools import browser_tool

    runtime_dir = tmp_path / ".hermes" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "browser_cdp_state.json").write_text(
        json.dumps({"cdp_url": "ws://localhost:9333"}),
        encoding="utf-8",
    )

    with patch.dict(
        "tools.browser_tool.os.environ",
        {
            "HERMES_HOME": str(tmp_path / ".hermes"),
            "BROWSER_CDP_URL": "ws://localhost:9222",
        },
        clear=False,
    ):
        assert browser_tool._get_cdp_override() == "ws://localhost:9222"


def test_is_local_mode_does_not_probe_live_cdp_endpoint():
    from tools import browser_tool

    with (
        patch(
            "tools.browser_tool.requests.get",
            side_effect=AssertionError("startup should not probe CDP"),
        ),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "BROWSER_CDP_URL": "ws://localhost:9222",
                "BROWSERBASE_API_KEY": "",
                "BROWSERBASE_PROJECT_ID": "",
            },
            clear=False,
        ),
    ):
        assert browser_tool._is_local_mode() is False


def test_sidecar_can_opt_in_to_headless_live_actions():
    from tools import browser_tool

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch(
            "tools.browser_tool._get_session_info",
            return_value={"session_name": "sidecar_local", "features": {"local": True}},
        ),
        patch("tools.browser_tool._run_agent_browser_subprocess", return_value=_success_result()),
        patch.dict(
            "tools.browser_tool.os.environ",
            {
                "PATH": "C:\\Windows\\System32",
                "HERMES_HOME": "C:\\Users\\btgil\\.hermes",
                "HERMES_SIDECAR_ALLOW_HEADLESS_BROWSER_ACTIONS": "true",
            },
            clear=False,
        ),
    ):
        result = browser_tool._run_browser_command("sidecar_test", "open", ["https://example.com"])

    assert result.get("success") is True
