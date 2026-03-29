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


def test_tts_tool_prefers_native_opus_output_for_discord_kokoro(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    from tools import tts_tool

    monkeypatch.setattr(tts_tool, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    monkeypatch.setattr(tts_tool, "_get_provider", lambda _cfg: "kokoro")

    def _fake_generate_kokoro(_text, output_path, _cfg):
        assert output_path.endswith(".ogg")
        with open(output_path, "wb") as f:
            f.write(b"ogg")
        return output_path

    monkeypatch.setattr(tts_tool, "_generate_kokoro_tts", _fake_generate_kokoro)

    result = json.loads(tts_tool.text_to_speech_tool("hello kokoro"))

    assert result["success"] is True
    assert result["file_path"].endswith(".ogg")
    assert result["voice_compatible"] is True
    assert result["provider"] == "kokoro"


def test_generate_kokoro_tts_sends_voice_mix_and_speed(monkeypatch, tmp_path):
    from tools import tts_tool

    captured = {}

    class _Response:
        content = b"audio"

        def raise_for_status(self):
            return None

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(tts_tool.requests, "post", _fake_post)

    output_path = tmp_path / "kokoro.ogg"
    tts_tool._generate_kokoro_tts(
        "Hello world",
        str(output_path),
        {
            "kokoro": {
                "base_url": "http://localhost:8880",
                "model": "kokoro",
                "voice": "af_sky+af_v0+af_nicole",
                "speed": 1.75,
                "request_timeout_seconds": 45,
            }
        },
    )

    assert output_path.read_bytes() == b"audio"
    assert captured["url"] == "http://localhost:8880/v1/audio/speech"
    assert captured["timeout"] == 45
    assert captured["json"] == {
        "model": "kokoro",
        "input": "Hello world",
        "voice": "af_sky+af_v0+af_nicole",
        "response_format": "opus",
        "speed": 1.75,
    }


def test_check_tts_requirements_accepts_selected_kokoro_provider(monkeypatch):
    from tools import tts_tool

    def _raise_import_error():
        raise ImportError("not installed")

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "kokoro"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", _raise_import_error)
    monkeypatch.setattr(tts_tool, "_import_elevenlabs", _raise_import_error)
    monkeypatch.setattr(tts_tool, "_import_openai_client", _raise_import_error)
    monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("F5TTS_SECRET_KEY", raising=False)

    assert tts_tool.check_tts_requirements() is True
