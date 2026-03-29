import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.discord import DiscordAdapter


def _make_adapter():
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter._client = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_send_voice_splits_oversized_audio_into_multiple_messages(tmp_path):
    adapter = _make_adapter()
    audio_path = tmp_path / "input.wav"
    audio_path.write_bytes(b"input")
    part1 = tmp_path / "part1.ogg"
    part1.write_bytes(b"one")
    part2 = tmp_path / "part2.ogg"
    part2.write_bytes(b"two")

    channel = SimpleNamespace()
    adapter._client.get_channel.return_value = channel
    adapter._transcode_audio_for_discord = MagicMock(return_value=str(audio_path))
    adapter._split_audio_for_discord = MagicMock(return_value=[str(part1), str(part2)])
    adapter._cleanup_temp_audio_files = MagicMock()
    adapter._send_discord_audio_file = AsyncMock(side_effect=["m1", "m2"])

    result = await DiscordAdapter.send_voice(
        adapter,
        chat_id="123",
        audio_path=str(audio_path),
        caption="Requested audio",
    )

    assert result.success is True
    assert result.message_id == "m2"
    assert adapter._send_discord_audio_file.await_args_list[0].kwargs["caption"] == "Requested audio\nAudio segment 1/2"
    assert adapter._send_discord_audio_file.await_args_list[1].kwargs["caption"] == "Audio segment 2/2"


@pytest.mark.asyncio
async def test_send_voice_returns_error_instead_of_base_path_fallback(tmp_path):
    adapter = _make_adapter()
    audio_path = tmp_path / "input.ogg"
    audio_path.write_bytes(b"input")

    channel = SimpleNamespace()
    adapter._client.get_channel.return_value = channel
    adapter._transcode_audio_for_discord = MagicMock(return_value=str(audio_path))
    adapter._split_audio_for_discord = MagicMock(return_value=[str(audio_path)])
    adapter._cleanup_temp_audio_files = MagicMock()
    adapter._send_discord_audio_file = AsyncMock(side_effect=RuntimeError("send failed"))

    result = await DiscordAdapter.send_voice(
        adapter,
        chat_id="123",
        audio_path=str(audio_path),
    )

    assert result.success is False
    assert "send failed" in str(result.error)
