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

    def _fake_dispatch(command, work_dir, shell_override=None):
        dispatched_commands.append((command, shell_override))
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
    assert "__HERMES_FENCE" not in dispatched_commands[0][0]
    assert dispatched_commands[0][1] is None
    assert popen_args_seen
    assert popen_args_seen[0][0][0] == "cmd.exe"


def test_wsl_mode_adds_fence_wrapper_before_dispatch():
    from tools.environments import local as local_mod

    dispatched_commands = []

    def _fake_dispatch(command, work_dir, shell_override=None):
        dispatched_commands.append((command, shell_override))
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
    assert "__HERMES_FENCE" not in dispatched_commands[0][0]
    assert "__HERMES_FENCE" in dispatched_commands[1][0]


def test_local_execute_passes_shell_override_to_dispatch():
    from tools.environments import local as local_mod

    dispatch_calls = []

    def _fake_dispatch(command, work_dir, shell_override=None):
        dispatch_calls.append((command, shell_override))
        return (["powershell.exe", "-Command", command], {"cwd": work_dir, "shell": False}, "powershell")

    with (
        patch("tools.environments.local.build_local_subprocess_invocation", side_effect=_fake_dispatch),
        patch("tools.environments.local.subprocess.Popen", return_value=_FakeProc("hello from powershell\n")),
        patch("tools.environments.local.is_interrupted", return_value=False),
    ):
        env = local_mod.LocalEnvironment(cwd="C:\\Users\\btgil", timeout=5, persistent=False)
        result = env.execute("Write-Host hi", shell_mode_override="powershell")

    assert result["returncode"] == 0
    assert dispatch_calls
    assert dispatch_calls[0][1] == "powershell"


def test_windows_auto_prefers_cmd_before_powershell_and_wsl():
    from tools.environments import shell_utils

    with (
        patch("tools.environments.shell_utils.is_windows", return_value=True),
        patch("tools.environments.shell_utils.shutil.which", return_value="C:\\Windows\\System32\\cmd.exe"),
        patch("tools.environments.shell_utils._powershell_executable", return_value="C:\\Program Files\\PowerShell\\7\\pwsh.exe"),
        patch("tools.environments.shell_utils._wsl_available", return_value=True),
    ):
        assert shell_utils.get_local_shell_mode(shell_override="auto") == "cmd"
