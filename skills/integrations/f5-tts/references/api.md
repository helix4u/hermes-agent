# F5 FastAPI API

Use these examples when you need to talk to the local F5TTS-FASTAPI service directly.

Use the existing environment variable first. Do not ask the user for a key if `F5TTS_SECRET_KEY` is already set in Hermes' environment.

## Defaults in this environment

- Base URL: `http://localhost:8081`
- Health endpoint: `GET /health`
- Voices endpoint: `GET /api/v1/voices/list`
- Synthesis endpoint: `POST /api/v1/tts/synthesize`
- Auth: `Authorization: Bearer <jwt>`

## Service limits

- `text`: required, min length 1, max length 1000
- `voice_profile`: required, min length 1
- Successful synthesis returns `audio/wav`

## Token generation in Python

```python
import datetime
import os
import jwt

def build_token(secret_key: str | None = None, ttl_minutes: int = 30) -> str:
    secret_key = secret_key or os.getenv("F5TTS_SECRET_KEY", "")
    if not secret_key:
        raise RuntimeError("F5TTS_SECRET_KEY is not set in the environment")
    now = datetime.datetime.utcnow()
    return jwt.encode(
        {
            "sub": "hermes-agent",
            "iat": now,
            "exp": now + datetime.timedelta(minutes=ttl_minutes),
            "scope": "tts",
        },
        secret_key,
        algorithm="HS256",
    )
```

## Health check

```powershell
curl.exe -s http://localhost:8081/health
```

Expected response:

```json
{"status":"healthy"}
```

## List voices in Python

```python
import requests

base_url = "http://localhost:8081"
token = build_token()

response = requests.get(
    f"{base_url}/api/v1/voices/list",
    headers={"Authorization": f"Bearer {token}"},
    timeout=10,
)
response.raise_for_status()
profiles = response.json()["profiles"]
```

## Synthesize in Python

```python
import requests
from pathlib import Path

base_url = "http://localhost:8081"
voice_profile = "<voice-from-voices-list>"
text = "Hello from the local F5 voice API."
token = build_token()

response = requests.post(
    f"{base_url}/api/v1/tts/synthesize",
    headers={"Authorization": f"Bearer {token}"},
    json={"text": text, "voice_profile": voice_profile},
    timeout=300,
)
response.raise_for_status()

output_path = Path.home() / ".hermes" / "audio_cache" / "f5_direct.wav"
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_bytes(response.content)
```

## Long text guidance

Keep each request at 1000 characters or less.

Suggested workflow:

1. Normalize whitespace.
2. Split on sentence boundaries.
3. Pack sentences into chunks up to 1000 characters.
4. Synthesize each chunk as WAV.
5. Concatenate the WAV files in order.
6. Convert to OGG Opus only after concatenation if Telegram delivery needs it.

## Voice discovery

Do not hardcode a machine-specific voice list into the workflow.

Always call `/api/v1/voices/list` at runtime when you need to:

- show the user available cloned voices
- validate a requested `voice_profile`
- choose a fallback voice after a missing-profile error

Treat the voice inventory as dynamic and local to the user's running F5 service.
