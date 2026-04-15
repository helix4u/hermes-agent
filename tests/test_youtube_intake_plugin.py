from pathlib import Path

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager



def test_project_youtube_intake_plugin_loads(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "true")

    mgr = PluginManager()
    mgr.discover_and_load()

    assert "youtube-intake" in mgr._plugins
    plugin = mgr._plugins["youtube-intake"]
    assert plugin.enabled
    assert "youtube_digest" in mgr._plugin_tool_names



def test_project_youtube_intake_tool_visible_in_definitions(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "true")

    mgr = PluginManager()
    mgr.discover_and_load()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)

    from model_tools import get_tool_definitions

    tools = get_tool_definitions(enabled_toolsets=["youtube_intake"], quiet_mode=True)
    tool_names = [t["function"]["name"] for t in tools]
    assert "youtube_digest" in tool_names
