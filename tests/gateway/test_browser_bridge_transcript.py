import builtins
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from gateway.run import (
    BrowserBridgeTranscriptUnavailable,
    GatewayRunner,
)


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_fetch_transcript_returns_extension_contract(monkeypatch):
    runner = _make_runner()
    calls = []

    def _fake_fetch(target: str, language: str = ""):
        calls.append((target, language))
        return {
            "video_id": "abc123xyz98",
            "language": "en",
            "segment_count": 2,
            "transcript_text": "hello world",
            "char_count": 11,
        }

    monkeypatch.setattr(runner, "_fetch_bridge_youtube_transcript", _fake_fetch)

    result = await runner._handle_browser_bridge_session(
        {
            "action": "fetch_transcript",
            "url": "https://www.youtube.com/watch?v=abc123xyz98",
            "language": "en",
        }
    )

    assert calls == [("https://www.youtube.com/watch?v=abc123xyz98", "en")]
    assert result == {
        "ok": True,
        "available": True,
        "video_id": "abc123xyz98",
        "language": "en",
        "segment_count": 2,
        "transcript_text": "hello world",
        "char_count": 11,
    }


@pytest.mark.asyncio
async def test_fetch_transcript_returns_unavailable_payload(monkeypatch):
    runner = _make_runner()

    def _fake_fetch(_target: str, language: str = ""):
        raise BrowserBridgeTranscriptUnavailable(
            "Transcript disabled",
            video_id="abc123xyz98",
            requested_language=language,
            available_languages=["en", "es"],
        )

    monkeypatch.setattr(runner, "_fetch_bridge_youtube_transcript", _fake_fetch)

    result = await runner._handle_browser_bridge_session(
        {
            "action": "fetch_transcript",
            "video_id": "abc123xyz98",
            "language": "es",
        }
    )

    assert result == {
        "ok": True,
        "available": False,
        "video_id": "abc123xyz98",
        "language": "es",
        "transcript_text": "",
        "char_count": 0,
        "segment_count": 0,
        "error": "Transcript disabled",
        "available_languages": ["en", "es"],
    }


@pytest.mark.asyncio
async def test_fetch_transcript_requires_url_or_video_id():
    runner = _make_runner()

    with pytest.raises(ValueError, match="requires a YouTube URL or video_id"):
        await runner._handle_browser_bridge_session(
            {
                "action": "fetch_transcript",
                "language": "en",
            }
        )


def test_ensure_gateway_python_package_prefers_uv(monkeypatch):
    real_import = builtins.__import__
    state = {"installed": False}

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "youtube_transcript_api":
            if state["installed"]:
                return object()
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    def _fake_run(cmd, **kwargs):
        assert cmd[:5] == ["uv", "pip", "install", "--python", sys.executable]
        assert cmd[5] == "youtube-transcript-api>=1.2.0"
        state["installed"] = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("builtins.__import__", _fake_import)
    monkeypatch.setattr("gateway.run.shutil.which", lambda name: "uv" if name == "uv" else None)
    monkeypatch.setattr("gateway.run.subprocess.run", _fake_run)

    GatewayRunner._ensure_gateway_python_package(
        import_name="youtube_transcript_api",
        package_spec="youtube-transcript-api>=1.2.0",
    )


def test_ensure_gateway_python_package_bootstraps_pip_when_uv_missing(monkeypatch):
    real_import = builtins.__import__
    state = {"installed": False}
    calls = []

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "youtube_transcript_api":
            if state["installed"]:
                return object()
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    def _fake_run(args, timeout=120):
        calls.append(tuple(args))
        if args == ["-m", "pip", "--version"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="No module named pip")
        if args == ["-m", "ensurepip", "--upgrade"]:
            return subprocess.CompletedProcess(args, 0, stdout="installed", stderr="")
        if args == ["-m", "pip", "install", "--disable-pip-version-check", "youtube-transcript-api>=1.2.0"]:
            state["installed"] = True
            return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
        raise AssertionError(f"Unexpected installer args: {args}")

    monkeypatch.setattr("builtins.__import__", _fake_import)
    monkeypatch.setattr("gateway.run.shutil.which", lambda name: None)
    monkeypatch.setattr(GatewayRunner, "_run_gateway_python_install_command", staticmethod(_fake_run))

    GatewayRunner._ensure_gateway_python_package(
        import_name="youtube_transcript_api",
        package_spec="youtube-transcript-api>=1.2.0",
    )

    assert calls == [
        ("-m", "pip", "--version"),
        ("-m", "ensurepip", "--upgrade"),
        ("-m", "pip", "install", "--disable-pip-version-check", "youtube-transcript-api>=1.2.0"),
    ]
