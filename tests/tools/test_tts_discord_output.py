import json


def test_tts_tool_prefers_compressed_output_for_discord_f5(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    from tools import tts_tool

    wav_path = tmp_path / "tts.wav"
    ogg_path = tmp_path / "tts.ogg"

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    monkeypatch.setattr(tts_tool, "_get_provider", lambda _cfg: "f5")

    def _fake_generate_f5(_text, output_path, _cfg):
        assert output_path.endswith(".wav")
        wav_path.write_bytes(b"wav")
        return str(wav_path)

    def _fake_convert_to_opus(_path):
        ogg_path.write_bytes(b"ogg")
        return str(ogg_path)

    monkeypatch.setattr(tts_tool, "_generate_f5_tts", _fake_generate_f5)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", _fake_convert_to_opus)

    result = json.loads(tts_tool.text_to_speech_tool("hello world", output_path=str(wav_path)))

    assert result["success"] is True
    assert result["file_path"].endswith(".ogg")
    assert result["voice_compatible"] is True
