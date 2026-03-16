"""Regression tests for Windows agent-browser stream port handling."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path
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

    def _fake_run(cmd_parts, capture_output, text, timeout, env):
        observed_envs.append(dict(env))
        return _success_result()

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_sess"}),
        patch("tools.browser_tool.subprocess.run", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path.home", return_value=Path("C:/Users/btgil")),
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

    def _fake_run(cmd_parts, capture_output, text, timeout, env):
        observed_envs.append(dict(env))
        if len(observed_envs) == 1:
            return bind_failure
        return success

    with (
        patch("tools.browser_tool._find_agent_browser", return_value=["agent-browser"]),
        patch("tools.browser_tool._get_session_info", return_value={"session_name": "win_retry"}),
        patch("tools.browser_tool.subprocess.run", side_effect=_fake_run),
        patch("tools.browser_tool.os.name", "nt"),
        patch("tools.browser_tool.Path.home", return_value=Path("C:/Users/btgil")),
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
