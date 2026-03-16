"""Tests for local terminal shell dispatch behavior on Windows/WSL."""

import io
import os
import sys
import types
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

if "firecrawl" not in sys.modules:
    firecrawl_stub = types.ModuleType("firecrawl")
    firecrawl_stub.Firecrawl = object
    sys.modules["firecrawl"] = firecrawl_stub

if "fal_client" not in sys.modules:
    fal_client_stub = types.ModuleType("fal_client")
    sys.modules["fal_client"] = fal_client_stub


class _FakeProc:
    def __init__(self, output: str = "ok\n"):
        self.stdout = io.StringIO(output)
        self.stdin = io.StringIO()
        self.returncode = None
        self._poll_count = 0

    def poll(self):
        self._poll_count += 1
        if self._poll_count == 1:
            return None
        self.returncode = 0
        return 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


def test_windows_cmd_mode_uses_shell_utils_dispatch_without_fence():
    from tools.environments import local as local_mod

    dispatched_commands = []
    popen_args_seen = []

    def _fake_dispatch(command, work_dir):
        dispatched_commands.append(command)
        return (["cmd.exe", "/d", "/s", "/c", command], {"cwd": work_dir, "shell": False}, "cmd")

    def _fake_popen(args, **kwargs):
        popen_args_seen.append((args, kwargs))
        return _FakeProc("hello from cmd\n")

    with (
        patch("tools.environments.local.build_local_subprocess_invocation", side_effect=_fake_dispatch),
        patch("tools.environments.local.subprocess.Popen", side_effect=_fake_popen),
        patch("tools.environments.local.is_interrupted", return_value=False),
    ):
        env = local_mod.LocalEnvironment(cwd="C:\\Users\\btgil", timeout=5, persistent=False)
        result = env.execute("echo hello from cmd")

    assert result["returncode"] == 0
    assert "hello from cmd" in result["output"]
    assert len(dispatched_commands) == 1
    assert "__HERMES_FENCE" not in dispatched_commands[0]
    assert popen_args_seen
    assert popen_args_seen[0][0][0] == "cmd.exe"


def test_wsl_mode_adds_fence_wrapper_before_dispatch():
    from tools.environments import local as local_mod

    dispatched_commands = []

    def _fake_dispatch(command, work_dir):
        dispatched_commands.append(command)
        return (["wsl.exe", "-e", "bash", "-lc", command], {"shell": False}, "wsl")

    with (
        patch("tools.environments.local.build_local_subprocess_invocation", side_effect=_fake_dispatch),
        patch("tools.environments.local.subprocess.Popen", return_value=_FakeProc("hello from wsl\n")),
        patch("tools.environments.local.is_interrupted", return_value=False),
    ):
        env = local_mod.LocalEnvironment(cwd="C:\\Users\\btgil", timeout=5, persistent=False)
        result = env.execute("echo hello from wsl")

    assert result["returncode"] == 0
    assert "hello from wsl" in result["output"]
    assert len(dispatched_commands) == 2
    assert "__HERMES_FENCE" not in dispatched_commands[0]
    assert "__HERMES_FENCE" in dispatched_commands[1]
