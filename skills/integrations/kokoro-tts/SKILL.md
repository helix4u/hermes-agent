---
name: kokoro-tts
description: Use a local Kokoro FastAPI server for text-to-speech, voice discovery, and mixed-voice synthesis. Trigger when working with the user's Kokoro FastAPI Docker service, validating health, listing voices, generating speech with a specific Kokoro voice or voice mix, or using the Kokoro API independently of Hermes' built-in text_to_speech provider.
metadata:
  hermes:
    tags: [TTS, Kokoro, Voice Mixing, FastAPI, Audio]
---

# kokoro-tts

Use this skill when the task is specifically about the user's local Kokoro FastAPI server or when you need to choose voices or mixed voice strings directly instead of relying on Hermes' default `text_to_speech` configuration.

## Core workflow

1. Check the service first with `GET /health`.
2. Read the base URL from `tts.kokoro.base_url` in `~/.hermes/config.yaml` when available. Default to `http://localhost:8880`.
3. List voices with `GET /v1/audio/voices` instead of guessing.
4. Generate audio with `POST /v1/audio/speech`.
5. Use the configured default voice from `tts.kokoro.voice` when the user does not specify one.
6. Use the configured default speed from `tts.kokoro.speed` when the user does not specify one. Hermes defaults this to `1.75`.
7. Save the returned audio bytes to disk and return the file path or `MEDIA:` tag as needed.

## Preferred behavior

- Prefer the built-in Hermes `text_to_speech` tool when the user simply wants speech output and the configured Kokoro default voice is fine.
- Prefer direct Kokoro API use when the user wants to inspect available voices, choose a custom mix, debug the local server, or bypass Hermes' default provider selection.
- Preserve the user's text exactly unless they asked for rewriting.
- Treat the `voice` field as a raw Kokoro voice selector. Mixed voices like `af_sky+af_v0+af_nicole` are valid.
- Weighted voice mixes are also valid, such as `af_bella(2)+af_sky(1)`.

## Voice mixing

- Simple mixes use `+`, for example `af_sky+af_v0+af_nicole`.
- Weighted mixes add ratios in parentheses, for example `af_bella(2)+af_sky(1)`.
- Kokoro normalizes the ratios automatically.
- If the user wants to reuse a blended voice repeatedly, the server also supports voice-combine flows described in `references/api.md`.

## Failure handling

- If `/health` fails, report that the local Kokoro container is unavailable before trying anything else.
- If `GET /v1/audio/voices` fails, report that voice discovery is unavailable and do not guess a replacement voice.
- If synthesis fails, surface the server error and include the requested voice string and response format in the diagnosis.
- If the user requests a format that the target platform does not like, prefer `opus` for Telegram or Discord voice-style playback and `mp3` otherwise.

## Reference file

- Load `references/api.md` when you need exact endpoint shapes or request examples.
