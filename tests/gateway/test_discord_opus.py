"""Tests for Discord Opus codec loading — must use ctypes.util.find_library."""

import ctypes.util
import inspect
from pathlib import Path
from types import SimpleNamespace


class TestOpusFindLibrary:
    """Opus loading must try ctypes.util.find_library first, with platform fallback."""

    def test_uses_find_library_first(self):
        """find_library must be the primary lookup strategy."""
        from gateway.platforms.discord import _find_opus_library_path
        source = inspect.getsource(_find_opus_library_path)
        assert "find_library" in source, \
            "Opus loading must use ctypes.util.find_library"

    def test_homebrew_fallback_is_conditional(self):
        """Homebrew paths must only be tried when find_library returns None."""
        from gateway.platforms.discord import _find_opus_library_path
        source = inspect.getsource(_find_opus_library_path)
        # Homebrew fallback must exist
        assert "/opt/homebrew" in source or "homebrew" in source, \
            "Opus loading should have macOS Homebrew fallback"
        # find_library must appear BEFORE any Homebrew path
        fl_idx = source.index("find_library")
        hb_idx = source.index("/opt/homebrew")
        assert fl_idx < hb_idx, \
            "find_library must be tried before Homebrew fallback paths"
        # Fallback must be guarded by platform check
        assert "sys.platform" in source or "darwin" in source, \
            "Homebrew fallback must be guarded by macOS platform check"

    def test_windows_bundled_discord_dll_is_used_when_find_library_fails(self, monkeypatch, tmp_path):
        """Windows should fall back to discord.py's bundled libopus DLLs."""
        from gateway.platforms import discord as discord_platform

        dll_dir = tmp_path / "discord" / "bin"
        dll_dir.mkdir(parents=True)
        dll_path = dll_dir / "libopus-0.x64.dll"
        dll_path.write_bytes(b"fake")

        fake_discord = SimpleNamespace(__file__=str(tmp_path / "discord" / "__init__.py"))
        monkeypatch.setattr(discord_platform, "discord", fake_discord, raising=False)
        monkeypatch.setattr(discord_platform.sys, "platform", "win32", raising=False)
        monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)

        result = discord_platform._find_opus_library_path()

        assert Path(result) == dll_path

    def test_opus_decode_error_logged(self):
        """Opus decode failure must log the error, not silently return."""
        from gateway.platforms.discord import VoiceReceiver
        source = inspect.getsource(VoiceReceiver._on_packet)
        assert "logger" in source, \
            "_on_packet must log Opus decode errors"
        # Must not have bare `except Exception:\n            return`
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "except Exception" in line and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                assert next_line != "return", \
                    f"_on_packet has bare 'except Exception: return' at line {i+1}"
