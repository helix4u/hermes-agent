"""Tests for Honcho CLI helpers."""

import sys

from honcho_integration import cli
from honcho_integration.cli import _resolve_api_key


class TestResolveApiKey:
    def test_prefers_host_scoped_key(self):
        cfg = {
            "apiKey": "root-key",
            "hosts": {
                "hermes": {
                    "apiKey": "host-key",
                }
            },
        }
        assert _resolve_api_key(cfg) == "host-key"

    def test_falls_back_to_root_key(self):
        cfg = {
            "apiKey": "root-key",
            "hosts": {"hermes": {}},
        }
        assert _resolve_api_key(cfg) == "root-key"

    def test_falls_back_to_env_key(self, monkeypatch):
        monkeypatch.setenv("HONCHO_API_KEY", "env-key")
        assert _resolve_api_key({}) == "env-key"
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)


class TestInstallHonchoSdk:
    def test_prefers_uv_pip_when_available(self, monkeypatch):
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/bin/uv.exe" if name == "uv" else None)
        monkeypatch.setattr(cli, "_run_install", fake_run)

        ok, err = cli._install_honcho_sdk()

        assert ok is True
        assert err == ""
        assert calls == [[
            "C:/bin/uv.exe",
            "pip",
            "install",
            "--python",
            sys.executable,
            cli.HONCHO_SDK_SPEC,
        ]]

    def test_retries_after_ensurepip_when_pip_main_missing(self, monkeypatch):
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            if cmd[:3] == [sys.executable, "-m", "pip"]:
                if len(calls) == 1:
                    return type("Result", (), {
                        "returncode": 1,
                        "stderr": "No module named pip.__main__",
                        "stdout": "",
                    })()
                return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
            if cmd[:3] == [sys.executable, "-m", "ensurepip"]:
                return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(cli.shutil, "which", lambda _: None)
        monkeypatch.setattr(cli, "_run_install", fake_run)

        ok, err = cli._install_honcho_sdk()

        assert ok is True
        assert err == ""
        assert calls == [
            [sys.executable, "-m", "pip", "install", cli.HONCHO_SDK_SPEC],
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            [sys.executable, "-m", "pip", "install", cli.HONCHO_SDK_SPEC],
        ]

