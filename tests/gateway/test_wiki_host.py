import asyncio
import socket
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import gateway.run as gateway_run


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _make_runner():
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner._wiki_server = None
    runner._wiki_server_thread = None
    runner._wiki_host_port = None
    runner._wiki_web_root = None
    return runner


def test_wiki_host_lifecycle(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    system_root = hermes_home / "workspace" / "data" / "wiki" / "system"
    system_root.mkdir(parents=True)
    landing = hermes_home / "workspace" / "data" / "knowledge_wiki.html"
    landing.write_text("<html><body>wiki ok</body></html>", encoding="utf-8")
    (system_root / "index.html").write_text(
        '<html><body><a href="../../knowledge_wiki.html">Main Index</a> system ok</body></html>',
        encoding="utf-8",
    )
    (hermes_home / "workspace" / "data" / "secret.txt").write_text("not for LAN", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_resolve_lan_ip", lambda: "127.0.0.1")

    runner = _make_runner()
    port = _free_port()

    status = runner._start_wiki_host(port)
    assert "Wiki hosting is enabled" in status
    assert f"http://127.0.0.1:{port}/knowledge_wiki.html" in status

    with urlopen(f"http://127.0.0.1:{port}/knowledge_wiki.html", timeout=5) as resp:
        body = resp.read().decode("utf-8")
    assert "wiki ok" in body
    with urlopen(f"http://127.0.0.1:{port}/wiki/system/index.html", timeout=5) as resp:
        nested_body = resp.read().decode("utf-8")
    assert "../../knowledge_wiki.html" in nested_body

    try:
        urlopen(f"http://127.0.0.1:{port}/secret.txt", timeout=5)
        assert False, "non-wiki sibling file should not be exposed"
    except HTTPError as exc:
        assert exc.code == 404

    runner._stop_wiki_host()
    assert runner._wiki_server is None
    assert runner._wiki_host_port is None


def test_handle_wiki_host_command_status_enable_disable(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    data_root = hermes_home / "workspace" / "data"
    data_root.mkdir(parents=True)
    (data_root / "knowledge_wiki.html").write_text("<html><body>wiki ok</body></html>", encoding="utf-8")
    wiki_root = data_root / "wiki"
    wiki_root.mkdir()
    (wiki_root / "index.html").write_text("<html><body>wiki index</body></html>", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_resolve_lan_ip", lambda: "127.0.0.1")

    runner = _make_runner()
    port = _free_port()

    event_status = type("Event", (), {"get_command_args": lambda self: "status"})()
    status_reply = asyncio.run(runner._handle_wiki_host_command(event_status))
    assert "currently disabled" in status_reply

    event_enable = type("Event", (), {"get_command_args": lambda self: f"enable {port}"})()
    enable_reply = asyncio.run(runner._handle_wiki_host_command(event_enable))
    assert f"http://127.0.0.1:{port}/knowledge_wiki.html" in enable_reply

    event_disable = type("Event", (), {"get_command_args": lambda self: "disable"})()
    disable_reply = asyncio.run(runner._handle_wiki_host_command(event_disable))
    assert disable_reply == "📚 Wiki hosting disabled."


def test_wiki_host_requires_exclusive_port_ownership(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    data_root = hermes_home / "workspace" / "data"
    data_root.mkdir(parents=True)
    (data_root / "knowledge_wiki.html").write_text("<html><body>wiki ok</body></html>", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_resolve_lan_ip", lambda: "127.0.0.1")

    occupied_port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("0.0.0.0", occupied_port))
    blocker.listen(1)

    runner = _make_runner()
    event_enable = type("Event", (), {"get_command_args": lambda self: f"enable {occupied_port}"})()
    reply = asyncio.run(runner._handle_wiki_host_command(event_enable))

    blocker.close()

    assert "Could not start wiki host" in reply
