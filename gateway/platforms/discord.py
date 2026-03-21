from __future__ import annotations

"""
Discord platform adapter.

Uses discord.py library for:
- Receiving messages from servers and DMs
- Sending responses back
- Handling threads and channels
"""

import asyncio
import io
import json
import logging
import math
import mimetypes
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from collections import defaultdict
from pathlib import Path
from contextlib import suppress
from typing import Callable, Dict, List, Optional, Any

logger = logging.getLogger(__name__)

VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}
DISCORD_AUDIO_SAFE_BYTES = 7_500_000

try:
    import discord
    from discord import Message as DiscordMessage, Intents
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    DiscordMessage = Any
    Intents = Any
    commands = None

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
    cache_audio_from_url,
)


def _clean_discord_id(entry: str) -> str:
    """Strip common prefixes from a Discord user ID or username entry.

    Users sometimes paste IDs with prefixes like ``user:123``, ``<@123>``,
    or ``<@!123>`` from Discord's UI or other tools.  This normalises the
    entry to just the bare ID or username.
    """
    entry = entry.strip()
    # Strip Discord mention syntax: <@123> or <@!123>
    if entry.startswith("<@") and entry.endswith(">"):
        entry = entry.lstrip("<@!").rstrip(">")
    # Strip "user:" prefix (seen in some Discord tools / onboarding pastes)
    if entry.lower().startswith("user:"):
        entry = entry[5:]
    return entry.strip()


def check_discord_requirements() -> bool:
    """Check if Discord dependencies are available."""
    return DISCORD_AVAILABLE


def _find_opus_library_path() -> Optional[str]:
    """Locate an Opus shared library path for discord.py voice support."""
    import ctypes.util

    opus_path = ctypes.util.find_library("opus")
    if opus_path:
        return opus_path

    # ctypes.util.find_library fails on macOS with Homebrew-installed libs,
    # so fall back to known Homebrew paths if needed.
    _homebrew_paths = (
        "/opt/homebrew/lib/libopus.dylib",  # Apple Silicon
        "/usr/local/lib/libopus.dylib",     # Intel Mac
    )
    if sys.platform == "darwin":
        for _hp in _homebrew_paths:
            if os.path.isfile(_hp):
                return _hp

    # On Windows, discord.py often bundles libopus alongside the package.
    # Prefer those DLLs before giving up, since find_library() commonly
    # returns None even when the bundled codec is present.
    if sys.platform == "win32" and discord is not None:
        discord_pkg_dir = Path(getattr(discord, "__file__", "")).resolve().parent
        candidates = [
            discord_pkg_dir / "bin" / "libopus-0.x64.dll",
            discord_pkg_dir / "bin" / "libopus-0.x86.dll",
        ]
        try:
            av_libs_dir = discord_pkg_dir.parent / "av.libs"
            if av_libs_dir.is_dir():
                candidates.extend(sorted(av_libs_dir.glob("libopus-*.dll")))
        except Exception:
            pass
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

    return None


class VoiceReceiver:
    """Captures and decodes voice audio from a Discord voice channel.

    Attaches to a VoiceClient's socket listener, decrypts RTP packets
    (NaCl transport + DAVE E2EE), decodes Opus to PCM, and buffers
    per-user audio.  A polling loop detects silence and delivers
    completed utterances via a callback.
    """

    SILENCE_THRESHOLD = 1.5    # seconds of silence → end of utterance
    MIN_SPEECH_DURATION = 0.5  # minimum seconds to process (skip noise)
    SAMPLE_RATE = 48000        # Discord native rate
    CHANNELS = 2               # Discord sends stereo

    def __init__(self, voice_client, allowed_user_ids: set = None):
        self._vc = voice_client
        self._allowed_user_ids = allowed_user_ids or set()
        self._running = False

        # Decryption
        self._secret_key: Optional[bytes] = None
        self._dave_session = None
        self._bot_ssrc: int = 0

        # SSRC -> user_id mapping (populated from SPEAKING events)
        self._ssrc_to_user: Dict[int, int] = {}
        self._lock = threading.Lock()

        # Per-user audio buffers
        self._buffers: Dict[int, bytearray] = defaultdict(bytearray)
        self._last_packet_time: Dict[int, float] = {}

        # Opus decoder per SSRC (each user needs own decoder state)
        self._decoders: Dict[int, object] = {}

        # Pause flag: don't capture while bot is playing TTS
        self._paused = False

        # Debug logging counter (instance-level to avoid cross-instance races)
        self._packet_debug_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start listening for voice packets."""
        conn = self._vc._connection
        self._secret_key = bytes(conn.secret_key)
        self._dave_session = conn.dave_session
        self._bot_ssrc = conn.ssrc

        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        self._running = True
        logger.info("VoiceReceiver started (bot_ssrc=%d)", self._bot_ssrc)

    def stop(self):
        """Stop listening and clean up."""
        self._running = False
        try:
            self._vc._connection.remove_socket_listener(self._on_packet)
        except Exception:
            pass
        with self._lock:
            self._buffers.clear()
            self._last_packet_time.clear()
            self._decoders.clear()
            self._ssrc_to_user.clear()
        logger.info("VoiceReceiver stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ------------------------------------------------------------------
    # SSRC -> user_id mapping via SPEAKING opcode hook
    # ------------------------------------------------------------------

    def map_ssrc(self, ssrc: int, user_id: int):
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def _install_speaking_hook(self, conn):
        """Wrap the voice websocket hook to capture SPEAKING events (op 5).

        VoiceConnectionState stores the hook as ``conn.hook`` (public attr).
        It is passed to DiscordVoiceWebSocket on each (re)connect, so we
        must wrap it on the VoiceConnectionState level AND on the current
        live websocket instance.
        """
        original_hook = conn.hook
        receiver_self = self

        async def wrapped_hook(ws, msg):
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d", {})
                ssrc = data.get("ssrc")
                user_id = data.get("user_id")
                if ssrc and user_id:
                    logger.info("SPEAKING event: ssrc=%d -> user=%s", ssrc, user_id)
                    receiver_self.map_ssrc(int(ssrc), int(user_id))
            if original_hook:
                await original_hook(ws, msg)

        # Set on connection state (for future reconnects)
        conn.hook = wrapped_hook
        # Set on the current live websocket (for immediate effect)
        try:
            from discord.utils import MISSING
            if hasattr(conn, 'ws') and conn.ws is not MISSING:
                conn.ws._hook = wrapped_hook
                logger.info("Speaking hook installed on live websocket")
        except Exception as e:
            logger.warning("Could not install hook on live ws: %s", e)

    # ------------------------------------------------------------------
    # Packet handler (called from SocketReader thread)
    # ------------------------------------------------------------------

    def _on_packet(self, data: bytes):
        if not self._running or self._paused:
            return

        # Log first few raw packets for debugging
        self._packet_debug_count += 1
        if self._packet_debug_count <= 5:
            logger.debug(
                "Raw UDP packet: len=%d, first_bytes=%s",
                len(data), data[:4].hex() if len(data) >= 4 else "short",
            )

        if len(data) < 16:
            return

        # RTP version check: top 2 bits must be 10 (version 2).
        # Lower bits may vary (padding, extension, CSRC count).
        # Payload type (byte 1 lower 7 bits) = 0x78 (120) for voice.
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            if self._packet_debug_count <= 5:
                logger.debug("Skipped non-RTP: byte0=0x%02x byte1=0x%02x", data[0], data[1])
            return

        first_byte = data[0]
        _, _, seq, timestamp, ssrc = struct.unpack_from(">BBHII", data, 0)

        # Skip bot's own audio
        if ssrc == self._bot_ssrc:
            return

        # Calculate dynamic RTP header size (RFC 9335 / rtpsize mode)
        cc = first_byte & 0x0F  # CSRC count
        has_extension = bool(first_byte & 0x10)  # extension bit
        header_size = 12 + (4 * cc) + (4 if has_extension else 0)

        if len(data) < header_size + 4:  # need at least header + nonce
            return

        # Read extension length from preamble (for skipping after decrypt)
        ext_data_len = 0
        if has_extension:
            ext_preamble_offset = 12 + (4 * cc)
            ext_words = struct.unpack_from(">H", data, ext_preamble_offset + 2)[0]
            ext_data_len = ext_words * 4

        if self._packet_debug_count <= 10:
            with self._lock:
                known_user = self._ssrc_to_user.get(ssrc, "unknown")
            logger.debug(
                "RTP packet: ssrc=%d, seq=%d, user=%s, hdr=%d, ext_data=%d",
                ssrc, seq, known_user, header_size, ext_data_len,
            )

        header = bytes(data[:header_size])
        payload_with_nonce = data[header_size:]

        # --- NaCl transport decrypt (aead_xchacha20_poly1305_rtpsize) ---
        if len(payload_with_nonce) < 4:
            return
        nonce = bytearray(24)
        nonce[:4] = payload_with_nonce[-4:]
        encrypted = bytes(payload_with_nonce[:-4])

        try:
            import nacl.secret  # noqa: delayed import – only in voice path
            box = nacl.secret.Aead(self._secret_key)
            decrypted = box.decrypt(encrypted, header, bytes(nonce))
        except Exception as e:
            if self._packet_debug_count <= 10:
                logger.warning("NaCl decrypt failed: %s (hdr=%d, enc=%d)", e, header_size, len(encrypted))
            return

        # Skip encrypted extension data to get the actual opus payload
        if ext_data_len and len(decrypted) > ext_data_len:
            decrypted = decrypted[ext_data_len:]

        # --- DAVE E2EE decrypt ---
        if self._dave_session:
            with self._lock:
                user_id = self._ssrc_to_user.get(ssrc, 0)
            if user_id:
                try:
                    import davey
                    decrypted = self._dave_session.decrypt(
                        user_id, davey.MediaType.audio, decrypted
                    )
                except Exception as e:
                    # Unencrypted passthrough — use NaCl-decrypted data as-is
                    if "Unencrypted" not in str(e):
                        if self._packet_debug_count <= 10:
                            logger.warning("DAVE decrypt failed for ssrc=%d: %s", ssrc, e)
                        return
            # If SSRC unknown (no SPEAKING event yet), skip DAVE and try
            # Opus decode directly — audio may be in passthrough mode.
            # Buffer will get a user_id when SPEAKING event arrives later.

        # --- Opus decode -> PCM ---
        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = discord.opus.Decoder()
            pcm = self._decoders[ssrc].decode(decrypted)
            with self._lock:
                self._buffers[ssrc].extend(pcm)
                self._last_packet_time[ssrc] = time.monotonic()
        except Exception as e:
            logger.debug("Opus decode error for SSRC %s: %s", ssrc, e)
            return

    # ------------------------------------------------------------------
    # Silence detection
    # ------------------------------------------------------------------

    def _infer_user_for_ssrc(self, ssrc: int) -> int:
        """Try to infer user_id for an unmapped SSRC.

        When the bot rejoins a voice channel, Discord may not resend
        SPEAKING events for users already speaking.  If exactly one
        allowed user is in the channel, map the SSRC to them.
        """
        try:
            channel = self._vc.channel
            if not channel:
                return 0
            bot_id = self._vc.user.id if self._vc.user else 0
            allowed = self._allowed_user_ids
            candidates = [
                m.id for m in channel.members
                if m.id != bot_id and (not allowed or str(m.id) in allowed)
            ]
            if len(candidates) == 1:
                uid = candidates[0]
                self._ssrc_to_user[ssrc] = uid
                logger.info("Auto-mapped ssrc=%d -> user=%d (sole allowed member)", ssrc, uid)
                return uid
        except Exception:
            pass
        return 0

    def check_silence(self) -> list:
        """Return list of (user_id, pcm_bytes) for completed utterances."""
        now = time.monotonic()
        completed = []

        with self._lock:
            ssrc_user_map = dict(self._ssrc_to_user)
            ssrc_list = list(self._buffers.keys())

            for ssrc in ssrc_list:
                last_time = self._last_packet_time.get(ssrc, now)
                silence_duration = now - last_time
                buf = self._buffers[ssrc]
                # 48kHz, 16-bit, stereo = 192000 bytes/sec
                buf_duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * 2)

                if silence_duration >= self.SILENCE_THRESHOLD and buf_duration >= self.MIN_SPEECH_DURATION:
                    user_id = ssrc_user_map.get(ssrc, 0)
                    if not user_id:
                        # SSRC not mapped (SPEAKING event missing after bot rejoin).
                        # Infer from allowed users in the voice channel.
                        user_id = self._infer_user_for_ssrc(ssrc)
                    if user_id:
                        completed.append((user_id, bytes(buf)))
                    self._buffers[ssrc] = bytearray()
                    self._last_packet_time.pop(ssrc, None)
                elif silence_duration >= self.SILENCE_THRESHOLD * 2:
                    # Stale buffer with no valid user — discard
                    self._buffers.pop(ssrc, None)
                    self._last_packet_time.pop(ssrc, None)

        return completed

    # ------------------------------------------------------------------
    # PCM -> WAV conversion (for Whisper STT)
    # ------------------------------------------------------------------

    @staticmethod
    def pcm_to_wav(pcm_data: bytes, output_path: str,
                   src_rate: int = 48000, src_channels: int = 2):
        """Convert raw PCM to 16kHz mono WAV via ffmpeg."""
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_data)
            pcm_path = f.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "s16le",
                    "-ar", str(src_rate),
                    "-ac", str(src_channels),
                    "-i", pcm_path,
                    "-ar", "16000",
                    "-ac", "1",
                    output_path,
                ],
                check=True,
                timeout=10,
            )
        finally:
            try:
                os.unlink(pcm_path)
            except OSError:
                pass


class DiscordAdapter(BasePlatformAdapter):
    """
    Discord bot adapter.
    
    Handles:
    - Receiving messages from servers and DMs
    - Sending responses with Discord markdown
    - Thread support
    - Native slash commands (/ask, /reset, /status, /stop)
    - Button-based exec approvals
    - Auto-threading for long conversations
    - Reaction-based feedback
    """
    
    # Keep slightly below Discord's embed description hard limit for safety.
    MAX_EMBED_DESCRIPTION = 4000
    CHAIN_SEND_DELAY_SECONDS = 0.5
    CHAIN_SEND_MAX_RETRIES = 5
    
    # Auto-disconnect from voice channel after this many seconds of inactivity
    VOICE_TIMEOUT = 300

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._client: Optional[commands.Bot] = None
        self._client_task: Optional[asyncio.Task] = None
        self._post_ready_task: Optional[asyncio.Task] = None
        self._post_ready_initialized = False
        self._ready_event = asyncio.Event()
        self._allowed_user_ids: set = set()  # For button approval authorization
        self._listen_view_registered = False
        self._seen_message_ids: Dict[int, float] = {}
        # Voice channel state (per-guild)
        self._voice_clients: Dict[int, Any] = {}  # guild_id -> VoiceClient
        self._voice_text_channels: Dict[int, int] = {}  # guild_id -> text_channel_id
        self._voice_timeout_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> timeout task
        # Phase 2: voice listening
        self._voice_receivers: Dict[int, VoiceReceiver] = {}  # guild_id -> VoiceReceiver
        self._voice_listen_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> listen loop
        self._voice_input_callback: Optional[Callable] = None  # set by run.py
        self._on_voice_disconnect: Optional[Callable] = None  # set by run.py
        # Track threads where the bot has participated so follow-up messages
        # in those threads don't require @mention.  Persisted to disk so the
        # set survives gateway restarts.
        self._bot_participated_threads: set = self._load_participated_threads()
        # Cap to prevent unbounded growth (Discord threads get archived).
        self._MAX_TRACKED_THREADS = 500

    def _flatten_slash_command_names(
        self, commands: list[Any], prefix: str = ""
    ) -> list[str]:
        """Return flattened slash command names, including grouped subcommands."""
        names: list[str] = []
        for command in commands or []:
            name = str(getattr(command, "name", "") or "").strip()
            if not name:
                continue
            full = f"{prefix} {name}".strip() if prefix else name
            children = getattr(command, "commands", None)
            if children:
                names.extend(self._flatten_slash_command_names(list(children), prefix=full))
            else:
                names.append(full)
        return names

    async def _run_post_ready_startup(self, *, members: bool) -> None:
        """Run slow, non-critical startup tasks after gateway readiness."""
        if not self._client:
            return
        logger.info("[%s] Post-ready startup begin", self.name)

        if members:
            try:
                await self._resolve_allowed_usernames()
            except Exception:  # pragma: no cover - defensive logging
                logger.warning(
                    "[%s] Failed to resolve allowed usernames",
                    self.name,
                    exc_info=True,
                )
        elif any(not str(entry).isdigit() for entry in (self._allowed_user_ids or set())):
            logger.warning(
                "[%s] DISCORD_ALLOWED_USERS includes username entries but members intent is disabled; "
                "username-based allowlisting will not be resolved",
                self.name,
            )

        try:
            synced = await self._client.tree.sync()
            logger.info("[%s] Synced %d slash command(s)", self.name, len(synced))
            try:
                command_roots = list(self._client.tree.get_commands())
                flattened = sorted(set(self._flatten_slash_command_names(command_roots)))
                if flattened:
                    print(f"[{self.name}] Synced {len(synced)} slash command(s)", flush=True)
                    print(
                        f"[{self.name}] Commands: " + ", ".join(f"/{name}" for name in flattened),
                        flush=True,
                    )
            except Exception as list_exc:  # pragma: no cover - defensive logging
                logger.debug("[%s] Failed to render slash command list: %s", self.name, list_exc)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning("[%s] Slash command sync failed: %s", self.name, e, exc_info=True)

        if not self._listen_view_registered:
            try:
                self._client.add_view(PersistentListenButtonView(self))
                self._listen_view_registered = True
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning(
                    "[%s] Failed to register persistent listen button: %s",
                    self.name,
                    e,
                    exc_info=True,
                )
        logger.info("[%s] Post-ready startup complete", self.name)

    async def connect(self) -> bool:
        """Connect to Discord and start receiving events."""
        if not DISCORD_AVAILABLE:
            logger.error("[%s] discord.py not installed. Run: pip install discord.py", self.name)
            return False

        # Load opus codec for voice channel support
        if not discord.opus.is_loaded():
            opus_path = _find_opus_library_path()
            if opus_path:
                try:
                    discord.opus.load_opus(opus_path)
                except Exception:
                    logger.warning("Opus codec found at %s but failed to load", opus_path)
            if not discord.opus.is_loaded():
                logger.warning("Opus codec not found — voice channel playback disabled")
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False

        # Parse allowed user entries (may contain usernames or IDs)
        allowed_env = os.getenv("DISCORD_ALLOWED_USERS", "")
        if allowed_env:
            self._allowed_user_ids = {
                _clean_discord_id(uid) for uid in allowed_env.split(",")
                if uid.strip()
            }

        # Username resolution requires guild member listing (privileged members intent).
        needs_members_intent = any(
            not str(entry).isdigit() for entry in (self._allowed_user_ids or set())
        )

        async def _attempt_connect(*, message_content: bool, members: bool) -> tuple[bool, Optional[Exception]]:
            """Try one connect profile and return (success, startup_exception)."""
            self._ready_event.clear()
            self._post_ready_initialized = False
            self._client_task = None

            intents = Intents.default()
            intents.message_content = message_content
            intents.dm_messages = True
            intents.guild_messages = True
            intents.members = members
            intents.voice_states = True
            # Needed for reaction-based moderation controls.
            if hasattr(intents, "reactions"):
                intents.reactions = True
            if hasattr(intents, "dm_reactions"):
                intents.dm_reactions = True

            self._client = commands.Bot(
                command_prefix="!",  # Not really used, we handle raw messages
                intents=intents,
            )

            adapter_self = self  # capture for closure

            @self._client.event
            async def on_ready():
                logger.info("[%s] Connected as %s", adapter_self.name, adapter_self._client.user)
                adapter_self._ready_event.set()
                if not adapter_self._post_ready_initialized:
                    adapter_self._post_ready_initialized = True
                    adapter_self._post_ready_task = asyncio.create_task(
                        adapter_self._run_post_ready_startup(members=members)
                    )

            @self._client.event
            async def on_message(message: DiscordMessage):
                # Drop duplicate deliveries of the same Discord message ID.
                # This protects against occasional repeated gateway dispatch.
                now = time.time()
                msg_id = int(getattr(message, "id", 0) or 0)
                if msg_id:
                    last = self._seen_message_ids.get(msg_id)
                    if last and (now - last) < 120:
                        logger.info("[discord] duplicate message ignored: msg_id=%s", str(msg_id))
                        return
                    self._seen_message_ids[msg_id] = now
                    # Lightweight TTL cleanup
                    if len(self._seen_message_ids) > 2000:
                        cutoff = now - 300
                        self._seen_message_ids = {
                            k: v for k, v in self._seen_message_ids.items() if v >= cutoff
                        }

                # Always ignore our own messages
                if message.author == self._client.user:
                    return

                # Bot message filtering (DISCORD_ALLOW_BOTS):
                #   "none"     — ignore all other bots (default)
                #   "mentions" — accept bot messages only when they @mention us
                #   "all"      — accept all bot messages
                if getattr(message.author, "bot", False):
                    allow_bots = os.getenv("DISCORD_ALLOW_BOTS", "none").lower().strip()
                    if allow_bots == "none":
                        return
                    elif allow_bots == "mentions":
                        if not self._client.user or self._client.user not in message.mentions:
                            return
                    # "all" falls through to handle_message

                try:
                    await self._handle_message(message)
                except Exception:
                    logger.exception("[discord] message handler crashed")

            @self._client.event
            async def on_raw_reaction_add(payload):
                """
                Delete this bot's messages when a user reacts with :x: / ❌.
                Uses raw events so this works even when the message isn't cached.
                """
                try:
                    if self._client.user and payload.user_id == self._client.user.id:
                        return

                    emoji_name = getattr(payload.emoji, "name", "") or ""
                    if emoji_name not in {"❌", "x", "✖", "✖️"}:
                        return

                    channel = self._client.get_channel(payload.channel_id)
                    if channel is None:
                        channel = await self._client.fetch_channel(payload.channel_id)
                    if channel is None:
                        return

                    message = await channel.fetch_message(payload.message_id)
                    if message is None:
                        return
                    # Delete only this bot's own messages.
                    if not self._client.user or getattr(message.author, "id", None) != self._client.user.id:
                        return

                    await message.delete()
                    logger.info(
                        "[discord] deleted bot message %s via %s reaction by user %s",
                        str(message.id),
                        emoji_name,
                        str(payload.user_id),
                    )
                except Exception:
                    logger.exception("[discord] reaction delete handler failed")

            @self._client.event
            async def on_voice_state_update(member, before, after):
                """Track voice channel join/leave events."""
                # Only track channels where the bot is connected
                bot_guild_ids = set(adapter_self._voice_clients.keys())
                if not bot_guild_ids:
                    return
                guild_id = member.guild.id
                if guild_id not in bot_guild_ids:
                    return
                # Ignore the bot itself
                if member == adapter_self._client.user:
                    return

                joined = before.channel is None and after.channel is not None
                left = before.channel is not None and after.channel is None
                switched = (
                    before.channel is not None
                    and after.channel is not None
                    and before.channel != after.channel
                )

                if joined or left or switched:
                    logger.info(
                        "Voice state: %s (%d) %s (guild %d)",
                        member.display_name,
                        member.id,
                        "joined " + after.channel.name if joined
                        else "left " + before.channel.name if left
                        else f"moved {before.channel.name} -> {after.channel.name}",
                        guild_id,
                    )

            # Register slash commands
            self._register_slash_commands()
            try:
                registered = len(self._client.tree.get_commands())
            except Exception:
                registered = -1
            if registered >= 0:
                logger.info("[%s] Registered %d slash command roots locally", self.name, registered)
            else:
                logger.info("[%s] Registered slash commands locally", self.name)

            start_task = asyncio.create_task(self._client.start(self.config.token))
            ready_task = asyncio.create_task(self._ready_event.wait())

            try:
                done, pending = await asyncio.wait(
                    {start_task, ready_task},
                    timeout=30,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                _ = pending

                if ready_task in done and self._ready_event.is_set():
                    self._running = True
                    # Keep the Discord client task alive for the session lifetime.
                    self._client_task = start_task
                    return True, None

                if start_task in done:
                    if not ready_task.done():
                        ready_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await ready_task
                    exc = start_task.exception()
                    if exc:
                        return False, exc
                    return False, RuntimeError("Discord client exited before ready")

                # Timeout waiting for readiness: shut down this attempt cleanly.
                ready_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ready_task
                if not start_task.done():
                    start_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await start_task
                return False, asyncio.TimeoutError("Timeout waiting for Discord ready event")
            finally:
                if not self._running and self._client:
                    try:
                        await self._client.close()
                    except Exception:
                        pass
                    self._client = None
                if not self._running:
                    self._client_task = None

        try:
            # First attempt: normal behavior (message content + optional members intent)
            ok, err = await _attempt_connect(
                message_content=True,
                members=needs_members_intent,
            )
            if ok:
                return True

            err_text = str(err) if err else ""
            privileged_intents_rejected = bool(
                err
                and (
                    "PrivilegedIntentsRequired" in err.__class__.__name__
                    or "PrivilegedIntentsRequired" in err_text
                )
            )
            # If Discord rejects privileged intents, retry without them so DMs and slash
            # commands can still function.
            if privileged_intents_rejected:
                logger.warning(
                    "[%s] Privileged intents not enabled; retrying with reduced intents",
                    self.name,
                )
                ok, err = await _attempt_connect(message_content=False, members=False)
                if ok:
                    return True

            if isinstance(err, asyncio.TimeoutError):
                logger.error("[%s] Timeout waiting for connection to Discord", self.name)
            elif err:
                logger.error("[%s] Failed to connect to Discord: %s", self.name, err)
            else:
                logger.error("[%s] Failed to connect to Discord", self.name)
            return False
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to connect to Discord: %s", self.name, e, exc_info=True)
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        if self._post_ready_task and not self._post_ready_task.done():
            self._post_ready_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._post_ready_task
        self._post_ready_task = None
        self._post_ready_initialized = False

        # Clean up all active voice connections before closing the client
        for guild_id in list(self._voice_clients.keys()):
            try:
                await self.leave_voice_channel(guild_id)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.debug("[%s] Error leaving voice channel %s: %s", self.name, guild_id, e)

        if self._client:
            try:
                await self._client.close()
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning("[%s] Error during disconnect: %s", self.name, e, exc_info=True)
        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._client_task

        self._running = False
        self._client_task = None
        self._client = None
        self._ready_event.clear()
        logger.info("[%s] Disconnected", self.name)
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        include_listen_button: bool = True,
    ) -> SendResult:
        """Send a message to a Discord channel using chained embeds."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # Get the channel
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
            
            # Format and split message into embed-sized chunks
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_EMBED_DESCRIPTION)
            
            message_ids = []
            reference = None
            
            if reply_to:
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    reference = ref_msg
                except Exception as e:
                    logger.debug("Could not fetch reply-to message: %s", e)
            
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(description=chunk)
                is_tool_progress = bool((metadata or {}).get("tool_progress"))
                view = ListenButtonView(self) if (include_listen_button and not is_tool_progress) else None
                last_err: Optional[Exception] = None
                for attempt in range(1, self.CHAIN_SEND_MAX_RETRIES + 1):
                    try:
                        chunk_reference = reference if i == 0 else None
                        msg = await channel.send(
                            embed=embed,
                            view=view,
                            reference=chunk_reference,
                        )
                        last_err = None
                        break
                    except Exception as e:
                        err_text = str(e)
                        if (
                            chunk_reference is not None
                            and "error code: 50035" in err_text
                            and "Cannot reply to a system message" in err_text
                        ):
                            logger.warning(
                                "[%s] Reply target %s is a Discord system message; retrying send without reply reference",
                                self.name,
                                reply_to,
                            )
                            try:
                                msg = await channel.send(
                                    embed=embed,
                                    view=view,
                                    reference=None,
                                )
                                last_err = None
                                break
                            except Exception as retry_without_ref_error:
                                e = retry_without_ref_error
                        last_err = e
                        status = getattr(e, "status", None)
                        code = getattr(e, "code", None)
                        retry_after = getattr(e, "retry_after", None)
                        response_obj = getattr(e, "response", None)
                        if retry_after is None and response_obj is not None:
                            try:
                                header_val = response_obj.headers.get("Retry-After")
                                retry_after = float(header_val) if header_val else None
                            except Exception:
                                retry_after = None

                        logger.warning(
                            "[discord] chunk send failed (%d/%d, chunk %d/%d, len=%d, status=%s, code=%s, retry_after=%s): %s",
                            attempt,
                            self.CHAIN_SEND_MAX_RETRIES,
                            i + 1,
                            len(chunks),
                            len(chunk),
                            status,
                            code,
                            retry_after,
                            e,
                        )
                        if attempt < self.CHAIN_SEND_MAX_RETRIES:
                            if retry_after is not None:
                                delay = max(float(retry_after), self.CHAIN_SEND_DELAY_SECONDS)
                            else:
                                delay = self.CHAIN_SEND_DELAY_SECONDS * attempt
                            await asyncio.sleep(delay)

                if last_err is not None:
                    raise last_err

                message_ids.append(str(msg.id))

                # Add slight pacing between chained sends to reduce burst failures.
                if i < (len(chunks) - 1):
                    await asyncio.sleep(self.CHAIN_SEND_DELAY_SECONDS)
            
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids}
            )
            
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send Discord message: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent Discord message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            msg = await channel.fetch_message(int(message_id))
            formatted = self.format_message(content)
            if len(formatted) > self.MAX_EMBED_DESCRIPTION:
                formatted = formatted[:self.MAX_EMBED_DESCRIPTION - 3] + "..."
            embed = discord.Embed(description=formatted)
            await msg.edit(embed=embed)
            return SendResult(success=True, message_id=message_id)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to edit Discord message %s: %s", self.name, message_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def _send_file_attachment(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Send a local file as a Discord attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        channel = self._client.get_channel(int(chat_id))
        if not channel:
            channel = await self._client.fetch_channel(int(chat_id))
        if not channel:
            return SendResult(success=False, error=f"Channel {chat_id} not found")

        filename = file_name or os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            file = discord.File(fh, filename=filename)
            msg = await channel.send(content=caption if caption else None, file=file)
        return SendResult(success=True, message_id=str(msg.id))

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """Play auto-TTS audio.

        When the bot is in a voice channel for this chat's guild, play
        directly in the VC instead of sending as a file attachment.
        """
        for gid, text_ch_id in self._voice_text_channels.items():
            if str(text_ch_id) == str(chat_id) and self.is_in_voice_channel(gid):
                logger.info("[%s] Playing TTS in voice channel (guild=%d)", self.name, gid)
                success = await self.play_in_voice_channel(gid, audio_path)
                return SendResult(success=success)
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)

    @staticmethod
    def _discord_audio_duration_seconds(audio_path: str) -> Optional[float]:
        suffix = os.path.splitext(audio_path)[1].lower()
        try:
            if suffix == ".wav":
                with wave.open(audio_path, "rb") as wav_file:
                    frame_rate = wav_file.getframerate()
                    frame_count = wav_file.getnframes()
                    return (frame_count / float(frame_rate)) if frame_rate else None
            if suffix in {".ogg", ".opus"}:
                from mutagen.oggopus import OggOpus

                info = OggOpus(audio_path)
                return float(getattr(info.info, "length", 0.0) or 0.0) or None
            if suffix == ".mp3":
                from mutagen.mp3 import MP3

                info = MP3(audio_path)
                return float(getattr(info.info, "length", 0.0) or 0.0) or None
        except Exception:
            return None
        return None

    @staticmethod
    def _cleanup_temp_audio_files(paths: List[str], preserve: Optional[set[str]] = None) -> None:
        keep = preserve or set()
        for path in paths:
            if not path or path in keep:
                continue
            with suppress(OSError):
                os.unlink(path)

    @staticmethod
    def _transcode_audio_for_discord(audio_path: str) -> str:
        if not shutil.which("ffmpeg"):
            return audio_path

        suffix = os.path.splitext(audio_path)[1].lower()
        if suffix in {".ogg", ".opus"} and os.path.getsize(audio_path) <= DISCORD_AUDIO_SAFE_BYTES:
            return audio_path

        output_path = tempfile.NamedTemporaryFile(
            suffix=".ogg",
            prefix="discord-audio-",
            delete=False,
        ).name
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    audio_path,
                    "-vn",
                    "-ac",
                    "1",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "64k",
                    "-vbr",
                    "on",
                    "-application",
                    "voip",
                    output_path,
                    "-y",
                ],
                capture_output=True,
                timeout=180,
            )
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
        except Exception:
            pass

        with suppress(OSError):
            os.unlink(output_path)
        return audio_path

    @classmethod
    def _split_wav_for_discord(cls, audio_path: str, max_bytes: int) -> List[str]:
        target_bytes = int(max_bytes * 0.9)
        if target_bytes <= 0:
            return [audio_path]

        parts: List[str] = []
        with wave.open(audio_path, "rb") as wav_file:
            params = wav_file.getparams()
            bytes_per_frame = params.nchannels * params.sampwidth
            if bytes_per_frame <= 0:
                return [audio_path]
            frames_per_chunk = max(params.framerate, target_bytes // bytes_per_frame)
            index = 1
            while True:
                frames = wav_file.readframes(frames_per_chunk)
                if not frames:
                    break
                part_path = tempfile.NamedTemporaryFile(
                    suffix=f".part{index:03d}.wav",
                    prefix="discord-audio-",
                    delete=False,
                ).name
                with wave.open(part_path, "wb") as out_file:
                    out_file.setparams(params)
                    out_file.writeframes(frames)
                parts.append(part_path)
                index += 1
        return parts or [audio_path]

    @classmethod
    def _split_audio_for_discord(cls, audio_path: str, max_bytes: int = DISCORD_AUDIO_SAFE_BYTES) -> List[str]:
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) <= max_bytes:
            return [audio_path]

        duration = cls._discord_audio_duration_seconds(audio_path)
        if shutil.which("ffmpeg") and duration and duration > 1:
            estimated_parts = max(2, math.ceil(os.path.getsize(audio_path) / float(max_bytes)))
            segment_seconds = max(15, int(math.ceil(duration / estimated_parts)))
            for _attempt in range(4):
                part_dir = tempfile.mkdtemp(prefix="discord-audio-parts-")
                pattern = os.path.join(part_dir, "part-%03d.ogg")
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg",
                            "-i",
                            audio_path,
                            "-vn",
                            "-ac",
                            "1",
                            "-c:a",
                            "libopus",
                            "-b:a",
                            "64k",
                            "-vbr",
                            "on",
                            "-application",
                            "voip",
                            "-f",
                            "segment",
                            "-segment_time",
                            str(segment_seconds),
                            "-reset_timestamps",
                            "1",
                            pattern,
                            "-y",
                        ],
                        capture_output=True,
                        timeout=300,
                    )
                    parts = sorted(
                        os.path.join(part_dir, name)
                        for name in os.listdir(part_dir)
                        if name.endswith(".ogg")
                    )
                    if result.returncode == 0 and parts and all(os.path.getsize(part) <= max_bytes for part in parts):
                        return parts
                except Exception:
                    pass
                segment_seconds = max(5, segment_seconds // 2)

        if os.path.splitext(audio_path)[1].lower() == ".wav":
            return cls._split_wav_for_discord(audio_path, max_bytes)

        return [audio_path]

    async def _send_discord_audio_file(
        self,
        channel,
        audio_path: str,
        *,
        caption: Optional[str] = None,
    ) -> str:
        filename = os.path.basename(audio_path)
        with open(audio_path, "rb") as f:
            file = discord.File(io.BytesIO(f.read()), filename=filename)
        msg = await channel.send(content=str(caption or "").strip() or None, file=file)
        return str(msg.id)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a Discord file attachment."""
        temp_paths: List[str] = []
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")

            prepared_path = self._transcode_audio_for_discord(audio_path)
            if prepared_path != audio_path:
                temp_paths.append(prepared_path)

            segment_paths = self._split_audio_for_discord(prepared_path, DISCORD_AUDIO_SAFE_BYTES)
            for path in segment_paths:
                if path not in {audio_path, prepared_path}:
                    temp_paths.append(path)

            message_id = ""
            total_segments = len(segment_paths)
            for index, segment_path in enumerate(segment_paths, start=1):
                segment_caption = None
                if total_segments > 1:
                    prefix = f"Audio segment {index}/{total_segments}"
                    segment_caption = prefix if not caption or index > 1 else f"{caption}\n{prefix}"
                elif caption:
                    segment_caption = caption

                message_id = await self._send_discord_audio_file(
                    channel,
                    segment_path,
                    caption=segment_caption,
                )

            return SendResult(success=True, message_id=message_id)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send Discord audio: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))
        finally:
            self._cleanup_temp_audio_files(temp_paths, preserve={audio_path})

    # ------------------------------------------------------------------
    # Voice channel methods (join / leave / play)
    # ------------------------------------------------------------------

    async def join_voice_channel(self, channel) -> bool:
        """Join a Discord voice channel. Returns True on success."""
        if not self._client or not DISCORD_AVAILABLE:
            return False
        guild_id = channel.guild.id

        # Already connected in this guild?
        existing = self._voice_clients.get(guild_id)
        if existing and existing.is_connected():
            if existing.channel.id == channel.id:
                self._reset_voice_timeout(guild_id)
                return True
            await existing.move_to(channel)
            self._reset_voice_timeout(guild_id)
            return True

        vc = await channel.connect()
        self._voice_clients[guild_id] = vc
        self._reset_voice_timeout(guild_id)

        # Start voice receiver (Phase 2: listen to users)
        try:
            receiver = VoiceReceiver(vc, allowed_user_ids=self._allowed_user_ids)
            receiver.start()
            self._voice_receivers[guild_id] = receiver
            self._voice_listen_tasks[guild_id] = asyncio.ensure_future(
                self._voice_listen_loop(guild_id)
            )
        except Exception as e:
            logger.warning("Voice receiver failed to start: %s", e)

        return True

    async def leave_voice_channel(self, guild_id: int) -> None:
        """Disconnect from the voice channel in a guild."""
        # Stop voice receiver first
        receiver = self._voice_receivers.pop(guild_id, None)
        if receiver:
            receiver.stop()
        listen_task = self._voice_listen_tasks.pop(guild_id, None)
        if listen_task:
            listen_task.cancel()

        vc = self._voice_clients.pop(guild_id, None)
        if vc and vc.is_connected():
            await vc.disconnect()
        task = self._voice_timeout_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        self._voice_text_channels.pop(guild_id, None)

    # Maximum seconds to wait for voice playback before giving up
    PLAYBACK_TIMEOUT = 120

    async def play_in_voice_channel(self, guild_id: int, audio_path: str) -> bool:
        """Play an audio file in the connected voice channel."""
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return False

        # Pause voice receiver while playing (echo prevention)
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            receiver.pause()

        try:
            # Wait for current playback to finish (with timeout)
            wait_start = time.monotonic()
            while vc.is_playing():
                if time.monotonic() - wait_start > self.PLAYBACK_TIMEOUT:
                    logger.warning("Timed out waiting for previous playback to finish")
                    vc.stop()
                    break
                await asyncio.sleep(0.1)

            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _after(error):
                if error:
                    logger.error("Voice playback error: %s", error)
                loop.call_soon_threadsafe(done.set)

            discord_lib = discord
            if discord_lib is None:
                try:
                    import importlib
                    discord_lib = importlib.import_module("discord")
                except Exception:
                    logger.warning("discord module unavailable for voice playback")
                    return False

            source = discord_lib.FFmpegPCMAudio(audio_path)
            source = discord_lib.PCMVolumeTransformer(source, volume=1.0)
            vc.play(source, after=_after)
            try:
                await asyncio.wait_for(done.wait(), timeout=self.PLAYBACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Voice playback timed out after %ds", self.PLAYBACK_TIMEOUT)
                vc.stop()
            self._reset_voice_timeout(guild_id)
            return True
        finally:
            if receiver:
                receiver.resume()

    async def get_user_voice_channel(self, guild_id: int, user_id: str):
        """Return the voice channel the user is currently in, or None."""
        if not self._client:
            return None
        guild = self._client.get_guild(guild_id)
        if not guild:
            return None
        member = guild.get_member(int(user_id))
        if not member or not member.voice:
            return None
        return member.voice.channel

    def _reset_voice_timeout(self, guild_id: int) -> None:
        """Reset the auto-disconnect inactivity timer."""
        task = self._voice_timeout_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        self._voice_timeout_tasks[guild_id] = asyncio.ensure_future(
            self._voice_timeout_handler(guild_id)
        )

    async def _voice_timeout_handler(self, guild_id: int) -> None:
        """Auto-disconnect after VOICE_TIMEOUT seconds of inactivity."""
        try:
            await asyncio.sleep(self.VOICE_TIMEOUT)
        except asyncio.CancelledError:
            return
        text_ch_id = self._voice_text_channels.get(guild_id)
        await self.leave_voice_channel(guild_id)
        # Notify the runner so it can clean up voice_mode state
        if self._on_voice_disconnect and text_ch_id:
            try:
                self._on_voice_disconnect(str(text_ch_id))
            except Exception:
                pass
        if text_ch_id and self._client:
            ch = self._client.get_channel(text_ch_id)
            if ch:
                try:
                    await ch.send("Left voice channel (inactivity timeout).")
                except Exception:
                    pass

    def is_in_voice_channel(self, guild_id: int) -> bool:
        """Check if the bot is connected to a voice channel in this guild."""
        vc = self._voice_clients.get(guild_id)
        return vc is not None and vc.is_connected()

    def get_voice_channel_info(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Return voice channel awareness info for the given guild.

        Returns None if the bot is not in a voice channel.  Otherwise
        returns a dict with channel name, member list, count, and
        currently-speaking user IDs (from SSRC mapping).
        """
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return None

        channel = vc.channel
        if not channel:
            return None

        # Members currently in the voice channel (includes bot)
        members_info = []
        bot_user = self._client.user if self._client else None
        for m in channel.members:
            if bot_user and m.id == bot_user.id:
                continue  # skip the bot itself
            members_info.append({
                "user_id": m.id,
                "display_name": m.display_name,
                "is_bot": m.bot,
            })

        # Currently speaking users (from SSRC mapping + active buffers)
        speaking_user_ids: set = set()
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            import time as _time
            now = _time.monotonic()
            with receiver._lock:
                for ssrc, last_t in receiver._last_packet_time.items():
                    # Consider "speaking" if audio received within last 2 seconds
                    if now - last_t < 2.0:
                        uid = receiver._ssrc_to_user.get(ssrc)
                        if uid:
                            speaking_user_ids.add(uid)

        # Tag speaking status on members
        for info in members_info:
            info["is_speaking"] = info["user_id"] in speaking_user_ids

        return {
            "channel_name": channel.name,
            "member_count": len(members_info),
            "members": members_info,
            "speaking_count": len(speaking_user_ids),
        }

    def get_voice_channel_context(self, guild_id: int) -> str:
        """Return a human-readable voice channel context string.

        Suitable for injection into the system/ephemeral prompt so the
        agent is always aware of voice channel state.
        """
        info = self.get_voice_channel_info(guild_id)
        if not info:
            return ""

        parts = [f"[Voice channel: #{info['channel_name']} — {info['member_count']} participant(s)]"]
        for m in info["members"]:
            status = " (speaking)" if m["is_speaking"] else ""
            parts.append(f"  - {m['display_name']}{status}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Voice listening (Phase 2)
    # ------------------------------------------------------------------

    # UDP keepalive interval in seconds — prevents Discord from dropping
    # the UDP route after ~60s of silence.
    _KEEPALIVE_INTERVAL = 15

    async def _voice_listen_loop(self, guild_id: int):
        """Periodically check for completed utterances and process them."""
        receiver = self._voice_receivers.get(guild_id)
        if not receiver:
            return
        last_keepalive = time.monotonic()
        try:
            while receiver._running:
                await asyncio.sleep(0.2)

                # Send periodic UDP keepalive to prevent Discord from
                # dropping the UDP session after ~60s of silence.
                now = time.monotonic()
                if now - last_keepalive >= self._KEEPALIVE_INTERVAL:
                    last_keepalive = now
                    try:
                        vc = self._voice_clients.get(guild_id)
                        if vc and vc.is_connected():
                            vc._connection.send_packet(b'\xf8\xff\xfe')
                    except Exception:
                        pass

                completed = receiver.check_silence()
                for user_id, pcm_data in completed:
                    if not self._is_allowed_user(str(user_id)):
                        continue
                    await self._process_voice_input(guild_id, user_id, pcm_data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Voice listen loop error: %s", e, exc_info=True)

    async def _process_voice_input(self, guild_id: int, user_id: int, pcm_data: bytes):
        """Convert PCM -> WAV -> STT -> callback."""
        from tools.voice_mode import is_whisper_hallucination

        tmp_f = tempfile.NamedTemporaryFile(suffix=".wav", prefix="vc_listen_", delete=False)
        wav_path = tmp_f.name
        tmp_f.close()
        try:
            await asyncio.to_thread(VoiceReceiver.pcm_to_wav, pcm_data, wav_path)

            from tools.transcription_tools import transcribe_audio, get_stt_model_from_config
            stt_model = get_stt_model_from_config()
            result = await asyncio.to_thread(transcribe_audio, wav_path, model=stt_model)

            if not result.get("success"):
                return
            transcript = result.get("transcript", "").strip()
            if not transcript or is_whisper_hallucination(transcript):
                return

            logger.info("Voice input from user %d: %s", user_id, transcript[:100])

            if self._voice_input_callback:
                await self._voice_input_callback(
                    guild_id=guild_id,
                    user_id=user_id,
                    transcript=transcript,
                )
        except Exception as e:
            logger.warning("Voice input processing failed: %s", e, exc_info=True)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _is_allowed_user(self, user_id: str) -> bool:
        """Check if user is in DISCORD_ALLOWED_USERS."""
        if not self._allowed_user_ids:
            return True
        return user_id in self._allowed_user_ids

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file natively as a Discord file attachment."""
        try:
            return await self._send_file_attachment(chat_id, image_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Image file not found: {image_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local image, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        
        try:
            import aiohttp
            
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")
            
            # Download the image and send as a Discord file attachment
            # (Discord renders attachments inline, unlike plain URLs)
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to download image: HTTP {resp.status}")
                    
                    image_data = await resp.read()
                    
                    # Determine filename from URL or content type
                    content_type = resp.headers.get("content-type", "image/png")
                    ext = "png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = "jpg"
                    elif "gif" in content_type:
                        ext = "gif"
                    elif "webp" in content_type:
                        ext = "webp"
                    
                    import io
                    file = discord.File(io.BytesIO(image_data), filename=f"image.{ext}")
                    
                    msg = await channel.send(
                        content=caption if caption else None,
                        file=file,
                    )
                    return SendResult(success=True, message_id=str(msg.id))
        
        except ImportError:
            logger.warning(
                "[%s] aiohttp not installed, falling back to URL. Run: pip install aiohttp",
                self.name,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send image attachment, falling back to URL: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local video file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, video_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Video file not found: {video_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local video, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an arbitrary file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, file_path, caption, file_name=file_name)
        except FileNotFoundError:
            return SendResult(success=False, error=f"File not found: {file_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send document, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to, metadata=metadata)
    
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator."""
        if self._client:
            try:
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    await channel.typing()
            except Exception:
                pass  # Ignore typing indicator failures
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Discord channel."""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}
        
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            
            if not channel:
                return {"name": str(chat_id), "type": "dm"}
            
            # Determine channel type
            if isinstance(channel, discord.DMChannel):
                chat_type = "dm"
                name = channel.recipient.name if channel.recipient else str(chat_id)
            elif isinstance(channel, discord.Thread):
                chat_type = "thread"
                name = channel.name
            elif isinstance(channel, discord.TextChannel):
                chat_type = "channel"
                name = f"#{channel.name}"
                if channel.guild:
                    name = f"{channel.guild.name} / {name}"
            else:
                chat_type = "channel"
                name = getattr(channel, "name", str(chat_id))
            
            return {
                "name": name,
                "type": chat_type,
                "guild_id": str(channel.guild.id) if hasattr(channel, "guild") and channel.guild else None,
                "guild_name": channel.guild.name if hasattr(channel, "guild") and channel.guild else None,
            }
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to get chat info for %s: %s", self.name, chat_id, e, exc_info=True)
            return {"name": str(chat_id), "type": "dm", "error": str(e)}
    
    async def _resolve_allowed_usernames(self) -> None:
        """
        Resolve non-numeric entries in DISCORD_ALLOWED_USERS to Discord user IDs.

        Users can specify usernames (e.g. "teknium") or display names instead of
        raw numeric IDs.  After resolution, the env var and internal set are updated
        so authorization checks work with IDs only.
        """
        if not self._allowed_user_ids or not self._client:
            return

        numeric_ids = set()
        to_resolve = set()

        for entry in self._allowed_user_ids:
            if entry.isdigit():
                numeric_ids.add(entry)
            else:
                to_resolve.add(entry.lower())

        if not to_resolve:
            return

        print(f"[{self.name}] Resolving {len(to_resolve)} username(s): {', '.join(to_resolve)}")
        resolved_count = 0

        for guild in self._client.guilds:
            # Fetch full member list (requires members intent)
            try:
                members = guild.members
                if len(members) < guild.member_count:
                    members = [m async for m in guild.fetch_members(limit=None)]
            except Exception as e:
                logger.warning("Failed to fetch members for guild %s: %s", guild.name, e)
                continue

            for member in members:
                name_lower = member.name.lower()
                display_lower = member.display_name.lower()
                global_lower = (member.global_name or "").lower()

                matched = name_lower in to_resolve or display_lower in to_resolve or global_lower in to_resolve
                if matched:
                    uid = str(member.id)
                    numeric_ids.add(uid)
                    resolved_count += 1
                    matched_name = name_lower if name_lower in to_resolve else (
                        display_lower if display_lower in to_resolve else global_lower
                    )
                    to_resolve.discard(matched_name)
                    print(f"[{self.name}] Resolved '{matched_name}' -> {uid} ({member.name}#{member.discriminator})")

            if not to_resolve:
                break

        if to_resolve:
            print(f"[{self.name}] Could not resolve usernames: {', '.join(to_resolve)}")

        # Update internal set and env var so gateway auth checks use IDs
        self._allowed_user_ids = numeric_ids
        os.environ["DISCORD_ALLOWED_USERS"] = ",".join(sorted(numeric_ids))
        if resolved_count:
            print(f"[{self.name}] Updated DISCORD_ALLOWED_USERS with {resolved_count} resolved ID(s)")

    def format_message(self, content: str) -> str:
        """
        Format message for Discord.
        
        Discord uses its own markdown variant.
        """
        # Discord markdown is fairly standard, no special escaping needed
        return content

    async def _run_simple_slash(
        self,
        interaction: discord.Interaction,
        command_text: str,
        followup_msg: str | None = None,
        delete_original: bool = False,
    ) -> None:
        """Common handler for simple slash commands that dispatch a command string."""
        await interaction.response.defer(ephemeral=True)
        event = self._build_slash_event(interaction, command_text)
        await self.handle_message(event)
        if delete_original:
            try:
                await interaction.delete_original_response()
                return
            except Exception as e:
                logger.debug("Discord delete_original_response failed: %s", e)
        if not followup_msg:
            return
        try:
            await interaction.followup.send(followup_msg, ephemeral=True)
        except Exception as e:
            logger.debug("Discord followup failed: %s", e)

    async def _cron_job_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[Any]:
        """Return Discord autocomplete choices for cron job selectors."""
        del interaction  # unused for now; keep signature compatible with discord.py

        choice_cls = getattr(getattr(discord, "app_commands", None), "Choice", None)
        if choice_cls is None:
            return []

        try:
            from cron.jobs import list_jobs
            jobs = list_jobs(include_disabled=True)
        except Exception:
            return []

        needle = str(current or "").strip().lower()
        choices: List[Any] = []
        for job in jobs:
            job_id = str(job.get("id") or "").strip()
            if not job_id:
                continue
            name = str(job.get("name") or "(unnamed)").strip()
            state = str(job.get("state") or ("scheduled" if job.get("enabled", True) else "paused")).strip()
            schedule = str(job.get("schedule_display") or job.get("schedule") or "").strip()
            haystack = " ".join(part for part in (job_id, name, state, schedule) if part).lower()
            if needle and needle not in haystack:
                continue

            label = f"{name} [{state}] ({job_id})"
            if len(label) > 100:
                label = label[:97] + "..."
            choices.append(choice_cls(name=label, value=job_id))
            if len(choices) >= 25:
                break
        return choices

    def _register_slash_commands(self) -> None:
        """Register Discord slash commands on the command tree."""
        if not self._client:
            return

        tree = self._client.tree

        @tree.command(name="new", description="Start a new conversation")
        async def slash_new(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reset", "New conversation started~")

        @tree.command(name="reset", description="Reset your Hermes session")
        async def slash_reset(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reset", "Session reset~")

        @tree.command(name="model", description="Show or change the model")
        @discord.app_commands.describe(name="Model name (e.g. anthropic/claude-sonnet-4). Leave empty to see current.")
        async def slash_model(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/model {name}".strip())

        @tree.command(name="reasoning", description="Show or change reasoning effort")
        @discord.app_commands.describe(effort="Reasoning effort: xhigh, high, medium, low, minimal, or none.")
        async def slash_reasoning(interaction: discord.Interaction, effort: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/reasoning {effort}".strip())
            await self.handle_message(event)

        @tree.command(name="terminal", description="Show or change the local terminal shell (Windows)")
        @discord.app_commands.describe(mode="powershell, wsl, auto, or cmd. Leave empty to show current.")
        async def slash_terminal(interaction: discord.Interaction, mode: str = ""):
            await self._run_simple_slash(
                interaction,
                f"/terminal {mode}".strip(),
                followup_msg=None,
                delete_original=True,
            )

        @tree.command(name="personality", description="Set a personality")
        @discord.app_commands.describe(name="Personality name. Leave empty to list available.")
        async def slash_personality(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/personality {name}".strip())

        cron_group_cls = getattr(discord.app_commands, "Group", None)
        if cron_group_cls is not None:
            cron = cron_group_cls(name="cron", description="Manage Hermes cron jobs")

            @cron.command(name="list", description="List all scheduled cron jobs")
            async def slash_cron_list(interaction: discord.Interaction):
                await self._run_simple_slash(
                    interaction,
                    "/cron list",
                    followup_msg=None,
                    delete_original=True,
                )

            @cron.command(name="add", description="Add a new cron job")
            @discord.app_commands.describe(
                schedule="Accepted: 30m | 2h | 1d | every 30m | 0 9 * * * | 2026-03-03T14:00:00",
                prompt="What the job should do",
            )
            async def slash_cron_add(
                interaction: discord.Interaction,
                schedule: str,
                prompt: str,
            ):
                # Cron schedule parsing in the runner expects quoted schedules when
                # spaces are present (e.g., cron expressions).
                schedule_arg = f'"{schedule}"' if " " in schedule else schedule
                await self._run_simple_slash(
                    interaction,
                    f"/cron add {schedule_arg} {prompt}",
                    followup_msg=None,
                    delete_original=True,
                )

            @cron.command(name="remove", description="Remove a cron job")
            @discord.app_commands.describe(job_id="Job ID from /cron list")
            async def slash_cron_remove(interaction: discord.Interaction, job_id: str):
                await self._run_simple_slash(
                    interaction,
                    f"/cron remove {job_id}",
                    followup_msg=None,
                    delete_original=True,
                )
            autocomplete = getattr(discord.app_commands, "autocomplete", None)
            if autocomplete is not None:
                slash_cron_remove = autocomplete(job_id=self._cron_job_autocomplete)(slash_cron_remove)

            @cron.command(name="run", description="Run one cron job immediately")
            @discord.app_commands.describe(job_id="Job ID from /cron list")
            async def slash_cron_run(interaction: discord.Interaction, job_id: str):
                await self._run_simple_slash(
                    interaction,
                    f"/cron run {job_id}",
                    followup_msg=None,
                    delete_original=True,
                )
            if autocomplete is not None:
                slash_cron_run = autocomplete(job_id=self._cron_job_autocomplete)(slash_cron_run)

            if hasattr(tree, "add_command"):
                tree.add_command(cron)

        @tree.command(name="retry", description="Retry your last message")
        async def slash_retry(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/retry", "Retrying~")

        @tree.command(name="undo", description="Remove the last exchange")
        async def slash_undo(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/undo")

        @tree.command(name="status", description="Show Hermes session status")
        async def slash_status(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/status", "Status sent~")

        @tree.command(name="sethome", description="Set this chat as the home channel")
        async def slash_sethome(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/sethome")

        @tree.command(name="stop", description="Stop the running Hermes agent")
        async def slash_stop(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/stop", "Stop requested~")

        @tree.command(name="compress", description="Compress conversation context")
        async def slash_compress(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/compress")

        @tree.command(name="title", description="Set or show the session title")
        @discord.app_commands.describe(name="Session title. Leave empty to show current.")
        async def slash_title(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/title {name}".strip())

        @tree.command(name="resume", description="Resume a previously-named session")
        @discord.app_commands.describe(name="Session name to resume. Leave empty to list sessions.")
        async def slash_resume(interaction: discord.Interaction, name: str = ""):
            await self._run_simple_slash(interaction, f"/resume {name}".strip())

        @tree.command(name="usage", description="Show token usage for this session")
        async def slash_usage(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/usage")

        @tree.command(name="provider", description="Show available providers")
        async def slash_provider(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/provider")

        @tree.command(name="help", description="Show available commands")
        async def slash_help(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/help")

        @tree.command(name="insights", description="Show usage insights and analytics")
        @discord.app_commands.describe(days="Number of days to analyze (default: 7)")
        async def slash_insights(interaction: discord.Interaction, days: int = 7):
            await self._run_simple_slash(interaction, f"/insights {days}")

        @tree.command(name="reload-mcp", description="Reload MCP servers from config")
        async def slash_reload_mcp(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/reload-mcp")

        @tree.command(name="voice", description="Toggle voice reply mode")
        @discord.app_commands.describe(mode="Voice mode: on, off, tts, channel, leave, or status")
        @discord.app_commands.choices(mode=[
            discord.app_commands.Choice(name="channel — join your voice channel", value="channel"),
            discord.app_commands.Choice(name="leave — leave voice channel", value="leave"),
            discord.app_commands.Choice(name="on — voice reply to voice messages", value="on"),
            discord.app_commands.Choice(name="tts — voice reply to all messages", value="tts"),
            discord.app_commands.Choice(name="off — text only", value="off"),
            discord.app_commands.Choice(name="status — show current mode", value="status"),
        ])
        async def slash_voice(interaction: discord.Interaction, mode: str = ""):
            await interaction.response.defer(ephemeral=True)
            event = self._build_slash_event(interaction, f"/voice {mode}".strip())
            await self.handle_message(event)

        @tree.command(name="update", description="Update Hermes Agent to the latest version")
        async def slash_update(interaction: discord.Interaction):
            await self._run_simple_slash(interaction, "/update", "Update initiated~")

        @tree.command(name="thread", description="Create a new thread and start a Hermes session in it")
        @discord.app_commands.describe(
            name="Thread name",
            message="Optional first message to send to Hermes in the thread",
            auto_archive_duration="Auto-archive in minutes (60, 1440, 4320, 10080)",
        )
        async def slash_thread(
            interaction: discord.Interaction,
            name: str,
            message: str = "",
            auto_archive_duration: int = 1440,
        ):
            await interaction.response.defer(ephemeral=True)
            await self._handle_thread_create_slash(interaction, name, message, auto_archive_duration)

    def _build_slash_event(self, interaction: discord.Interaction, text: str) -> MessageEvent:
        """Build a MessageEvent from a Discord slash command interaction."""
        is_dm = isinstance(interaction.channel, discord.DMChannel)
        chat_type = "dm" if is_dm else "group"
        chat_name = ""
        if not is_dm and hasattr(interaction.channel, "name"):
            chat_name = interaction.channel.name
            if hasattr(interaction.channel, "guild") and interaction.channel.guild:
                chat_name = f"{interaction.channel.guild.name} / #{chat_name}"
        
        # Get channel topic (if available)
        chat_topic = getattr(interaction.channel, "topic", None)

        source = self.build_source(
            chat_id=str(interaction.channel_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            chat_topic=chat_topic,
        )

        msg_type = MessageType.COMMAND if text.startswith("/") else MessageType.TEXT
        return MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=interaction,
        )

    # ------------------------------------------------------------------
    # Thread creation helpers
    # ------------------------------------------------------------------

    async def _handle_thread_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> None:
        """Create a Discord thread from a slash command and start a session in it."""
        result = await self._create_thread(
            interaction,
            name=name,
            message=message,
            auto_archive_duration=auto_archive_duration,
        )

        if not result.get("success"):
            error = result.get("error", "unknown error")
            await interaction.followup.send(f"Failed to create thread: {error}", ephemeral=True)
            return

        thread_id = result.get("thread_id")
        thread_name = result.get("thread_name") or name

        # Tell the user where the thread is
        link = f"<#{thread_id}>" if thread_id else f"**{thread_name}**"
        await interaction.followup.send(f"Created thread {link}", ephemeral=True)

        # Track thread participation so follow-ups don't require @mention
        if thread_id:
            self._track_thread(thread_id)

        # If a message was provided, kick off a new Hermes session in the thread
        starter = (message or "").strip()
        if starter and thread_id:
            await self._dispatch_thread_session(interaction, thread_id, thread_name, starter)

    async def _dispatch_thread_session(
        self,
        interaction: discord.Interaction,
        thread_id: str,
        thread_name: str,
        text: str,
    ) -> None:
        """Build a MessageEvent pointing at a thread and send it through handle_message."""
        guild_name = ""
        if hasattr(interaction, "guild") and interaction.guild:
            guild_name = interaction.guild.name

        chat_name = f"{guild_name} / {thread_name}" if guild_name else thread_name

        source = self.build_source(
            chat_id=thread_id,
            chat_name=chat_name,
            chat_type="thread",
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            thread_id=thread_id,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=interaction,
        )
        await self.handle_message(event)

    def _thread_parent_channel(self, channel: Any) -> Any:
        """Return the parent text channel when invoked from a thread."""
        return getattr(channel, "parent", None) or channel

    async def _resolve_interaction_channel(self, interaction: discord.Interaction) -> Optional[Any]:
        """Return the interaction channel, fetching it if the payload is partial."""
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            return channel
        if not self._client:
            return None
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            return None
        channel = self._client.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await self._client.fetch_channel(int(channel_id))
        except Exception:
            return None

    async def _create_thread(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> Dict[str, Any]:
        """Create a thread in the current Discord channel.

        Tries ``parent_channel.create_thread()`` first.  If Discord rejects
        that (e.g. permission issues), falls back to sending a seed message
        and creating the thread from it.
        """
        name = (name or "").strip()
        if not name:
            return {"error": "Thread name is required."}

        if auto_archive_duration not in VALID_THREAD_AUTO_ARCHIVE_MINUTES:
            allowed = ", ".join(str(v) for v in sorted(VALID_THREAD_AUTO_ARCHIVE_MINUTES))
            return {"error": f"auto_archive_duration must be one of: {allowed}."}

        channel = await self._resolve_interaction_channel(interaction)
        if channel is None:
            return {"error": "Could not resolve the current Discord channel."}
        if isinstance(channel, discord.DMChannel):
            return {"error": "Discord threads can only be created inside server text channels, not DMs."}

        parent_channel = self._thread_parent_channel(channel)
        if parent_channel is None:
            return {"error": "Could not determine a parent text channel for the new thread."}

        display_name = getattr(getattr(interaction, "user", None), "display_name", None) or "unknown user"
        reason = f"Requested by {display_name} via /thread"
        starter_message = (message or "").strip()

        try:
            thread = await parent_channel.create_thread(
                name=name,
                auto_archive_duration=auto_archive_duration,
                reason=reason,
            )
            if starter_message:
                await thread.send(starter_message)
            return {
                "success": True,
                "thread_id": str(thread.id),
                "thread_name": getattr(thread, "name", None) or name,
            }
        except Exception as direct_error:
            try:
                seed_content = starter_message or f"\U0001f9f5 Thread created by Hermes: **{name}**"
                seed_msg = await parent_channel.send(seed_content)
                thread = await seed_msg.create_thread(
                    name=name,
                    auto_archive_duration=auto_archive_duration,
                    reason=reason,
                )
                return {
                    "success": True,
                    "thread_id": str(thread.id),
                    "thread_name": getattr(thread, "name", None) or name,
                }
            except Exception as fallback_error:
                return {
                    "error": (
                        "Discord rejected direct thread creation and the fallback also failed. "
                        f"Direct error: {direct_error}. Fallback error: {fallback_error}"
                    )
                }

    # ------------------------------------------------------------------
    # Auto-thread helpers
    # ------------------------------------------------------------------

    async def _auto_create_thread(self, message: 'DiscordMessage') -> Optional[Any]:
        """Create a thread from a user message for auto-threading.

        Returns the created thread object, or ``None`` on failure.
        """
        # Build a short thread name from the message
        content = (message.content or "").strip()
        thread_name = content[:80] if content else "Hermes"
        if len(content) > 80:
            thread_name = thread_name[:77] + "..."

        try:
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            return thread
        except Exception as e:
            logger.warning("[%s] Auto-thread creation failed: %s", self.name, e)
            return None

    async def send_exec_approval(
        self, chat_id: str, command: str, approval_id: str
    ) -> SendResult:
        """
        Send a button-based exec approval prompt for a dangerous command.

        Returns SendResult. The approval is resolved when a user clicks a button.
        """
        if not self._client or not DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")

        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))

            # Discord embed description limit is 4096; show full command up to that
            max_desc = 4088
            cmd_display = command if len(command) <= max_desc else command[: max_desc - 3] + "..."
            embed = discord.Embed(
                title="Command Approval Required",
                description=f"```\n{cmd_display}\n```",
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"Approval ID: {approval_id}")

            view = ExecApprovalView(
                approval_id=approval_id,
                allowed_user_ids=self._allowed_user_ids,
            )

            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    def _get_parent_channel_id(self, channel: Any) -> Optional[str]:
        """Return the parent channel ID for a Discord thread-like channel, if present."""
        parent = getattr(channel, "parent", None)
        if parent is not None and getattr(parent, "id", None) is not None:
            return str(parent.id)
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return str(parent_id)
        return None

    def _is_forum_parent(self, channel: Any) -> bool:
        """Best-effort check for whether a Discord channel is a forum channel."""
        if channel is None:
            return False
        forum_cls = getattr(discord, "ForumChannel", None)
        if forum_cls and isinstance(channel, forum_cls):
            return True
        channel_type = getattr(channel, "type", None)
        if channel_type is not None:
            type_value = getattr(channel_type, "value", channel_type)
            if type_value == 15:
                return True
        return False

    def _format_thread_chat_name(self, thread: Any) -> str:
        """Build a readable chat name for thread-like Discord channels, including forum context when available."""
        thread_name = getattr(thread, "name", None) or str(getattr(thread, "id", "thread"))
        parent = getattr(thread, "parent", None)
        guild = getattr(thread, "guild", None) or getattr(parent, "guild", None)
        guild_name = getattr(guild, "name", None)
        parent_name = getattr(parent, "name", None)

        if self._is_forum_parent(parent) and guild_name and parent_name:
            return f"{guild_name} / {parent_name} / {thread_name}"
        if parent_name and guild_name:
            return f"{guild_name} / #{parent_name} / {thread_name}"
        if parent_name:
            return f"{parent_name} / {thread_name}"
        return thread_name

    # ------------------------------------------------------------------
    # Thread participation persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _thread_state_path() -> Path:
        """Path to the persisted thread participation set."""
        from hermes_cli.config import get_hermes_home
        return get_hermes_home() / "discord_threads.json"

    @classmethod
    def _load_participated_threads(cls) -> set:
        """Load persisted thread IDs from disk."""
        path = cls._thread_state_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(data)
        except Exception as e:
            logger.debug("Could not load discord thread state: %s", e)
        return set()

    def _save_participated_threads(self) -> None:
        """Persist the current thread set to disk (best-effort)."""
        path = self._thread_state_path()
        try:
            # Trim to most recent entries if over cap
            thread_list = list(self._bot_participated_threads)
            if len(thread_list) > self._MAX_TRACKED_THREADS:
                thread_list = thread_list[-self._MAX_TRACKED_THREADS:]
                self._bot_participated_threads = set(thread_list)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(thread_list), encoding="utf-8")
        except Exception as e:
            logger.debug("Could not save discord thread state: %s", e)

    def _track_thread(self, thread_id: str) -> None:
        """Add a thread to the participation set and persist."""
        if thread_id not in self._bot_participated_threads:
            self._bot_participated_threads.add(thread_id)
            self._save_participated_threads()

    async def _handle_message(self, message: DiscordMessage) -> None:
        """Handle incoming Discord messages."""
        # In server channels (not DMs), require the bot to be @mentioned
        # UNLESS the channel is in the free-response list or the message is
        # in a thread where the bot has already participated.
        #
        # Config (all settable via discord.* in config.yaml):
        #   discord.require_mention: Require @mention in server channels (default: true)
        #   discord.free_response_channels: Channel IDs where bot responds without mention
        #   discord.auto_thread: Auto-create thread on @mention in channels (default: true)

        thread_id = None
        parent_channel_id = None
        is_thread = isinstance(message.channel, discord.Thread)
        if is_thread:
            thread_id = str(message.channel.id)
            parent_channel_id = self._get_parent_channel_id(message.channel)

        if not isinstance(message.channel, discord.DMChannel):
            free_channels_raw = os.getenv("DISCORD_FREE_RESPONSE_CHANNELS", "")
            free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
            channel_ids = {str(message.channel.id)}
            if parent_channel_id:
                channel_ids.add(parent_channel_id)

            require_mention = os.getenv("DISCORD_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no")
            is_free_channel = bool(channel_ids & free_channels)

            # Skip the mention check if the message is in a thread where
            # the bot has previously participated (auto-created or replied in).
            in_bot_thread = is_thread and thread_id in self._bot_participated_threads

            if require_mention and not is_free_channel and not in_bot_thread:
                if self._client.user not in message.mentions:
                    return

            if self._client.user and self._client.user in message.mentions:
                message.content = message.content.replace(f"<@{self._client.user.id}>", "").strip()
                message.content = message.content.replace(f"<@!{self._client.user.id}>", "").strip()

        # Auto-thread: when enabled, automatically create a thread for every
        # @mention in a text channel so each conversation is isolated (like Slack).
        # Messages already inside threads or DMs are unaffected.
        auto_threaded_channel = None
        if not is_thread and not isinstance(message.channel, discord.DMChannel):
            auto_thread = os.getenv("DISCORD_AUTO_THREAD", "true").lower() in ("true", "1", "yes")
            if auto_thread:
                thread = await self._auto_create_thread(message)
                if thread:
                    is_thread = True
                    thread_id = str(thread.id)
                    auto_threaded_channel = thread
                    self._track_thread(thread_id)

        # Determine message type
        msg_type = MessageType.TEXT
        if message.content.startswith("/"):
            msg_type = MessageType.COMMAND
        elif message.attachments:
            # Check attachment types
            for att in message.attachments:
                if att.content_type:
                    if att.content_type.startswith("image/"):
                        msg_type = MessageType.PHOTO
                    elif att.content_type.startswith("video/"):
                        msg_type = MessageType.VIDEO
                    elif att.content_type.startswith("audio/"):
                        msg_type = MessageType.AUDIO
                    else:
                        msg_type = MessageType.DOCUMENT
                    break
        
        # When auto-threading kicked in, route responses to the new thread
        effective_channel = auto_threaded_channel or message.channel

        # Determine chat type
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "dm"
            chat_name = message.author.name
        elif is_thread:
            chat_type = "thread"
            chat_name = self._format_thread_chat_name(effective_channel)
        else:
            chat_type = "group"
            chat_name = getattr(message.channel, "name", str(message.channel.id))
            if hasattr(message.channel, "guild") and message.channel.guild:
                chat_name = f"{message.channel.guild.name} / #{chat_name}"

        # Get channel topic (if available - TextChannels have topics, DMs/threads don't)
        chat_topic = getattr(message.channel, "topic", None)
        
        # Build source
        source = self.build_source(
            chat_id=str(effective_channel.id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )
        
        # Build media URLs -- download image attachments to local cache so the
        # vision tool can access them reliably (Discord CDN URLs can expire).
        media_urls = []
        media_types = []
        for att in message.attachments:
            content_type = att.content_type or "unknown"
            if content_type.startswith("image/"):
                try:
                    # Determine extension from content type (image/png -> .png)
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    cached_path = await cache_image_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user image: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache image attachment: {e}", flush=True)
                    # Fall back to the CDN URL if caching fails
                    media_urls.append(att.url)
                    media_types.append(content_type)
            elif content_type.startswith("audio/"):
                try:
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".ogg", ".mp3", ".wav", ".webm", ".m4a"):
                        ext = ".ogg"
                    cached_path = await cache_audio_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user audio: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache audio attachment: {e}", flush=True)
                    media_urls.append(att.url)
                    media_types.append(content_type)
            else:
                # Other attachments: keep the original URL
                media_urls.append(att.url)
                media_types.append(content_type)
        
        event = MessageEvent(
            text=message.content,
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.id),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=str(message.reference.message_id) if message.reference else None,
            timestamp=message.created_at,
        )

        # Track thread participation so the bot won't require @mention for
        # follow-up messages in threads it has already engaged in.
        if thread_id:
            self._track_thread(thread_id)

        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Discord UI Components (outside the adapter class)
# ---------------------------------------------------------------------------

if DISCORD_AVAILABLE:
    _LISTEN_BUTTON_STYLE = getattr(discord.ButtonStyle, "secondary", None)
    if _LISTEN_BUTTON_STYLE is None:
        _LISTEN_BUTTON_STYLE = getattr(discord.ButtonStyle, "primary", 1)

    class ListenButtonView(discord.ui.View):
        """Button view that reads the current embed text aloud via TTS."""

        def __init__(self, adapter: "DiscordAdapter"):
            try:
                super().__init__(timeout=3600)  # 1 hour
            except TypeError:
                # Test stubs often replace discord.ui.View with bare object.
                super().__init__()
            self.adapter = adapter

        @discord.ui.button(label="Listen", style=_LISTEN_BUTTON_STYLE, emoji="🔊")
        async def listen(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await _handle_discord_listen(self.adapter, interaction)

    class PersistentListenButtonView(discord.ui.View):
        """Persistent listen button for messages sent through REST payloads."""

        CUSTOM_ID = "hermes:listen"

        def __init__(self, adapter: "DiscordAdapter"):
            try:
                super().__init__(timeout=None)  # Persistent while process is running
            except TypeError:
                super().__init__()
            self.adapter = adapter

        @discord.ui.button(
            label="Listen",
            style=_LISTEN_BUTTON_STYLE,
            emoji="🔊",
            custom_id=CUSTOM_ID,
        )
        async def listen(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await _handle_discord_listen(self.adapter, interaction)


async def _handle_discord_listen(
    adapter: "DiscordAdapter", interaction: "discord.Interaction"
) -> None:
    try:
        if not interaction.message:
            await interaction.response.send_message(
                "No message context available for TTS.", ephemeral=True
            )
            return

        # Use embed description first (gateway uses embeds for normal replies).
        text = ""
        if interaction.message.embeds:
            text = (interaction.message.embeds[0].description or "").strip()
        if not text:
            text = (interaction.message.content or "").strip()
        if not text:
            await interaction.response.send_message(
                "Nothing to read from this message.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        from tools.tts_tool import text_to_speech_tool
        tts_json = await asyncio.to_thread(text_to_speech_tool, text)
        data = json.loads(tts_json)
        if not data.get("success"):
            await interaction.followup.send(
                f"TTS failed: {data.get('error', 'unknown error')}",
                ephemeral=True,
            )
            return

        audio_path = str(data.get("file_path", "")).strip()
        if not audio_path:
            await interaction.followup.send(
                "TTS returned no output file path.",
                ephemeral=True,
            )
            return

        result = await adapter.send_voice(
            chat_id=str(interaction.channel_id),
            audio_path=audio_path,
            reply_to=str(interaction.message.id),
        )
        if not result.success:
            await interaction.followup.send(
                f"Failed to send audio: {result.error}",
                ephemeral=True,
            )
            return

        # Success is silent: audio delivery in channel is the confirmation.
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("[discord] listen button handler failed")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("TTS failed unexpectedly.", ephemeral=True)
            else:
                await interaction.response.send_message("TTS failed unexpectedly.", ephemeral=True)
        except Exception:
            pass

    class ExecApprovalView(discord.ui.View):
        """
        Interactive button view for exec approval of dangerous commands.

        Shows three buttons: Allow Once (green), Always Allow (blue), Deny (red).
        Only users in the allowed list can click. The view times out after 5 minutes.
        """

        def __init__(self, approval_id: str, allowed_user_ids: set):
            super().__init__(timeout=300)  # 5-minute timeout
            self.approval_id = approval_id
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Verify the user clicking is authorized."""
            if not self.allowed_user_ids:
                return True  # No allowlist = anyone can approve
            return str(interaction.user.id) in self.allowed_user_ids

        async def _resolve(
            self, interaction: discord.Interaction, action: str, color: discord.Color
        ):
            """Resolve the approval and update the message."""
            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return

            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to approve commands~", ephemeral=True
                )
                return

            self.resolved = True

            # Update the embed with the decision
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{action} by {interaction.user.display_name}")

            # Disable all buttons
            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            # Store the approval decision
            try:
                from tools.approval import approve_permanent
                if action == "allow_once":
                    pass  # One-time approval handled by gateway
                elif action == "allow_always":
                    approve_permanent(self.approval_id)
            except ImportError:
                pass

        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_once", discord.Color.green())

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_always", discord.Color.blue())

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "deny", discord.Color.red())

        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True
