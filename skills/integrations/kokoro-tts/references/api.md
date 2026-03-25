# Kokoro FastAPI API Notes

These notes match the public `remsky/Kokoro-FastAPI` README and are the preferred quick reference for Hermes-side integrations.

## Base URL

- Default local server: `http://localhost:8880`
- OpenAI-compatible base for SDK clients: `http://localhost:8880/v1`

## Health check

- `GET /health`

Use this before attempting voice discovery or synthesis.

## List voices

- `GET /v1/audio/voices`

Expected shape:

```json
{
  "voices": ["af_bella", "af_sky", "af_nicole"]
}
```

## Generate speech

- `POST /v1/audio/speech`

Example JSON body:

```json
{
  "model": "kokoro",
  "input": "Hello world!",
  "voice": "af_sky+af_v0+af_nicole",
  "response_format": "mp3",
  "speed": 1.75
}
```

Supported response formats in the upstream docs include `mp3`, `wav`, `opus`, and `flac`.

## Voice mixing

- Simple equal-weight mix:

```json
{
  "input": "Hello world!",
  "voice": "af_bella+af_sky",
  "response_format": "mp3"
}
```

- Weighted mix:

```json
{
  "input": "Hello world!",
  "voice": "af_bella(2)+af_sky(1)",
  "response_format": "mp3"
}
```

## Optional voice combine route

- `POST /v1/audio/voices/combine`

This can be used when the user wants the server to save or export a combined voice pack for reuse.
