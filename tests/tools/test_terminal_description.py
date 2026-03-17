"""Tests for terminal tool description on Windows local backends."""

import importlib

terminal_tool = importlib.import_module("tools.terminal_tool")


def test_terminal_description_windows_powershell(monkeypatch):
    monkeypatch.setattr(terminal_tool.os, "name", "nt", raising=False)
    monkeypatch.setattr(terminal_tool, "get_local_shell_mode", lambda: "powershell")

    description = terminal_tool._build_terminal_tool_description()

    assert "Windows note" in description
    assert "PowerShell" in description
    assert "C:\\Users\\..." in description
    assert "Execute shell commands in the configured terminal backend." in description
