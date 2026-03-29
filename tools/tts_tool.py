#!/usr/bin/env python3
"""
Text-to-Speech Tool Module

Supports six TTS providers:
- Edge TTS (default, free, no API key): Microsoft Edge neural voices
- ElevenLabs (premium): High-quality voices, needs ELEVENLABS_API_KEY
- OpenAI TTS: Good quality, needs VOICE_TOOLS_OPENAI_KEY or OPENAI_API_KEY
- Kokoro FastAPI (local): OpenAI-compatible local TTS with voice mixing
- NeuTTS (local, free, no API key): On-device TTS via neutts_cli, needs neutts installed
- F5 TTS (local): Fast local voice cloning, needs F5TTS_SECRET_KEY

Output formats:
- Opus (.ogg) for Telegram voice bubbles (requires ffmpeg for Edge TTS)
- MP3 (.mp3) for most providers/platforms
- WAV (.wav) for local F5 output before optional Opus conversion

Configuration is loaded from ~/.hermes/config.yaml under the 'tts:' key.
The user chooses the provider and voice; the model just sends text.

Usage:
    from tools.tts_tool import text_to_speech_tool, check_tts_requirements

    result = text_to_speech_tool(text="Hello world")
"""

import asyncio
import datetime
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from hermes_constants import get_hermes_dir
from typing import Callable, Dict, Any, Optional
from urllib.parse import urljoin

import jwt
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports -- providers are imported only when actually used to avoid
# crashing in headless environments (SSH, Docker, WSL, no PortAudio).
# ---------------------------------------------------------------------------

def _import_edge_tts():
    """Lazy import edge_tts. Returns the module or raises ImportError."""
    import edge_tts
    return edge_tts

def _import_elevenlabs():
    """Lazy import ElevenLabs client. Returns the class or raises ImportError."""
    from elevenlabs.client import ElevenLabs
    return ElevenLabs

def _import_openai_client():
    """Lazy import OpenAI client. Returns the class or raises ImportError."""
    from openai import OpenAI as OpenAIClient
    return OpenAIClient

def _import_sounddevice():
    """Lazy import sounddevice. Returns the module or raises ImportError/OSError."""
    import sounddevice as sd
    return sd


# ===========================================================================
# Defaults
# ===========================================================================
DEFAULT_PROVIDER = "edge"
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"
DEFAULT_ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam
DEFAULT_ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_ELEVENLABS_STREAMING_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "alloy"
def _get_default_output_dir() -> str:
    return str(get_hermes_dir("cache/audio", "audio_cache"))
DEFAULT_KOKORO_BASE_URL = "http://localhost:8880"
DEFAULT_KOKORO_MODEL = "kokoro"
DEFAULT_KOKORO_VOICE = "af_sky+af_v0+af_nicole"
DEFAULT_KOKORO_SPEED = 1.75
DEFAULT_KOKORO_REQUEST_TIMEOUT = 120
DEFAULT_F5_BASE_URL = "http://localhost:8081"
DEFAULT_F5_TOKEN_TTL_MINUTES = 30
DEFAULT_F5_HEALTH_TIMEOUT = 10
DEFAULT_F5_REQUEST_TIMEOUT = 300
DEFAULT_F5_MAX_TEXT_LENGTH = 1000
MIN_F5_SECRET_KEY_BYTES = 32
DEFAULT_OUTPUT_DIR = _get_default_output_dir()
MAX_TEXT_LENGTH = 4000


# ===========================================================================
# Config loader -- reads tts: section from ~/.hermes/config.yaml
# ===========================================================================
def _load_tts_config() -> Dict[str, Any]:
    """
    Load TTS configuration from ~/.hermes/config.yaml.

    Returns a dict with provider settings. Falls back to defaults
    for any missing fields.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("tts", {})
    except ImportError:
        logger.debug("hermes_cli.config not available, using default TTS config")
        return {}
    except Exception as e:
        logger.warning("Failed to load TTS config: %s", e, exc_info=True)
        return {}


def _get_provider(tts_config: Dict[str, Any]) -> str:
    """Get the configured TTS provider name."""
    return (tts_config.get("provider") or DEFAULT_PROVIDER).lower().strip()


# ===========================================================================
# ffmpeg Opus conversion (Edge TTS MP3 -> OGG Opus for Telegram)
# ===========================================================================
def _has_ffmpeg() -> bool:
    """Check if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def _convert_to_opus(mp3_path: str) -> Optional[str]:
    """
    Convert an MP3 file to OGG Opus format for Telegram voice bubbles.

    Args:
        mp3_path: Path to the input MP3 file.

    Returns:
        Path to the .ogg file, or None if conversion fails.
    """
    if not _has_ffmpeg():
        return None

    ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", mp3_path, "-acodec", "libopus",
             "-ac", "1", "-b:a", "64k", "-vbr", "off", ogg_path, "-y"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg conversion failed with return code %d: %s", 
                          result.returncode, result.stderr.decode('utf-8', errors='ignore')[:200])
            return None
        if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
            return ogg_path
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg OGG conversion timed out after 30s")
    except FileNotFoundError:
        logger.warning("ffmpeg not found in PATH")
    except Exception as e:
        logger.warning("ffmpeg OGG conversion failed: %s", e, exc_info=True)
    return None


def _normalize_base_url(base_url: str, default_base_url: str = DEFAULT_F5_BASE_URL) -> str:
    """Normalize a configured API base URL."""
    value = (base_url or default_base_url).strip()
    return value.rstrip("/")


def _response_format_for_output(output_path: str) -> str:
    """Infer the provider response format from the requested output path."""
    suffix = Path(output_path).suffix.lower()
    if suffix == ".ogg":
        return "opus"
    if suffix == ".wav":
        return "wav"
    if suffix == ".flac":
        return "flac"
    if suffix == ".m4a":
        return "m4a"
    if suffix == ".pcm":
        return "pcm"
    return "mp3"


def _build_f5_bearer_token(secret_key: str, ttl_minutes: int) -> str:
    """Create a short-lived bearer token for the F5 FastAPI service."""
    now = datetime.datetime.utcnow()
    payload = {
        "sub": "hermes-agent",
        "iat": now,
        "exp": now + datetime.timedelta(minutes=max(1, ttl_minutes)),
        "scope": "tts",
    }
    token = jwt.encode(payload, secret_key, algorithm="HS256")
    if isinstance(token, bytes):
        return token.decode("utf-8", errors="replace")
    return token


def _split_long_token(token: str, max_length: int) -> list[str]:
    """Split a single oversized token into max-length chunks."""
    return [token[i:i + max_length] for i in range(0, len(token), max_length)]


def _chunk_text_for_f5(text: str, max_length: int = DEFAULT_F5_MAX_TEXT_LENGTH) -> list[str]:
    """
    Split text into sentence-aware chunks that fit the F5 API limit.

    Falls back to whitespace or hard splits when a sentence/token alone exceeds
    the limit. Empty chunks are never returned.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    if len(normalized) <= max_length:
        return [normalized]

    sentence_parts = re.split(r"(?<=[.!?])\s+", normalized)
    chunks: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        value = current.strip()
        if value:
            chunks.append(value)
        current = ""

    for sentence in sentence_parts:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > max_length:
            flush_current()
            words = sentence.split(" ")
            word_chunk = ""
            for word in words:
                if not word:
                    continue
                if len(word) > max_length:
                    if word_chunk:
                        chunks.append(word_chunk)
                        word_chunk = ""
                    chunks.extend(_split_long_token(word, max_length))
                    continue

                candidate = f"{word_chunk} {word}".strip()
                if len(candidate) <= max_length:
                    word_chunk = candidate
                else:
                    if word_chunk:
                        chunks.append(word_chunk)
                    word_chunk = word
            if word_chunk:
                chunks.append(word_chunk)
            continue

        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_length:
            current = candidate
        else:
            flush_current()
            current = sentence

    flush_current()
    return chunks


def _concatenate_wav_files(input_paths: list[str], output_path: str) -> str:
    """Concatenate PCM WAV files with matching parameters into one WAV file."""
    if not input_paths:
        raise ValueError("No WAV inputs were provided for concatenation")

    reference_params = None
    frames: list[bytes] = []

    for path in input_paths:
        with wave.open(path, "rb") as wav_file:
            params = wav_file.getparams()
            if reference_params is None:
                reference_params = params
            elif (
                params.nchannels != reference_params.nchannels
                or params.sampwidth != reference_params.sampwidth
                or params.framerate != reference_params.framerate
                or params.comptype != reference_params.comptype
            ):
                raise ValueError("F5 chunk WAV parameters did not match for concatenation")
            frames.append(wav_file.readframes(wav_file.getnframes()))

    with wave.open(output_path, "wb") as out_file:
        out_file.setnchannels(reference_params.nchannels)
        out_file.setsampwidth(reference_params.sampwidth)
        out_file.setframerate(reference_params.framerate)
        out_file.setcomptype(reference_params.comptype, reference_params.compname)
        for frame_chunk in frames:
            out_file.writeframes(frame_chunk)

    return output_path


# ===========================================================================
# Provider: Edge TTS (free)
# ===========================================================================
async def _generate_edge_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    Generate audio using Edge TTS.

    Args:
        text: Text to convert.
        output_path: Where to save the MP3 file.
        tts_config: TTS config dict.

    Returns:
        Path to the saved audio file.
    """
    _edge_tts = _import_edge_tts()
    edge_config = tts_config.get("edge", {})
    voice = edge_config.get("voice", DEFAULT_EDGE_VOICE)

    communicate = _edge_tts.Communicate(text, voice)
    await communicate.save(output_path)
    return output_path


# ===========================================================================
# Provider: ElevenLabs (premium)
# ===========================================================================
def _generate_elevenlabs(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    Generate audio using ElevenLabs.

    Args:
        text: Text to convert.
        output_path: Where to save the audio file.
        tts_config: TTS config dict.

    Returns:
        Path to the saved audio file.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY not set. Get one at https://elevenlabs.io/")

    el_config = tts_config.get("elevenlabs", {})
    voice_id = el_config.get("voice_id", DEFAULT_ELEVENLABS_VOICE_ID)
    model_id = el_config.get("model_id", DEFAULT_ELEVENLABS_MODEL_ID)

    # Determine output format based on file extension
    if output_path.endswith(".ogg"):
        output_format = "opus_48000_64"
    else:
        output_format = "mp3_44100_128"

    ElevenLabs = _import_elevenlabs()
    client = ElevenLabs(api_key=api_key)
    audio_generator = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format=output_format,
    )

    # audio_generator yields chunks -- write them all
    with open(output_path, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    return output_path


# ===========================================================================
# Provider: OpenAI TTS
# ===========================================================================
def _get_openai_voice_api_key() -> str:
    """Resolve the OpenAI key used for STT/TTS, preferring the voice-specific override."""
    return os.getenv("VOICE_TOOLS_OPENAI_KEY") or os.getenv("OPENAI_API_KEY") or ""


def _generate_openai_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    Generate audio using OpenAI TTS.

    Args:
        text: Text to convert.
        output_path: Where to save the audio file.
        tts_config: TTS config dict.

    Returns:
        Path to the saved audio file.
    """
    api_key = _get_openai_voice_api_key()
    if not api_key:
        raise ValueError(
            "VOICE_TOOLS_OPENAI_KEY or OPENAI_API_KEY not set. "
            "Get one at https://platform.openai.com/api-keys"
        )

    oai_config = tts_config.get("openai", {})
    model = oai_config.get("model", DEFAULT_OPENAI_MODEL)
    voice = oai_config.get("voice", DEFAULT_OPENAI_VOICE)
    base_url = oai_config.get("base_url", "https://api.openai.com/v1")

    # Determine response format from extension
    response_format = _response_format_for_output(output_path)

    OpenAIClient = _import_openai_client()
    client = OpenAIClient(api_key=api_key, base_url=base_url)
    response = client.audio.speech.create(
        model=model,
        voice=voice,
        input=text,
        response_format=response_format,
    )

    response.stream_to_file(output_path)
    return output_path


def _generate_kokoro_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """Generate audio using a local Kokoro FastAPI server."""
    kokoro_config = tts_config.get("kokoro", {})
    base_url = _normalize_base_url(
        kokoro_config.get("base_url", DEFAULT_KOKORO_BASE_URL),
        DEFAULT_KOKORO_BASE_URL,
    )
    model = str(kokoro_config.get("model", DEFAULT_KOKORO_MODEL)).strip() or DEFAULT_KOKORO_MODEL
    voice = str(kokoro_config.get("voice", DEFAULT_KOKORO_VOICE)).strip() or DEFAULT_KOKORO_VOICE
    speed = float(kokoro_config.get("speed", DEFAULT_KOKORO_SPEED))
    request_timeout = int(
        kokoro_config.get("request_timeout_seconds", DEFAULT_KOKORO_REQUEST_TIMEOUT)
    )

    if not voice:
        raise ValueError(
            "Kokoro TTS provider selected but no voice is configured. "
            "Set tts.kokoro.voice in ~/.hermes/config.yaml."
        )
    if speed <= 0:
        raise ValueError("Kokoro speed must be greater than 0.")

    synth_url = urljoin(f"{base_url}/", "v1/audio/speech")
    response = requests.post(
        synth_url,
        json={
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": _response_format_for_output(output_path),
            "speed": speed,
        },
        timeout=request_timeout,
    )
    response.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(response.content)
    return output_path


def _generate_f5_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    Generate audio using a locally hosted F5 TTS FastAPI service.

    The FastAPI service returns WAV bytes; Hermes saves them directly and
    optionally converts them to OGG Opus for Telegram voice bubbles.
    """
    f5_config = tts_config.get("f5", {})
    base_url = _normalize_base_url(f5_config.get("base_url", DEFAULT_F5_BASE_URL))
    voice_profile = str(f5_config.get("voice_profile", "")).strip()
    token_ttl_minutes = int(f5_config.get("token_ttl_minutes", DEFAULT_F5_TOKEN_TTL_MINUTES))
    request_timeout = int(f5_config.get("request_timeout_seconds", DEFAULT_F5_REQUEST_TIMEOUT))

    if not voice_profile:
        raise ValueError(
            "F5 TTS provider selected but no voice profile is configured. "
            "Set tts.f5.voice_profile in ~/.hermes/config.yaml."
        )

    secret_key = os.getenv("F5TTS_SECRET_KEY", "").strip()
    if not secret_key:
        raise ValueError(
            "F5 TTS provider selected but F5TTS_SECRET_KEY is not set. "
            "Use the same SECRET_KEY as the F5TTS-FASTAPI container."
        )
    secret_key_size = len(secret_key.encode("utf-8"))
    if secret_key_size < MIN_F5_SECRET_KEY_BYTES:
        raise ValueError(
            "F5TTS_SECRET_KEY is too short for HS256 JWT signing "
            f"({secret_key_size} bytes; minimum {MIN_F5_SECRET_KEY_BYTES}). "
            "Set a 32+ byte secret and use the same value as the F5TTS-FASTAPI SECRET_KEY."
        )

    token = _build_f5_bearer_token(secret_key, token_ttl_minutes)
    headers = {"Authorization": f"Bearer {token}"}

    health_url = urljoin(f"{base_url}/", "health")
    voices_url = urljoin(f"{base_url}/", "api/v1/voices/list")
    synth_url = urljoin(f"{base_url}/", "api/v1/tts/synthesize")

    health_response = requests.get(health_url, timeout=DEFAULT_F5_HEALTH_TIMEOUT)
    health_response.raise_for_status()

    voices_response = requests.get(voices_url, headers=headers, timeout=DEFAULT_F5_HEALTH_TIMEOUT)
    voices_response.raise_for_status()
    voices_payload = voices_response.json()
    profiles = voices_payload.get("profiles", [])
    if voice_profile not in profiles:
        available = ", ".join(sorted(str(p) for p in profiles)) or "(none found)"
        raise ValueError(
            f"Configured F5 voice profile '{voice_profile}' was not found. "
            f"Available profiles: {available}"
        )

    text_chunks = _chunk_text_for_f5(text, DEFAULT_F5_MAX_TEXT_LENGTH)
    if not text_chunks:
        raise ValueError("F5 TTS received empty text after normalization")

    if len(text_chunks) > 1:
        logger.info("F5 TTS splitting long input into %d chunks", len(text_chunks))

    chunk_paths: list[str] = []
    try:
        for index, chunk in enumerate(text_chunks, start=1):
            synth_response = requests.post(
                synth_url,
                headers=headers,
                json={"text": chunk, "voice_profile": voice_profile},
                timeout=request_timeout,
            )
            synth_response.raise_for_status()

            if len(text_chunks) == 1:
                with open(output_path, "wb") as f:
                    f.write(synth_response.content)
                return output_path

            with tempfile.NamedTemporaryFile(delete=False, suffix=f".chunk{index:03d}.wav") as temp_file:
                temp_file.write(synth_response.content)
                chunk_paths.append(temp_file.name)

        _concatenate_wav_files(chunk_paths, output_path)
    finally:
        for path in chunk_paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    return output_path


# ===========================================================================
# NeuTTS (local, on-device TTS via neutts_cli)
# ===========================================================================

def _check_neutts_available() -> bool:
    """Check if the neutts engine is importable (installed locally)."""
    try:
        import importlib.util
        return importlib.util.find_spec("neutts") is not None
    except Exception:
        return False


def _default_neutts_ref_audio() -> str:
    """Return path to the bundled default voice reference audio."""
    return str(Path(__file__).parent / "neutts_samples" / "jo.wav")


def _default_neutts_ref_text() -> str:
    """Return path to the bundled default voice reference transcript."""
    return str(Path(__file__).parent / "neutts_samples" / "jo.txt")


def _generate_neutts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """Generate speech using the local NeuTTS engine.

    Runs synthesis in a subprocess via tools/neutts_synth.py to keep the
    ~500MB model in a separate process that exits after synthesis.
    Outputs WAV; the caller handles conversion for Telegram if needed.
    """
    import sys

    neutts_config = tts_config.get("neutts", {})
    ref_audio = neutts_config.get("ref_audio", "") or _default_neutts_ref_audio()
    ref_text = neutts_config.get("ref_text", "") or _default_neutts_ref_text()
    model = neutts_config.get("model", "neuphonic/neutts-air-q4-gguf")
    device = neutts_config.get("device", "cpu")

    # NeuTTS outputs WAV natively — use a .wav path for generation,
    # let the caller convert to the final format afterward.
    wav_path = output_path
    if not output_path.endswith(".wav"):
        wav_path = output_path.rsplit(".", 1)[0] + ".wav"

    synth_script = str(Path(__file__).parent / "neutts_synth.py")
    cmd = [
        sys.executable, synth_script,
        "--text", text,
        "--out", wav_path,
        "--ref-audio", ref_audio,
        "--ref-text", ref_text,
        "--model", model,
        "--device", device,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Filter out the "OK:" line from stderr
        error_lines = [l for l in stderr.splitlines() if not l.startswith("OK:")]
        raise RuntimeError(f"NeuTTS synthesis failed: {chr(10).join(error_lines) or 'unknown error'}")

    # If the caller wanted .mp3 or .ogg, convert from WAV
    if wav_path != output_path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            subprocess.run(conv_cmd, check=True, timeout=30)
            os.remove(wav_path)
        else:
            # No ffmpeg — just rename the WAV to the expected path
            os.rename(wav_path, output_path)

    return output_path


# ===========================================================================
# Main tool function
# ===========================================================================
def text_to_speech_tool(
    text: str,
    output_path: Optional[str] = None,
) -> str:
    """
    Convert text to speech audio.

    Reads provider/voice config from ~/.hermes/config.yaml (tts: section).
    The model sends text; the user configures voice and provider.

    On messaging platforms, the returned MEDIA:<path> tag is intercepted
    by the send pipeline and delivered as a native voice message.
    In CLI mode, the file is saved to ~/voice-memos/.

    Args:
        text: The text to convert to speech.
        output_path: Optional custom save path. Defaults to ~/voice-memos/<timestamp>.mp3

    Returns:
        str: JSON result with success, file_path, and optionally MEDIA tag.
    """
    if not text or not text.strip():
        return json.dumps({"success": False, "error": "Text is required"}, ensure_ascii=False)

    # Truncate very long text with a warning
    if len(text) > MAX_TEXT_LENGTH:
        logger.warning("TTS text too long (%d chars), truncating to %d", len(text), MAX_TEXT_LENGTH)
        text = text[:MAX_TEXT_LENGTH]

    tts_config = _load_tts_config()
    provider = _get_provider(tts_config)

    # Detect platform from gateway env var to choose the best output format.
    # Telegram and Discord both benefit from compressed audio. OpenAI,
    # ElevenLabs, and Kokoro can produce Opus natively; Edge/F5/NeuTTS can
    # be converted with ffmpeg when available.
    platform = os.getenv("HERMES_SESSION_PLATFORM", "").lower()
    want_opus = platform in {"telegram", "discord"}

    # Determine output path
    if output_path:
        file_path = Path(output_path).expanduser()
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(DEFAULT_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Use .ogg for platforms that prefer compressed voice/audio output when
        # providers support native Opus output. Use .wav for local F5 output
        # before optional conversion, otherwise .mp3.
        if want_opus and provider in ("openai", "elevenlabs", "kokoro"):
            file_path = out_dir / f"tts_{timestamp}.ogg"
        elif provider == "f5":
            file_path = out_dir / f"tts_{timestamp}.wav"
        else:
            file_path = out_dir / f"tts_{timestamp}.mp3"

    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_str = str(file_path)

    try:
        # Generate audio with the configured provider
        if provider == "elevenlabs":
            try:
                _import_elevenlabs()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "ElevenLabs provider selected but 'elevenlabs' package not installed. Run: pip install elevenlabs"
                }, ensure_ascii=False)
            logger.info("Generating speech with ElevenLabs...")
            _generate_elevenlabs(text, file_str, tts_config)

        elif provider == "openai":
            try:
                _import_openai_client()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "OpenAI provider selected but 'openai' package not installed."
                }, ensure_ascii=False)
            logger.info("Generating speech with OpenAI TTS...")
            _generate_openai_tts(text, file_str, tts_config)

        elif provider == "kokoro":
            logger.info("Generating speech with Kokoro FastAPI...")
            _generate_kokoro_tts(text, file_str, tts_config)

        elif provider == "neutts":
            if not _check_neutts_available():
                return json.dumps({
                    "success": False,
                    "error": "NeuTTS provider selected but neutts is not installed. "
                             "Run hermes setup and choose NeuTTS, or install espeak-ng and run python -m pip install -U neutts[all]."
                }, ensure_ascii=False)
            logger.info("Generating speech with NeuTTS (local)...")
            _generate_neutts(text, file_str, tts_config)

        elif provider == "f5":
            logger.info("Generating speech with local F5 TTS...")
            _generate_f5_tts(text, file_str, tts_config)

        else:
            # Default: Edge TTS (free), with NeuTTS as local fallback
            edge_available = True
            try:
                _import_edge_tts()
            except ImportError:
                edge_available = False

            if edge_available:
                logger.info("Generating speech with Edge TTS...")
                try:
                    loop = asyncio.get_running_loop()
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(
                            lambda: asyncio.run(_generate_edge_tts(text, file_str, tts_config))
                        ).result(timeout=60)
                except RuntimeError:
                    asyncio.run(_generate_edge_tts(text, file_str, tts_config))
            elif _check_neutts_available():
                logger.info("Edge TTS not available, falling back to NeuTTS (local)...")
                provider = "neutts"
                _generate_neutts(text, file_str, tts_config)
            else:
                return json.dumps({
                    "success": False,
                    "error": "No TTS provider available. Install edge-tts (pip install edge-tts) "
                             "or set up NeuTTS for local synthesis."
                }, ensure_ascii=False)

        # Check the file was actually created
        if not os.path.exists(file_str) or os.path.getsize(file_str) == 0:
            return json.dumps({
                "success": False,
                "error": f"TTS generation produced no output (provider: {provider})"
            }, ensure_ascii=False)

        # Try Opus conversion for Telegram compatibility.
        voice_compatible = False
        if want_opus and provider in ("edge", "neutts", "f5") and not file_str.endswith(".ogg"):
            opus_path = _convert_to_opus(file_str)
            if opus_path:
                file_str = opus_path
                voice_compatible = True
        elif provider in ("elevenlabs", "openai", "kokoro"):
            # These providers can output Opus natively if the path ends in .ogg
            voice_compatible = file_str.endswith(".ogg")

        file_size = os.path.getsize(file_str)
        logger.info("TTS audio saved: %s (%s bytes, provider: %s)", file_str, f"{file_size:,}", provider)

        # Build response with MEDIA tag for platform delivery
        media_tag = f"MEDIA:{file_str}"
        if voice_compatible:
            media_tag = f"[[audio_as_voice]]\n{media_tag}"

        return json.dumps({
            "success": True,
            "file_path": file_str,
            "media_tag": media_tag,
            "provider": provider,
            "voice_compatible": voice_compatible,
        }, ensure_ascii=False)

    except ValueError as e:
        # Configuration errors (missing API keys, etc.)
        error_msg = f"TTS configuration error ({provider}): {e}"
        logger.error("%s", error_msg)
        return json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)
    except FileNotFoundError as e:
        # Missing dependencies or files
        error_msg = f"TTS dependency missing ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)
    except Exception as e:
        # Unexpected errors
        error_msg = f"TTS generation failed ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)


# ===========================================================================
# Requirements check
# ===========================================================================
def check_tts_requirements() -> bool:
    """
    Check if at least one TTS provider is available.

    Edge TTS needs no API key and is the default, so if the package
    is installed, TTS is available.

    Returns:
        bool: True if at least one provider can work.
    """
    try:
        if _get_provider(_load_tts_config()) == "kokoro":
            return True
    except Exception:
        pass
    try:
        _import_edge_tts()
        return True
    except ImportError:
        pass
    try:
        _import_elevenlabs()
        if os.getenv("ELEVENLABS_API_KEY"):
            return True
    except ImportError:
        pass
    if os.getenv("F5TTS_SECRET_KEY"):
        return True
    try:
        _import_openai_client()
        if _get_openai_voice_api_key():
            return True
    except ImportError:
        pass
    if _check_neutts_available():
        return True
    return False


# ===========================================================================
# Streaming TTS: sentence-by-sentence pipeline for ElevenLabs
# ===========================================================================
# Sentence boundary pattern: punctuation followed by space or newline
_SENTENCE_BOUNDARY_RE = re.compile(r'(?<=[.!?])(?:\s|\n)|(?:\n\n)')

# Markdown stripping patterns (same as cli.py _voice_speak_response)
_MD_CODE_BLOCK = re.compile(r'```[\s\S]*?```')
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_MD_URL = re.compile(r'https?://\S+')
_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MD_ITALIC = re.compile(r'\*(.+?)\*')
_MD_INLINE_CODE = re.compile(r'`(.+?)`')
_MD_HEADER = re.compile(r'^#+\s*', flags=re.MULTILINE)
_MD_LIST_ITEM = re.compile(r'^\s*[-*]\s+', flags=re.MULTILINE)
_MD_HR = re.compile(r'---+')
_MD_EXCESS_NL = re.compile(r'\n{3,}')


def _strip_markdown_for_tts(text: str) -> str:
    """Remove markdown formatting that shouldn't be spoken aloud."""
    text = _MD_CODE_BLOCK.sub(' ', text)
    text = _MD_LINK.sub(r'\1', text)
    text = _MD_URL.sub('', text)
    text = _MD_BOLD.sub(r'\1', text)
    text = _MD_ITALIC.sub(r'\1', text)
    text = _MD_INLINE_CODE.sub(r'\1', text)
    text = _MD_HEADER.sub('', text)
    text = _MD_LIST_ITEM.sub('', text)
    text = _MD_HR.sub('', text)
    text = _MD_EXCESS_NL.sub('\n\n', text)
    return text.strip()


def stream_tts_to_speaker(
    text_queue: queue.Queue,
    stop_event: threading.Event,
    tts_done_event: threading.Event,
    display_callback: Optional[Callable[[str], None]] = None,
):
    """Consume text deltas from *text_queue*, buffer them into sentences,
    and stream each sentence through ElevenLabs TTS to the speaker in
    real-time.

    Protocol:
        * The producer puts ``str`` deltas onto *text_queue*.
        * A ``None`` sentinel signals end-of-text (flush remaining buffer).
        * *stop_event* can be set to abort early (e.g. user interrupt).
        * *tts_done_event* is **set** in the ``finally`` block so callers
          waiting on it (continuous voice mode) know playback is finished.
    """
    tts_done_event.clear()

    try:
        # --- TTS client setup (optional -- display_callback works without it) ---
        client = None
        output_stream = None
        voice_id = DEFAULT_ELEVENLABS_VOICE_ID
        model_id = DEFAULT_ELEVENLABS_STREAMING_MODEL_ID

        tts_config = _load_tts_config()
        el_config = tts_config.get("elevenlabs", {})
        voice_id = el_config.get("voice_id", voice_id)
        model_id = el_config.get("streaming_model_id",
                                 el_config.get("model_id", model_id))

        api_key = os.getenv("ELEVENLABS_API_KEY", "")
        if not api_key:
            logger.warning("ELEVENLABS_API_KEY not set; streaming TTS audio disabled")
        else:
            try:
                ElevenLabs = _import_elevenlabs()
                client = ElevenLabs(api_key=api_key)
            except ImportError:
                logger.warning("elevenlabs package not installed; streaming TTS disabled")

            # Open a single sounddevice output stream for the lifetime of
            # this function.  ElevenLabs pcm_24000 produces signed 16-bit
            # little-endian mono PCM at 24 kHz.
            if client is not None:
                try:
                    sd = _import_sounddevice()
                    output_stream = sd.OutputStream(
                        samplerate=24000, channels=1, dtype="int16",
                    )
                    output_stream.start()
                except (ImportError, OSError) as exc:
                    logger.debug("sounddevice not available: %s", exc)
                    output_stream = None
                except Exception as exc:
                    logger.warning("sounddevice OutputStream failed: %s", exc)
                    output_stream = None

        sentence_buf = ""
        min_sentence_len = 20
        long_flush_len = 100
        queue_timeout = 0.5
        _spoken_sentences: list[str] = []  # track spoken sentences to skip duplicates
        # Regex to strip complete <think>...</think> blocks from buffer
        _think_block_re = re.compile(r'<think[\s>].*?</think>', flags=re.DOTALL)

        def _speak_sentence(sentence: str):
            """Display sentence and optionally generate + play audio."""
            if stop_event.is_set():
                return
            cleaned = _strip_markdown_for_tts(sentence).strip()
            if not cleaned:
                return
            # Skip duplicate/near-duplicate sentences (LLM repetition)
            cleaned_lower = cleaned.lower().rstrip(".!,")
            for prev in _spoken_sentences:
                if prev.lower().rstrip(".!,") == cleaned_lower:
                    return
            _spoken_sentences.append(cleaned)
            # Display raw sentence on screen before TTS processing
            if display_callback is not None:
                display_callback(sentence)
            # Skip audio generation if no TTS client available
            if client is None:
                return
            # Truncate very long sentences
            if len(cleaned) > MAX_TEXT_LENGTH:
                cleaned = cleaned[:MAX_TEXT_LENGTH]
            try:
                audio_iter = client.text_to_speech.convert(
                    text=cleaned,
                    voice_id=voice_id,
                    model_id=model_id,
                    output_format="pcm_24000",
                )
                if output_stream is not None:
                    for chunk in audio_iter:
                        if stop_event.is_set():
                            break
                        import numpy as _np
                        audio_array = _np.frombuffer(chunk, dtype=_np.int16)
                        output_stream.write(audio_array.reshape(-1, 1))
                else:
                    # Fallback: write chunks to temp file and play via system player
                    _play_via_tempfile(audio_iter, stop_event)
            except Exception as exc:
                logger.warning("Streaming TTS sentence failed: %s", exc)

        def _play_via_tempfile(audio_iter, stop_evt):
            """Write PCM chunks to a temp WAV file and play it."""
            tmp_path = None
            try:
                import wave
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp_path = tmp.name
                with wave.open(tmp, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(24000)
                    for chunk in audio_iter:
                        if stop_evt.is_set():
                            break
                        wf.writeframes(chunk)
                from tools.voice_mode import play_audio_file
                play_audio_file(tmp_path)
            except Exception as exc:
                logger.warning("Temp-file TTS fallback failed: %s", exc)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        while not stop_event.is_set():
            # Read next delta from queue
            try:
                delta = text_queue.get(timeout=queue_timeout)
            except queue.Empty:
                # Timeout: if we have accumulated a long buffer, flush it
                if len(sentence_buf) > long_flush_len:
                    _speak_sentence(sentence_buf)
                    sentence_buf = ""
                continue

            if delta is None:
                # End-of-text sentinel: strip any remaining think blocks, flush
                sentence_buf = _think_block_re.sub('', sentence_buf)
                if sentence_buf.strip():
                    _speak_sentence(sentence_buf)
                break

            sentence_buf += delta

            # --- Think block filtering ---
            # Strip complete <think>...</think> blocks from buffer.
            # Works correctly even when tags span multiple deltas.
            sentence_buf = _think_block_re.sub('', sentence_buf)

            # If an incomplete <think tag is at the end, wait for more data
            # before extracting sentences (the closing tag may arrive next).
            if '<think' in sentence_buf and '</think>' not in sentence_buf:
                continue

            # Check for sentence boundaries
            while True:
                m = _SENTENCE_BOUNDARY_RE.search(sentence_buf)
                if m is None:
                    break
                end_pos = m.end()
                sentence = sentence_buf[:end_pos]
                sentence_buf = sentence_buf[end_pos:]
                # Merge short fragments into the next sentence
                if len(sentence.strip()) < min_sentence_len:
                    sentence_buf = sentence + sentence_buf
                    break
                _speak_sentence(sentence)

        # Drain any remaining items from the queue
        while True:
            try:
                text_queue.get_nowait()
            except queue.Empty:
                break

        # output_stream is closed in the finally block below

    except Exception as exc:
        logger.warning("Streaming TTS pipeline error: %s", exc)
    finally:
        # Always close the audio output stream to avoid locking the device
        if output_stream is not None:
            try:
                output_stream.stop()
                output_stream.close()
            except Exception:
                pass
        tts_done_event.set()


# ===========================================================================
# Main -- quick diagnostics
# ===========================================================================
if __name__ == "__main__":
    print("🔊 Text-to-Speech Tool Module")
    print("=" * 50)

    def _check(importer, label):
        try:
            importer()
            return True
        except ImportError:
            return False

    print("\nProvider availability:")
    print(f"  Edge TTS:   {'installed' if _check(_import_edge_tts, 'edge') else 'not installed (pip install edge-tts)'}")
    print(f"  ElevenLabs: {'installed' if _check(_import_elevenlabs, 'el') else 'not installed (pip install elevenlabs)'}")
    print(f"    API Key:  {'set' if os.getenv('ELEVENLABS_API_KEY') else 'not set'}")
    print(f"  OpenAI:     {'installed' if _check(_import_openai_client, 'oai') else 'not installed'}")
    print(f"    API Key:  {'set' if _get_openai_voice_api_key() else 'not set (VOICE_TOOLS_OPENAI_KEY or OPENAI_API_KEY)'}")
    print(f"  F5 TTS:     {'set' if os.getenv('F5TTS_SECRET_KEY') else 'not set (F5TTS_SECRET_KEY)'}")
    print(f"  ffmpeg:     {'✅ found' if _has_ffmpeg() else '❌ not found (needed for Telegram Opus)'}")
    print(f"\n  Output dir: {DEFAULT_OUTPUT_DIR}")

    config = _load_tts_config()
    provider = _get_provider(config)
    print(f"  Kokoro:     configured at {config.get('kokoro', {}).get('base_url', DEFAULT_KOKORO_BASE_URL)}")
    print(f"  Configured provider: {provider}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

TTS_SCHEMA = {
    "name": "text_to_speech",
    "description": "Convert text to speech audio. Returns a MEDIA: path that the platform delivers as a voice message. On Telegram it plays as a voice bubble, on Discord/WhatsApp as an audio attachment. In CLI mode, saves to ~/voice-memos/. Voice and provider (Edge, ElevenLabs, OpenAI, Kokoro FastAPI, local F5) are user-configured, not model-selected.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to convert to speech. Keep under 4000 characters."
            },
            "output_path": {
                "type": "string",
                "description": "Optional custom file path to save the audio. Defaults to ~/.hermes/cache/audio/<timestamp>.mp3"
            }
        },
        "required": ["text"]
    }
}

registry.register(
    name="text_to_speech",
    toolset="tts",
    schema=TTS_SCHEMA,
    handler=lambda args, **kw: text_to_speech_tool(
        text=args.get("text", ""),
        output_path=args.get("output_path")),
    check_fn=check_tts_requirements,
    emoji="🔊",
)
