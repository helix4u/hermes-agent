"""
Gateway runner - entry point for messaging platform integrations.

This module provides:
- start_gateway(): Start all configured platform adapters
- GatewayRunner: Main class managing the gateway lifecycle

Usage:
    # Start the gateway
    python -m gateway.run
    
    # Or from CLI
    python cli.py --gateway
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import socket
import sys
import signal
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List, Callable
from urllib.request import urlopen

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Resolve Hermes home directory (respects HERMES_HOME override)
_hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))

# Load environment variables from ~/.hermes/.env first
from dotenv import load_dotenv
from agent.env_loader import load_dotenv_with_fallback
_env_path = _hermes_home / '.env'
if _env_path.exists():
    try:
        load_dotenv_with_fallback(_env_path, logger=logging.getLogger(__name__))
    except ValueError as exc:
        print(f"Failed to load {_env_path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
# Also try project .env as fallback
_project_env = Path(__file__).parent.parent / '.env'
if _project_env.exists():
    try:
        load_dotenv_with_fallback(
            _project_env,
            override=False,
            logger=logging.getLogger(__name__),
        )
    except ValueError as exc:
        print(f"Failed to load {_project_env}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

# Bridge config.yaml values into the environment so os.getenv() picks them up.
# config.yaml is authoritative for terminal settings — overrides .env.
_config_path = _hermes_home / 'config.yaml'
if _config_path.exists():
    try:
        import yaml as _yaml
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        # Top-level simple values (fallback only — don't override .env)
        for _key, _val in _cfg.items():
            if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
                os.environ[_key] = str(_val)
        # Terminal config is nested — bridge to TERMINAL_* env vars.
        # config.yaml overrides .env for these since it's the documented config path.
        _terminal_cfg = _cfg.get("terminal", {})
        if _terminal_cfg and isinstance(_terminal_cfg, dict):
            _terminal_env_map = {
                "backend": "TERMINAL_ENV",
                "cwd": "TERMINAL_CWD",
                "timeout": "TERMINAL_TIMEOUT",
                "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
                "docker_image": "TERMINAL_DOCKER_IMAGE",
                "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
                "modal_image": "TERMINAL_MODAL_IMAGE",
                "ssh_host": "TERMINAL_SSH_HOST",
                "ssh_user": "TERMINAL_SSH_USER",
                "ssh_port": "TERMINAL_SSH_PORT",
                "ssh_key": "TERMINAL_SSH_KEY",
                "container_cpu": "TERMINAL_CONTAINER_CPU",
                "container_memory": "TERMINAL_CONTAINER_MEMORY",
                "container_disk": "TERMINAL_CONTAINER_DISK",
                "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
            }
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    os.environ[_env_var] = str(_terminal_cfg[_cfg_key])
        _compression_cfg = _cfg.get("compression", {})
        if _compression_cfg and isinstance(_compression_cfg, dict):
            _compression_env_map = {
                "enabled": "CONTEXT_COMPRESSION_ENABLED",
                "threshold": "CONTEXT_COMPRESSION_THRESHOLD",
                "summary_provider": "CONTEXT_COMPRESSION_PROVIDER",
                "summary_model": "CONTEXT_COMPRESSION_MODEL",
                "protect_last_n": "CONTEXT_COMPRESSION_PROTECT_LAST_N",
                "summary_target_tokens": "CONTEXT_COMPRESSION_SUMMARY_TARGET_TOKENS",
            }
            for _cfg_key, _env_var in _compression_env_map.items():
                if _cfg_key in _compression_cfg:
                    os.environ[_env_var] = str(_compression_cfg[_cfg_key])
        _agent_cfg = _cfg.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "max_turns" in _agent_cfg:
                os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
    except Exception:
        pass  # Non-fatal; gateway can still run with .env values

# Gateway runs in quiet mode - suppress debug output and use cwd directly (no temp dirs)
os.environ["HERMES_QUIET"] = "1"

# Enable interactive exec approval for dangerous commands on messaging platforms
os.environ["HERMES_EXEC_ASK"] = "1"

# Set terminal working directory for messaging platforms.
# Uses MESSAGING_CWD if set. Otherwise prefer ~/.hermes/workspace (where
# project files are commonly located), then ~/.hermes, then home.
# This is separate from CLI which uses the directory where `hermes` is run.
_default_messaging_cwd = Path.home() / ".hermes" / "workspace"
if not _default_messaging_cwd.exists():
    _default_messaging_cwd = Path.home() / ".hermes"
if not _default_messaging_cwd.exists():
    _default_messaging_cwd = Path.home()
messaging_cwd = os.getenv("MESSAGING_CWD") or str(_default_messaging_cwd)
os.environ["TERMINAL_CWD"] = messaging_cwd

from gateway.config import (
    Platform,
    GatewayConfig,
    load_gateway_config,
)
from gateway.session import (
    SessionStore,
    SessionSource,
    SessionContext,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
)
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType

logger = logging.getLogger(__name__)

# Default model when nothing is configured (must match hermes_cli.config DEFAULT_CONFIG / CLI fallback)
_DEFAULT_MODEL = "google/gemini-2.0-flash-001:free"


def _resolve_gateway_model() -> str:
    """
    Resolve the model for gateway agents. Same priority as CLI so gateway and
    CLI share one source of truth: HERMES_MODEL (session override) > LLM_MODEL
    > OPENAI_MODEL > config.yaml model > default.
    """
    model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL")
    if model:
        return model.strip()
    try:
        import yaml as _yaml
        cfg_path = Path.home() / ".hermes" / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as _f:
                cfg = _yaml.safe_load(_f) or {}
            m = cfg.get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
            if isinstance(m, dict):
                default = (m.get("default") or "").strip()
                if default:
                    return default
    except Exception:
        pass
    return _DEFAULT_MODEL


def _resolve_runtime_agent_kwargs() -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances."""
    from hermes_cli.runtime_provider import (
        resolve_runtime_provider,
        format_runtime_provider_error,
    )

    try:
        runtime = resolve_runtime_provider(
            requested=os.getenv("HERMES_INFERENCE_PROVIDER"),
        )
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
    }


def _load_user_config() -> dict:
    """Load ~/.hermes/config.yaml as a dictionary."""
    import yaml

    config_path = _hermes_home / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_user_config(config: dict) -> None:
    """Persist ~/.hermes/config.yaml."""
    import yaml

    config_path = _hermes_home / "config.yaml"
    with open(config_path, "w", encoding="utf-8", newline="") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _get_image_generation_defaults() -> Optional[dict]:
    """Return persisted image-generation defaults, if any."""
    try:
        config = _load_user_config()
    except Exception:
        return None
    image_cfg = config.get("image_generation", {})
    if not isinstance(image_cfg, dict):
        return None
    defaults = image_cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        return None
    model = str(defaults.get("model") or "").strip()
    if not model:
        return None
    raw_aspect_ratio = defaults.get("aspect_ratio")
    sexagesimal_map = {
        61: "1:1",
        243: "4:3",
        184: "3:4",
        556: "9:16",
        969: "16:9",
    }
    if isinstance(raw_aspect_ratio, int):
        defaults["aspect_ratio"] = sexagesimal_map.get(raw_aspect_ratio, str(raw_aspect_ratio))
    elif raw_aspect_ratio is not None:
        defaults["aspect_ratio"] = str(raw_aspect_ratio).strip()
    return defaults


def _save_image_generation_defaults(
    *,
    model: str,
    aspect_ratio: str,
    width: int,
    height: int,
) -> None:
    """Persist image-generation defaults into config.yaml."""
    config = _load_user_config()
    image_cfg = config.setdefault("image_generation", {})
    if not isinstance(image_cfg, dict):
        image_cfg = {}
        config["image_generation"] = image_cfg
    defaults = image_cfg.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        image_cfg["defaults"] = defaults

    defaults.update({
        "provider": "invokeai-local-api",
        "model": model,
        "aspect_ratio": aspect_ratio,
        "width": int(width),
        "height": int(height),
    })
    _save_user_config(config)


def _format_image_defaults(defaults: dict) -> str:
    """Human-readable summary of saved image defaults."""
    model = defaults.get("model", "")
    aspect_ratio = defaults.get("aspect_ratio", "1:1")
    width = defaults.get("width")
    height = defaults.get("height")
    lines = [
        "🖼️ **Current image defaults**",
        f"- Model: `{model}`",
        f"- Aspect ratio: `{aspect_ratio}`",
    ]
    if width and height:
        lines.append(f"- Resolution: `{width}x{height}`")
    return "\n".join(lines)


def _get_wiki_source_root() -> Path:
    """Return the on-disk source root that contains wiki content."""
    return _hermes_home / "workspace" / "data"


def _get_wiki_web_root_base() -> Path:
    """Return the parent directory used for generated LAN wiki roots."""
    return _get_wiki_source_root() / "wiki_serve"


def _link_or_copy_path(source: Path, dest: Path) -> None:
    """Create a symlink when possible, otherwise copy the source into place."""
    try:
        os.symlink(source, dest, target_is_directory=source.is_dir())
        return
    except OSError:
        pass

    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)


def _build_wiki_web_root() -> Path:
    """Create an isolated web root that exposes only the wiki landing page and wiki tree."""
    source_root = _get_wiki_source_root()
    landing_page = source_root / "knowledge_wiki.html"
    wiki_root = source_root / "wiki"
    if not landing_page.exists():
        raise FileNotFoundError(f"Wiki landing page not found: {landing_page}")
    if not wiki_root.exists():
        raise FileNotFoundError(f"Wiki content directory not found: {wiki_root}")

    web_root_base = _get_wiki_web_root_base()
    web_root_base.mkdir(parents=True, exist_ok=True)
    web_root = Path(tempfile.mkdtemp(prefix="lan_wiki_", dir=web_root_base))

    _link_or_copy_path(landing_page, web_root / "knowledge_wiki.html")
    _link_or_copy_path(landing_page, web_root / "index.html")
    _link_or_copy_path(wiki_root, web_root / "wiki")
    return web_root


def _cleanup_wiki_web_root(web_root: Optional[Path]) -> None:
    """Remove a generated LAN wiki root without touching the source wiki content."""
    if not web_root:
        return
    try:
        shutil.rmtree(web_root, ignore_errors=True)
    except Exception:
        pass


def _get_wiki_host_config() -> dict:
    """Load persisted wiki-hosting configuration."""
    try:
        config = _load_user_config()
    except Exception:
        return {}
    wiki_cfg = config.get("wiki_hosting", {})
    return wiki_cfg if isinstance(wiki_cfg, dict) else {}


def _save_wiki_host_config(*, enabled: bool, port: Optional[int]) -> None:
    """Persist wiki-hosting configuration to config.yaml."""
    config = _load_user_config()
    wiki_cfg = config.setdefault("wiki_hosting", {})
    if not isinstance(wiki_cfg, dict):
        wiki_cfg = {}
        config["wiki_hosting"] = wiki_cfg
    wiki_cfg["enabled"] = bool(enabled)
    if port is not None:
        wiki_cfg["port"] = int(port)
    _save_user_config(config)


def _resolve_lan_ip() -> str:
    """Best-effort LAN IPv4 for URLs shown to the user."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass

    candidates: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            candidate = info[4][0]
            if candidate and not candidate.startswith("127.") and candidate not in candidates:
                candidates.append(candidate)
    except Exception:
        pass

    for candidate in candidates:
        if candidate.startswith("192.168.1."):
            return candidate
    for candidate in candidates:
        if candidate.startswith(("192.168.", "10.", "172.")):
            return candidate
    return "127.0.0.1"


class _QuietWikiRequestHandler(SimpleHTTPRequestHandler):
    """Simple static-file handler with quieter logging."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/wiki/knowledge_wiki.html", "/wiki/"}:
            self.send_response(302)
            self.send_header("Location", "/knowledge_wiki.html")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.debug("wiki-host: " + format, *args)


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP server that requests exclusive port ownership when available."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


class GatewayRunner:
    """
    Main gateway controller.
    
    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """
    
    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or load_gateway_config()
        self.adapters: Dict[Platform, BasePlatformAdapter] = {}

        # Load ephemeral config from config.yaml / env vars.
        # Both are injected at API-call time only and never persisted.
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._provider_routing = self._load_provider_routing()

        # Wire process registry into session store for reset protection
        from tools.process_registry import process_registry
        self.session_store = SessionStore(
            self.config.sessions_dir, self.config,
            has_active_processes_fn=lambda key: process_registry.has_active_for_session(key),
            on_auto_reset=self._flush_memories_before_reset,
        )
        self.delivery_router = DeliveryRouter(self.config)
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # Track running agents per session for interrupt support
        # Key: session_key, Value: AIAgent instance
        self._running_agents: Dict[str, Any] = {}
        self._pending_messages: Dict[str, str] = {}  # Queued messages during interrupt
        
        # Track pending exec approvals per session
        # Key: session_key, Value: {"command": str, "pattern_key": str}
        self._pending_approvals: Dict[str, Dict[str, str]] = {}
        self._wiki_server: ThreadingHTTPServer | None = None
        self._wiki_server_thread: threading.Thread | None = None
        self._wiki_host_port: int | None = None
        self._wiki_web_root: Path | None = None
        
        # Initialize session database for session_search tool support
        self._session_db = None
        try:
            from hermes_state import SessionDB
            self._session_db = SessionDB()
        except Exception as e:
            logger.debug("SQLite session store not available: %s", e)
        
        # DM pairing store for code-based user authorization
        from gateway.pairing import PairingStore
        self.pairing_store = PairingStore()
        
        # Event hook system
        from gateway.hooks import HookRegistry
        self.hooks = HookRegistry()

    def _wiki_host_status_message(self) -> str:
        """Human-readable current wiki hosting state."""
        if not self._wiki_server or not self._wiki_host_port:
            return (
                "📚 Wiki hosting is currently disabled.\n\n"
                "Use `/wiki-host enable <port>` to expose the local wiki on your LAN."
            )

        lan_ip = _resolve_lan_ip()
        url = f"http://{lan_ip}:{self._wiki_host_port}/knowledge_wiki.html"
        web_root = self._wiki_web_root or _get_wiki_web_root_base()
        return (
            "📚 Wiki hosting is enabled.\n"
            f"- Root: `{web_root}`\n"
            f"- Port: `{self._wiki_host_port}`\n"
            f"- URL: {url}"
        )

    def _start_wiki_host(self, port: int) -> str:
        """Start or restart the local LAN wiki host."""
        if self._wiki_server and self._wiki_host_port == port:
            return self._wiki_host_status_message()

        self._stop_wiki_host()
        web_root = _build_wiki_web_root()

        handler = lambda *args, **kwargs: _QuietWikiRequestHandler(  # noqa: E731
            *args,
            directory=str(web_root),
            **kwargs,
        )
        server = _ExclusiveThreadingHTTPServer(("0.0.0.0", int(port)), handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"wiki-host-{port}",
            daemon=True,
        )
        thread.start()

        self._wiki_server = server
        self._wiki_server_thread = thread
        self._wiki_host_port = int(port)
        self._wiki_web_root = web_root
        probe_url = f"http://127.0.0.1:{port}/knowledge_wiki.html"
        deadline = time.time() + 3
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urlopen(probe_url, timeout=1) as resp:
                    if getattr(resp, "status", 200) == 200:
                        last_error = None
                        break
            except Exception as e:
                last_error = e
                time.sleep(0.1)
        if last_error is not None:
            self._stop_wiki_host()
            raise RuntimeError(f"Wiki host started but self-probe failed for {probe_url}: {last_error}")
        _save_wiki_host_config(enabled=True, port=port)
        return self._wiki_host_status_message()

    def _stop_wiki_host(self) -> None:
        """Stop the local wiki host if it is running."""
        server = self._wiki_server
        thread = self._wiki_server_thread
        web_root = self._wiki_web_root
        self._wiki_server = None
        self._wiki_server_thread = None
        self._wiki_host_port = None
        self._wiki_web_root = None

        if server:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=2)
        _cleanup_wiki_web_root(web_root)
    
    def _flush_memories_before_reset(self, old_entry):
        """Prompt the agent to save memories/skills before an auto-reset.
        
        Called synchronously by SessionStore before destroying an expired session.
        Loads the transcript, gives the agent a real turn with memory + skills
        tools, and explicitly asks it to preserve anything worth keeping.
        """
        try:
            history = self.session_store.load_transcript(old_entry.session_id)
            if not history or len(history) < 4:
                return

            from run_agent import AIAgent
            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                return

            tmp_agent = AIAgent(
                **runtime_kwargs,
                max_iterations=8,
                quiet_mode=True,
                enabled_toolsets=["memory", "skills"],
                session_id=old_entry.session_id,
            )

            # Build conversation history from transcript
            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]

            # Give the agent a real turn to think about what to save
            flush_prompt = (
                "[System: This session is about to be automatically reset due to "
                "inactivity or a scheduled daily reset. The conversation context "
                "will be cleared after this turn.\n\n"
                "Review the conversation above and:\n"
                "1. Save any important facts, preferences, or decisions to memory "
                "(user profile or your notes) that would be useful in future sessions.\n"
                "2. If you discovered a reusable workflow or solved a non-trivial "
                "problem, consider saving it as a skill.\n"
                "3. If nothing is worth saving, that's fine — just skip.\n\n"
                "Do NOT respond to the user. Just use the memory and skill_manage "
                "tools if needed, then stop.]"
            )

            tmp_agent.run_conversation(
                user_message=flush_prompt,
                conversation_history=msgs,
            )
            logger.info("Pre-reset save completed for session %s", old_entry.session_id)
        except Exception as e:
            logger.debug("Pre-reset save failed for session %s: %s", old_entry.session_id, e)
    
    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.
        
        Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
        the prefill_messages_file key in ~/.hermes/config.yaml.
        Relative paths are resolved from ~/.hermes/.
        """
        import json as _json
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    file_path = cfg.get("prefill_messages_file", "")
            except Exception:
                pass
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as e:
            logger.warning("Failed to load prefill messages from %s: %s", path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var.
        
        Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then falls back to
        agent.system_prompt in ~/.hermes/config.yaml.
        """
        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return (cfg.get("agent", {}).get("system_prompt", "") or "").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _load_reasoning_config() -> dict | None:
        """Load reasoning effort from config or env var.
        
        Checks HERMES_REASONING_EFFORT env var first, then agent.reasoning_effort
        in config.yaml. Valid: "xhigh", "high", "medium", "low", "minimal", "none".
        Returns None to use default (xhigh).
        """
        effort = os.getenv("HERMES_REASONING_EFFORT", "")
        if not effort:
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    effort = str(cfg.get("agent", {}).get("reasoning_effort", "") or "").strip()
            except Exception:
                pass
        if not effort:
            return None
        effort = effort.lower().strip()
        if effort == "none":
            return {"enabled": False}
        valid = ("xhigh", "high", "medium", "low", "minimal")
        if effort in valid:
            return {"enabled": True, "effort": effort}
        logger.warning("Unknown reasoning_effort '%s', using default (xhigh)", effort)
        return None

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path) as _f:
                    cfg = _y.safe_load(_f) or {}
                return cfg.get("provider_routing", {}) or {}
        except Exception:
            pass
        return {}

    async def start(self) -> bool:
        """
        Start the gateway and all configured platform adapters.
        
        Returns True if at least one adapter connected successfully.
        """
        logger.info("Starting Hermes Gateway...")
        logger.info("Session storage: %s", self.config.sessions_dir)
        
        # Warn if no user allowlists are configured and open access is not opted in
        _any_allowlist = any(
            os.getenv(v)
            for v in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
                       "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
                       "GATEWAY_ALLOWED_USERS")
        )
        _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")
        if not _any_allowlist and not _allow_all:
            logger.warning(
                "No user allowlists configured. All unauthorized users will be denied. "
                "Set GATEWAY_ALLOW_ALL_USERS=true in ~/.hermes/.env to allow open access, "
                "or configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id)."
            )
        
        # Discover and load event hooks
        self.hooks.discover_and_load()
        
        # Recover background processes from checkpoint (crash recovery)
        try:
            from tools.process_registry import process_registry
            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                logger.info("Recovered %s background process(es) from previous run", recovered)
        except Exception as e:
            logger.warning("Process checkpoint recovery: %s", e)
        
        connected_count = 0
        
        # Initialize and connect each configured platform
        for platform, platform_config in self.config.platforms.items():
            if not platform_config.enabled:
                continue
            
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                logger.warning("No adapter available for %s", platform.value)
                continue
            
            # Set up message handler
            adapter.set_message_handler(self._handle_message)
            
            # Try to connect
            logger.info("Connecting to %s...", platform.value)
            try:
                success = await adapter.connect()
                if success:
                    self.adapters[platform] = adapter
                    connected_count += 1
                    logger.info("ok: %s connected", platform.value)
                else:
                    logger.warning("x: %s failed to connect", platform.value)
            except Exception as e:
                logger.error("x: %s error: %s", platform.value, e)
        
        if connected_count == 0:
            logger.warning("No messaging platforms connected.")
            logger.info("Gateway will continue running for cron job execution.")
        
        # Update delivery router with adapters
        self.delivery_router.adapters = self.adapters
        
        self._running = True
        
        # Emit gateway:startup hook
        hook_count = len(self.hooks.loaded_hooks)
        if hook_count:
            logger.info("%s hook(s) loaded", hook_count)
        await self.hooks.emit("gateway:startup", {
            "platforms": [p.value for p in self.adapters.keys()],
        })
        
        if connected_count > 0:
            logger.info("Gateway running with %s platform(s)", connected_count)
        
        # Build initial channel directory for send_message name resolution
        try:
            from gateway.channel_directory import build_channel_directory
            directory = build_channel_directory(self.adapters)
            ch_count = sum(len(chs) for chs in directory.get("platforms", {}).values())
            logger.info("Channel directory built: %d target(s)", ch_count)
        except Exception as e:
            logger.warning("Channel directory build failed: %s", e)

        wiki_cfg = _get_wiki_host_config()
        if wiki_cfg.get("enabled"):
            try:
                wiki_port = int(wiki_cfg.get("port") or 8008)
                logger.info("Restoring wiki host on port %s", wiki_port)
                self._start_wiki_host(wiki_port)
            except Exception as e:
                logger.warning("Failed to restore wiki host: %s", e)
        
        logger.info("Press Ctrl+C to stop")
        
        return True
    
    async def stop(self) -> None:
        """Stop the gateway and disconnect all adapters."""
        logger.info("Stopping gateway...")
        self._running = False
        self._stop_wiki_host()
        
        for platform, adapter in self.adapters.items():
            try:
                await adapter.disconnect()
                logger.info("%s disconnected", platform.value)
            except Exception as e:
                logger.error("%s disconnect error: %s", platform.value, e)
        
        self.adapters.clear()
        self._shutdown_event.set()
        
        from gateway.status import remove_pid_file
        remove_pid_file()
        
        logger.info("Gateway stopped")
    
    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()
    
    def _create_adapter(
        self, 
        platform: Platform, 
        config: Any
    ) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform."""
        if platform == Platform.TELEGRAM:
            from gateway.platforms.telegram import TelegramAdapter, check_telegram_requirements
            if not check_telegram_requirements():
                logger.warning("Telegram: python-telegram-bot not installed")
                return None
            return TelegramAdapter(config)
        
        elif platform == Platform.DISCORD:
            from gateway.platforms.discord import DiscordAdapter, check_discord_requirements
            if not check_discord_requirements():
                logger.warning("Discord: discord.py not installed")
                return None
            return DiscordAdapter(config)
        
        elif platform == Platform.WHATSAPP:
            from gateway.platforms.whatsapp import WhatsAppAdapter, check_whatsapp_requirements
            if not check_whatsapp_requirements():
                logger.warning("WhatsApp: Node.js not installed or bridge not configured")
                return None
            return WhatsAppAdapter(config)
        
        elif platform == Platform.SLACK:
            from gateway.platforms.slack import SlackAdapter, check_slack_requirements
            if not check_slack_requirements():
                logger.warning("Slack: slack-bolt not installed. Run: pip install 'hermes-agent[slack]'")
                return None
            return SlackAdapter(config)
        
        return None
    
    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        Check if a user is authorized to use the bot.
        
        Checks in order:
        1. Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        2. Environment variable allowlists (TELEGRAM_ALLOWED_USERS, etc.)
        3. DM pairing approved list
        4. Global allow-all (GATEWAY_ALLOW_ALL_USERS=true)
        5. Default: deny
        """
        user_id = source.user_id
        if not user_id:
            return False

        platform_env_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
            Platform.DISCORD: "DISCORD_ALLOWED_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
            Platform.SLACK: "SLACK_ALLOWED_USERS",
        }
        platform_allow_all_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOW_ALL_USERS",
            Platform.DISCORD: "DISCORD_ALLOW_ALL_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOW_ALL_USERS",
            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",
        }

        # Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        platform_allow_all_var = platform_allow_all_map.get(source.platform, "")
        if platform_allow_all_var and os.getenv(platform_allow_all_var, "").lower() in ("true", "1", "yes"):
            return True

        # Check pairing store (always checked, regardless of allowlists)
        platform_name = source.platform.value if source.platform else ""
        if self.pairing_store.is_approved(platform_name, user_id):
            return True

        # Check platform-specific and global allowlists
        platform_allowlist = os.getenv(platform_env_map.get(source.platform, ""), "").strip()
        global_allowlist = os.getenv("GATEWAY_ALLOWED_USERS", "").strip()

        if not platform_allowlist and not global_allowlist:
            # No allowlists configured -- check global allow-all flag
            return os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")

        # Check if user is in any allowlist
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update(uid.strip() for uid in platform_allowlist.split(",") if uid.strip())
        if global_allowlist:
            allowed_ids.update(uid.strip() for uid in global_allowlist.split(",") if uid.strip())

        # WhatsApp JIDs have @s.whatsapp.net suffix — strip it for comparison
        check_ids = {user_id}
        if "@" in user_id:
            check_ids.add(user_id.split("@")[0])
        return bool(check_ids & allowed_ids)
    
    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        Handle an incoming message from any platform.
        
        This is the core message processing pipeline:
        1. Check user authorization
        2. Check for commands (/new, /reset, etc.)
        3. Check for running agent and interrupt if needed
        4. Get or create session
        5. Build context for agent
        6. Run agent conversation
        7. Return response
        """
        source = event.source
        logger.info(
            "Gateway message received: platform=%s chat_type=%s chat_id=%s user_id=%s text_len=%s",
            source.platform.value if source.platform else "unknown",
            source.chat_type,
            source.chat_id,
            source.user_id,
            len(event.text or ""),
        )
        authorized = self._is_user_authorized(source)
        logger.info(
            "Gateway auth check: platform=%s user_id=%s authorized=%s",
            source.platform.value if source.platform else "unknown",
            source.user_id,
            authorized,
        )
        
        # Check if user is authorized
        if not authorized:
            logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            # In DMs: offer pairing code. In groups: silently ignore.
            if source.chat_type == "dm":
                platform_name = source.platform.value if source.platform else "unknown"
                code = self.pairing_store.generate_code(
                    platform_name, source.user_id, source.user_name or ""
                )
                if code:
                    adapter = self.adapters.get(source.platform)
                    if adapter:
                        await adapter.send(
                            source.chat_id,
                            f"Hi~ I don't recognize you yet!\n\n"
                            f"Here's your pairing code: `{code}`\n\n"
                            f"Ask the bot owner to run:\n"
                            f"`hermes pairing approve {platform_name} {code}`"
                        )
                else:
                    adapter = self.adapters.get(source.platform)
                    if adapter:
                        await adapter.send(
                            source.chat_id,
                            "Too many pairing requests right now~ "
                            "Please try again later!"
                        )
            return None
        
        # PRIORITY: If an agent is already running for this session, interrupt it
        # immediately. This is before command parsing to minimize latency -- the
        # user's "stop" message reaches the agent as fast as possible.
        _quick_key = build_session_key(source)
        if _quick_key in self._running_agents:
            running_agent = self._running_agents[_quick_key]
            logger.debug("PRIORITY interrupt for session %s", _quick_key[:20])
            running_agent.interrupt(event.text)
            if _quick_key in self._pending_messages:
                self._pending_messages[_quick_key] += "\n" + event.text
            else:
                self._pending_messages[_quick_key] = event.text
            return None
        
        # Check for commands
        command = event.get_command()
        if command:
            logger.info("Gateway command detected: /%s from user_id=%s", command, source.user_id)
        # Emit command:* hook for any recognized slash command
        _known_commands = {
            "new", "reset", "help", "status", "stop", "model",
            "personality", "retry", "undo", "sethome", "set-home",
            "terminal", "shell", "compress", "usage", "reload-mcp",
            "cron", "invokeai-defaults", "invokeai_defaults",
            "wiki-host", "wiki_host"
        }
        if command and command in _known_commands:
            await self.hooks.emit(f"command:{command}", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "command": command,
                "args": event.get_command_args().strip(),
            })

        if command in ["new", "reset"]:
            return await self._handle_reset_command(event)
        
        if command == "help":
            return await self._handle_help_command(event)
        
        if command == "status":
            return await self._handle_status_command(event)
        
        if command == "stop":
            return await self._handle_stop_command(event)
        
        if command == "model":
            return await self._handle_model_command(event)

        if command in ["invokeai-defaults", "invokeai_defaults"]:
            return await self._handle_image_defaults_command(event)

        if command in ["wiki-host", "wiki_host"]:
            return await self._handle_wiki_host_command(event)
        
        if command == "personality":
            return await self._handle_personality_command(event)
        
        if command in ["terminal", "shell"]:
            return await self._handle_terminal_command(event)
        
        if command == "retry":
            return await self._handle_retry_command(event)
        
        if command == "undo":
            return await self._handle_undo_command(event)
        
        if command in ["sethome", "set-home"]:
            return await self._handle_set_home_command(event)

        if command == "compress":
            return await self._handle_compress_command(event)

        if command == "usage":
            return await self._handle_usage_command(event)

        if command == "reload-mcp":
            return await self._handle_reload_mcp_command(event)

        if command == "cron":
            return await self._handle_cron_command(event)
        
        # Skill slash commands: /skill-name loads the skill and sends to agent
        if command:
            try:
                from agent.skill_commands import get_skill_commands, build_skill_invocation_message
                skill_cmds = get_skill_commands()
                cmd_key = f"/{command}"
                if cmd_key in skill_cmds:
                    user_instruction = event.get_command_args().strip()
                    msg = build_skill_invocation_message(cmd_key, user_instruction)
                    if msg:
                        event.text = msg
                        # Fall through to normal message processing with skill content
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)
        
        # Check for pending exec approval responses
        session_key_preview = build_session_key(source)
        if session_key_preview in self._pending_approvals:
            user_text = event.text.strip().lower()
            if user_text in ("yes", "y", "approve", "ok", "go", "do it"):
                approval = self._pending_approvals.pop(session_key_preview)
                cmd = approval["command"]
                pattern_key = approval.get("pattern_key", "")
                logger.info("User approved dangerous command: %s...", cmd[:60])
                from tools.terminal_tool import terminal_tool
                from tools.approval import approve_session
                approve_session(session_key_preview, pattern_key)
                result = terminal_tool(command=cmd, force=True)
                return f"✅ Command approved and executed.\n\n```\n{result[:3500]}\n```"
            elif user_text in ("no", "n", "deny", "cancel", "nope"):
                self._pending_approvals.pop(session_key_preview)
                return "❌ Command denied."
            # If it's not clearly an approval/denial, fall through to normal processing
        
        # Get or create session
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        
        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
            session_entry.created_at == session_entry.updated_at
            or getattr(session_entry, "was_auto_reset", False)
        )
        if _is_new_session:
            await self.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })
        
        # Build session context
        context = build_session_context(source, self.config, session_entry)
        
        # Set environment variables for tools
        self._set_session_env(context)
        
        # Build the context prompt to inject
        context_prompt = build_session_context_prompt(context)
        
        # If the previous session expired and was auto-reset, prepend a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if getattr(session_entry, 'was_auto_reset', False):
            context_prompt = (
                "[System note: The user's previous session expired due to inactivity. "
                "This is a fresh conversation with no prior context.]\n\n"
                + context_prompt
            )
            session_entry.was_auto_reset = False
        
        # Load conversation history from transcript
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # First-message onboarding -- only on the very first interaction ever
        if not history and not self.session_store.has_any_sessions():
            context_prompt += (
                "\n\n[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
        
        # One-time prompt if no home channel is set for this platform
        if not history and source.platform and source.platform != Platform.LOCAL:
            platform_name = source.platform.value
            env_key = f"{platform_name.upper()}_HOME_CHANNEL"
            if not os.getenv(env_key):
                adapter = self.adapters.get(source.platform)
                if adapter:
                    await adapter.send(
                        source.chat_id,
                        f"📬 No home channel is set for {platform_name.title()}. "
                        f"A home channel is where Hermes delivers cron job results "
                        f"and cross-platform messages.\n\n"
                        f"Type /sethome to make this chat your home channel, "
                        f"or ignore to skip."
                    )
        
        # -----------------------------------------------------------------
        # Auto-analyze images sent by the user
        #
        # If the user attached image(s), we run the vision tool eagerly so
        # the conversation model always receives a text description.  The
        # local file path is also included so the model can re-examine the
        # image later with a more targeted question via vision_analyze.
        #
        # We filter to image paths only (by media_type) so that non-image
        # attachments (documents, audio, etc.) are not sent to the vision
        # tool even when they appear in the same message.
        # -----------------------------------------------------------------
        message_text = event.text or ""
        if event.media_urls:
            image_paths = []
            for i, path in enumerate(event.media_urls):
                # Check media_types if available; otherwise infer from message type
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                is_image = (
                    mtype.startswith("image/")
                    or event.message_type == MessageType.PHOTO
                )
                if is_image:
                    image_paths.append(path)
            if image_paths:
                message_text = await self._enrich_message_with_vision(
                    message_text, image_paths
                )
        
        # -----------------------------------------------------------------
        # Auto-transcribe voice/audio messages sent by the user
        # -----------------------------------------------------------------
        if event.media_urls:
            audio_paths = []
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                is_audio = (
                    mtype.startswith("audio/")
                    or event.message_type in (MessageType.VOICE, MessageType.AUDIO)
                )
                if is_audio:
                    audio_paths.append(path)
            if audio_paths:
                message_text = await self._enrich_message_with_transcription(
                    message_text, audio_paths
                )

        # -----------------------------------------------------------------
        # Enrich document messages with context notes for the agent
        # -----------------------------------------------------------------
        if event.media_urls and event.message_type == MessageType.DOCUMENT:
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                if not (mtype.startswith("application/") or mtype.startswith("text/")):
                    continue
                # Extract display filename by stripping the doc_{uuid12}_ prefix
                import os as _os
                basename = _os.path.basename(path)
                # Format: doc_<12hex>_<original_filename>
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                # Sanitize to prevent prompt injection via filenames
                import re as _re
                display_name = _re.sub(r'[^\w.\- ]', '_', display_name)

                if mtype.startswith("text/"):
                    context_note = (
                        f"[The user sent a text document: '{display_name}'. "
                        f"Its content has been included below. "
                        f"The file is also saved at: {path}]"
                    )
                else:
                    context_note = (
                        f"[The user sent a document: '{display_name}'. "
                        f"The file is saved at: {path}. "
                        f"Ask the user what they'd like you to do with it.]"
                    )
                message_text = f"{context_note}\n\n{message_text}"

        try:
            # Emit agent:start hook
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await self.hooks.emit("agent:start", hook_ctx)
            
            # Run the agent
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_entry.session_id,
                session_key=session_key
            )
            returned_session_id = str(agent_result.get("session_id") or "").strip()
            if returned_session_id and returned_session_id != session_entry.session_id:
                if self.session_store.update_session_id(session_entry.session_key, returned_session_id):
                    logger.info(
                        "Session handoff: %s -> %s (key=%s)",
                        session_entry.session_id,
                        returned_session_id,
                        session_entry.session_key,
                    )
                    session_entry.session_id = returned_session_id
                    hook_ctx["session_id"] = returned_session_id
                else:
                    logger.warning(
                        "Agent returned new session_id=%s but session mapping update failed for key=%s",
                        returned_session_id,
                        session_entry.session_key,
                    )

            response = agent_result.get("final_response", "")
            agent_messages = agent_result.get("messages", [])
            
            # Emit agent:end hook
            await self.hooks.emit("agent:end", {
                **hook_ctx,
                "response": (response or "")[:500],
            })
            
            # Check for pending process watchers (check_interval on background processes)
            try:
                from tools.process_registry import process_registry
                while process_registry.pending_watchers:
                    watcher = process_registry.pending_watchers.pop(0)
                    asyncio.create_task(self._run_process_watcher(watcher))
            except Exception as e:
                logger.error("Process watcher setup error: %s", e)

            # Check if the agent encountered a dangerous command needing approval
            try:
                from tools.approval import pop_pending
                pending = pop_pending(session_key)
                if pending:
                    self._pending_approvals[session_key] = pending
            except Exception as e:
                logger.debug("Failed to check pending approvals: %s", e)
            
            # Save the full conversation to the transcript, including tool calls.
            # This preserves the complete agent loop (tool_calls, tool results,
            # intermediate reasoning) so sessions can be resumed with full context
            # and transcripts are useful for debugging and training data.
            ts = datetime.now().isoformat()
            
            # If this is a fresh session (no history), write the full tool
            # definitions as the first entry so the transcript is self-describing
            # -- the same list of dicts sent as tools=[...] in the API request.
            if not history:
                tool_defs = agent_result.get("tools", [])
                self.session_store.append_to_transcript(
                    session_entry.session_id,
                    {
                        "role": "session_meta",
                        "tools": tool_defs or [],
                        "model": _resolve_gateway_model(),
                        "platform": source.platform.value if source.platform else "",
                        "timestamp": ts,
                    }
                )

            def _strip_ts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                normalized: List[Dict[str, Any]] = []
                for m in msgs or []:
                    if not isinstance(m, dict):
                        continue
                    if m.get("role") == "system":
                        continue
                    normalized.append({k: v for k, v in m.items() if k != "timestamp"})
                return normalized

            normalized_history = _strip_ts(history)
            normalized_agent = _strip_ts(agent_messages)
            history_prefix_matches = (
                len(normalized_agent) >= len(normalized_history)
                and normalized_agent[: len(normalized_history)] == normalized_history
            )
            history_was_rewritten = bool(normalized_history) and not history_prefix_matches

            # If agent history was compressed/rewritten, replace transcript with
            # the returned message list so future turns stay compact.
            if history_was_rewritten:
                rewritten: List[Dict[str, Any]] = []

                # Preserve an existing session_meta entry if present.
                existing_meta = next(
                    (m for m in (history or []) if isinstance(m, dict) and m.get("role") == "session_meta"),
                    None,
                )
                if existing_meta:
                    meta_entry = dict(existing_meta)
                    meta_entry.setdefault("timestamp", ts)
                    rewritten.append(meta_entry)

                for msg in normalized_agent:
                    entry = dict(msg)
                    entry.setdefault("timestamp", ts)
                    rewritten.append(entry)

                self.session_store.rewrite_transcript(session_entry.session_id, rewritten)
                self.session_store.update_session(session_entry.session_key)
                return response
            
            # Find only the NEW messages from this turn (skip history we loaded).
            # Use the filtered history length (history_offset) that was actually
            # passed to the agent, not len(history) which includes session_meta
            # entries that were stripped before the agent saw them.
            history_len = agent_result.get("history_offset", len(history))
            new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []
            
            # If no new messages found (edge case), fall back to simple user/assistant
            if not new_messages:
                self.session_store.append_to_transcript(
                    session_entry.session_id,
                    {"role": "user", "content": message_text, "timestamp": ts}
                )
                if response:
                    self.session_store.append_to_transcript(
                        session_entry.session_id,
                        {"role": "assistant", "content": response, "timestamp": ts}
                    )
            else:
                for msg in new_messages:
                    # Skip system messages (they're rebuilt each run)
                    if msg.get("role") == "system":
                        continue
                    # Add timestamp to each message for debugging
                    entry = {**msg, "timestamp": ts}
                    self.session_store.append_to_transcript(
                        session_entry.session_id, entry
                    )
            
            # Update session
            self.session_store.update_session(session_entry.session_key)
            
            return response
            
        except Exception as e:
            logger.exception("Agent error in session %s", session_key)
            return (
                "Sorry, I encountered an unexpected error. "
                "The details have been logged for debugging. "
                "Try again or use /reset to start a fresh session."
            )
        finally:
            # Clear session env
            self._clear_session_env()
    
    async def _handle_reset_command(self, event: MessageEvent) -> str:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self.session_store._generate_session_key(source)
        
        # Memory flush before reset: load the old transcript and let a
        # temporary agent save memories before the session is wiped.
        try:
            old_entry = self.session_store._entries.get(session_key)
            if old_entry:
                old_history = self.session_store.load_transcript(old_entry.session_id)
                if old_history:
                    from run_agent import AIAgent
                    loop = asyncio.get_event_loop()
                    _flush_kwargs = _resolve_runtime_agent_kwargs()
                    def _do_flush():
                        tmp_agent = AIAgent(
                            **_flush_kwargs,
                            max_iterations=5,
                            quiet_mode=True,
                            enabled_toolsets=["memory"],
                            session_id=old_entry.session_id,
                        )
                        # Build simple message list from transcript
                        msgs = []
                        for m in old_history:
                            role = m.get("role")
                            content = m.get("content")
                            if role in ("user", "assistant") and content:
                                msgs.append({"role": role, "content": content})
                        tmp_agent.flush_memories(msgs)
                    await loop.run_in_executor(None, _do_flush)
        except Exception as e:
            logger.debug("Gateway memory flush on reset failed: %s", e)
        
        # Reset the session
        new_entry = self.session_store.reset_session(session_key)
        
        # Emit session:reset hook
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })
        
        if new_entry:
            return "✨ Session reset! I've started fresh with no memory of our previous conversation."
        else:
            # No existing session, just create one
            self.session_store.get_or_create_session(source, force_new=True)
            return "✨ New session started!"
    
    async def _handle_status_command(self, event: MessageEvent) -> str:
        """Handle /status command."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        
        connected_platforms = [p.value for p in self.adapters.keys()]
        
        # Check if there's an active agent
        session_key = session_entry.session_key
        is_running = session_key in self._running_agents
        
        lines = [
            "📊 **Hermes Gateway Status**",
            "",
            f"**Session ID:** `{session_entry.session_id[:12]}...`",
            f"**Created:** {session_entry.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"**Last Activity:** {session_entry.updated_at.strftime('%Y-%m-%d %H:%M')}",
            f"**Tokens:** {session_entry.total_tokens:,}",
            f"**Agent Running:** {'Yes ⚡' if is_running else 'No'}",
            "",
            f"**Connected Platforms:** {', '.join(connected_platforms)}",
        ]
        
        return "\n".join(lines)
    
    async def _handle_stop_command(self, event: MessageEvent) -> str:
        """Handle /stop command - interrupt a running agent."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        
        if session_key in self._running_agents:
            agent = self._running_agents[session_key]
            agent.interrupt()
            return "⚡ Stopping the current task... The agent will finish its current step and respond."
        else:
            return "No active task to stop."
    
    async def _handle_help_command(self, event: MessageEvent) -> str:
        """Handle /help command - list available commands."""
        lines = [
            "📖 **Hermes Commands**\n",
            "`/new` — Start a new conversation",
            "`/reset` — Reset conversation history",
            "`/status` — Show session info",
            "`/stop` — Interrupt the running agent",
            "`/model [name]` — Show or change the model",
            "`/invokeai-defaults [model] [aspect]` — Show or change default InvokeAI image settings",
            "`/wiki-host [enable|disable|status] [port]` — Toggle LAN hosting for the local wiki",
            "`/personality [name]` — Set a personality",
            "`/retry` — Retry your last message",
            "`/undo` — Remove the last exchange",
            "`/sethome` — Set this chat as the home channel",
            "`/compress` — Compress conversation context",
            "`/usage` — Show token usage for this session",
            "`/reload-mcp` — Reload MCP servers from config",
            "`/cron` — Manage scheduled jobs (`list`, `add`, `remove`, `run`)",
            "`/help` — Show this message",
        ]
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append(f"\n⚡ **Skill Commands** ({len(skill_cmds)} installed):")
                for cmd in sorted(skill_cmds):
                    lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
        except Exception:
            pass
        return "\n".join(lines)

    async def _handle_cron_command(self, event: MessageEvent) -> str:
        """Handle /cron command for scheduled jobs."""
        args = event.get_command_args().strip()

        if not args:
            # Show quick help + current jobs
            return await self._handle_cron_list_command()

        parts = args.split(maxsplit=1)
        subcommand = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcommand in ("list", "ls"):
            return await self._handle_cron_list_command()

        if subcommand == "add":
            if not rest:
                return (
                    "(._.) Usage: `/cron add <schedule> <prompt>`\n"
                    "Accepted schedules:\n"
                    "• `30m`, `2h`, `1d` (one-shot delay)\n"
                    "• `every 30m`, `every 2h` (recurring)\n"
                    "• `0 9 * * *` (cron expression)\n"
                    "• `2026-03-03T14:00:00` (one-shot timestamp)\n"
                    'Example: `/cron add 30m Remind me to take a break`\n'
                    'Example: `/cron add "every 2h" Check server health`\n'
                    'Example: `/cron add "0 9 * * *" Morning briefing`'
                )
            try:
                schedule, prompt = self._parse_cron_add_args(rest)
            except ValueError as e:
                return str(e)
            return await self._handle_cron_add_command(event, schedule=schedule, prompt=prompt)

        if subcommand in ("remove", "rm", "delete"):
            if not rest:
                return "(._.) Usage: `/cron remove <job_id>`"
            job_id = rest.split()[0]
            return await self._handle_cron_remove_command(job_id)

        if subcommand in ("run", "run_now", "run-now", "now"):
            if not rest:
                return "(._.) Usage: `/cron run <job_id>`"
            if subcommand == "run" and rest.lower().startswith("now "):
                rest = rest[4:].strip()
            job_id = rest.split()[0]
            return await self._handle_cron_run_command(event, job_id)

        if subcommand in ("help", "?"):
            return (
                "📖 **Cron Commands**\n"
                "`/cron list` — List jobs\n"
                "`/cron add <schedule> <prompt>` — Add job\n"
                "`/cron remove <job_id>` — Remove job\n"
                "`/cron run <job_id>` — Run one job immediately\n\n"
                "**Accepted schedules for `/cron add`:**\n"
                "• `30m`, `2h`, `1d` (one-shot delay)\n"
                "• `every 30m`, `every 2h` (recurring)\n"
                "• `0 9 * * *` (cron expression)\n"
                "• `2026-03-03T14:00:00` (one-shot timestamp)"
            )

        return (
            f"(._.) Unknown cron command: `{subcommand}`\n"
            "Available: list, add, remove, run"
        )

    def _parse_cron_add_args(self, args: str) -> tuple[str, str]:
        """Parse `/cron add` args into (schedule, prompt)."""
        rest = args.strip()
        if not rest:
            raise ValueError(
                "(._.) Usage: `/cron add <schedule> <prompt>`\n"
                "Accepted schedule examples: `30m`, `every 2h`, `0 9 * * *`, `2026-03-03T14:00:00`"
            )

        # Support quoted schedule like "every 2h" for spaces.
        if rest.startswith('"'):
            close_quote = rest.find('"', 1)
            if close_quote == -1:
                raise ValueError(
                    '(._.) Unmatched quote in schedule. Use `"every 2h"` or `30m`.\n'
                    "Accepted schedule examples: `30m`, `every 2h`, `0 9 * * *`, `2026-03-03T14:00:00`"
                )
            schedule = rest[1:close_quote].strip()
            prompt = rest[close_quote + 1 :].strip()
            if not prompt:
                raise ValueError("(._.) Please provide a prompt for the job.")
            return schedule, prompt

        # Support "every 2h" / "every 1d" without requiring quotes.
        lower_rest = rest.lower()
        if lower_rest.startswith("every "):
            parts = rest.split(maxsplit=2)
            if len(parts) < 3:
                raise ValueError(
                    "(._.) Usage: `/cron add <schedule> <prompt>`\n"
                    "For recurring jobs, use: `every 30m` or `every 2h`"
                )
            schedule = f"{parts[0]} {parts[1]}"
            prompt = parts[2].strip()
            if not prompt:
                raise ValueError("(._.) Please provide a prompt for the job.")
            return schedule, prompt

        # Support cron expressions without quoting when enough fields are present.
        parts = rest.split()
        if len(parts) >= 6 and all(
            re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]
        ):
            schedule = " ".join(parts[:5])
            prompt = " ".join(parts[5:]).strip()
            if not prompt:
                raise ValueError("(._.) Please provide a prompt for the job.")
            return schedule, prompt

        parts = rest.split(maxsplit=1)
        schedule = parts[0].strip()
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not schedule:
            raise ValueError(
                "(._.) Usage: `/cron add <schedule> <prompt>`\n"
                "Accepted schedule examples: `30m`, `every 2h`, `0 9 * * *`, `2026-03-03T14:00:00`"
            )
        if not prompt:
            raise ValueError("(._.) Please provide a prompt for the job.")
        return schedule, prompt

    async def _handle_cron_list_command(self) -> str:
        """List all active cron jobs for `/cron list`."""
        from cron.jobs import list_jobs

        jobs = list_jobs()
        if not jobs:
            return "No scheduled jobs. Use `/cron add <schedule> <prompt>` to create one."

        lines = ["Scheduled Jobs:\n"]
        for job in jobs:
            times = job["repeat"].get("times")
            completed = job["repeat"].get("completed", 0)
            repeat_str = "forever" if times is None else f"{completed}/{times}"
            prompt_preview = job["prompt"][:90] + ("..." if len(job["prompt"]) > 90 else "")
            lines.extend([
                f"ID: `{job['id']}`",
                f"Name: {job['name']}",
                f"Schedule: {job['schedule_display']} ({repeat_str})",
                f"Next run: {job.get('next_run_at', 'N/A')}",
                f"Prompt: {prompt_preview}",
                "",
            ])

        return "\n".join(lines)

    async def _handle_cron_add_command(
        self,
        event: MessageEvent,
        schedule: str,
        prompt: str,
    ) -> str:
        """Handle `/cron add` by creating a job from current chat context."""
        from cron.jobs import create_job

        origin = {
            "platform": event.source.platform.value if event.source.platform else "unknown",
            "chat_id": event.source.chat_id,
            "chat_name": event.source.chat_name,
        }

        try:
            job = create_job(prompt=prompt, schedule=schedule, origin=origin)
        except Exception as e:
            return f"⚠️ Failed to create cron job: {e}"

        return (
            f"Created cron job `{job['id']}`.\n"
            f"Name: {job['name']}\n"
            f"Schedule: {job['schedule_display']}\n"
            f"Next run: {job['next_run_at']}"
        )

    async def _handle_cron_remove_command(self, job_id: str) -> str:
        """Handle `/cron remove <job_id>`."""
        from cron.jobs import get_job, remove_job

        job = get_job(job_id)
        if not job:
            return f"(._.) Job not found: `{job_id}`"
        removed_name = job["name"]
        if remove_job(job_id):
            return f"Removed job `{removed_name}` (`{job_id}`)."
        return f"⚠️ Failed to remove job `{job_id}`"

    async def _handle_cron_run_command(self, event: MessageEvent, job_id: str) -> str:
        """Handle `/cron run <job_id>` for one-off immediate execution."""
        from cron.jobs import get_job
        import queue as _queue
        import time as _time

        job = get_job(job_id)
        if not job:
            return f"(._.) Job not found: `{job_id}`"

        source = event.source
        adapter = self.adapters.get(source.platform) if source else None
        job_name = job.get("name", job["id"])
        progress_queue: "_queue.Queue[str]" = _queue.Queue()
        progress_state = {
            "started_at": _time.monotonic(),
            "last_detail": "Calling model...",
            "tool_calls": 0,
            "updates": 0,
            "done": False,
        }
        heartbeat_faces = {
            "thinking": [
                "(·_·)",
                "(•‿•)",
                "(^‿^)",
                "(•ᴗ•)",
                "(ᵔ◡ᵔ)",
                "(^-^*)",
            ],
            "tool": [
                "(⌐■_■)",
                "(^_^)b",
                "(•̀ᴗ•́)و",
                "(｡•̀ᴗ-)✧",
                "(ง •_•)ง",
                "(๑•̀ㅂ•́)و",
            ],
            "steady": [
                "(o^▽^o)",
                "(≧◡≦)",
                "(¬‿¬)",
                "(づ｡◕‿‿◕｡)づ",
                "(づ￣ ³￣)づ",
                "(ﾉ◕ヮ◕)ﾉ",
                "(^o^)/",
            ],
        }
        progress_message_id: str | None = None

        def _set_progress_detail(detail: str, tool_call: bool = False) -> None:
            progress_state["last_detail"] = (detail or "Working...").strip()
            if tool_call:
                progress_state["tool_calls"] += 1
            progress_state["updates"] += 1
            progress_queue.put("__tick__")

        def _build_progress_message() -> str:
            elapsed = int(max(0, _time.monotonic() - progress_state["started_at"]))
            detail = (progress_state.get("last_detail") or "Working...").strip()
            if len(detail) > 180:
                detail = detail[:177] + "..."
            if str(detail).lower().startswith("💡"):
                pool = heartbeat_faces["thinking"]
            elif progress_state.get("tool_calls", 0) > 0:
                pool = heartbeat_faces["tool"]
            else:
                pool = heartbeat_faces["steady"]
            face = pool[elapsed % len(pool)]
            return (
                f"🛠️ {face} Cron job is running... ({elapsed}s)\n"
                f"Name: {job_name}\n"
                f"ID: {job['id']}\n"
                f"{detail}\n"
                f"Tools: {progress_state['tool_calls']} | Updates: {progress_state['updates']}"
            )

        def progress_callback(tool_name: str, preview: str = None, args: dict = None):
            if tool_name == "_thinking":
                _set_progress_detail(f"💡 thinking: {preview or 'working...'}", tool_call=False)
                return
            if preview:
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                _set_progress_detail(f"⚙️ {tool_name}: \"{preview}\"", tool_call=True)
                return
            _set_progress_detail(f"⚙️ {tool_name}...", tool_call=True)

        async def _manual_cron_progress_loop():
            nonlocal progress_message_id
            if not adapter or not source:
                return
            while True:
                try:
                    while True:
                        try:
                            progress_queue.get_nowait()
                        except _queue.Empty:
                            break
                    msg = _build_progress_message()
                    if progress_message_id is None:
                        result = await adapter.send(chat_id=source.chat_id, content=msg)
                        if result and result.success and result.message_id:
                            progress_message_id = result.message_id
                    else:
                        await adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=progress_message_id,
                            content=msg,
                        )
                    if progress_state.get("done"):
                        return
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    return
                except Exception:
                    await asyncio.sleep(1.0)

        if adapter and source:
            try:
                initial = await adapter.send(
                    chat_id=source.chat_id,
                    content=(
                        f"⏳ Cron job starting\n"
                        f"Name: {job_name}\n"
                        f"ID: {job['id']}\n"
                        f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ),
                )
                if initial and initial.success and initial.message_id:
                    progress_message_id = initial.message_id
            except Exception:
                pass

        progress_task = asyncio.create_task(_manual_cron_progress_loop())
        result = await asyncio.to_thread(self._run_cron_job_once, job, event, progress_callback)
        progress_state["done"] = True
        try:
            await progress_task
        except Exception:
            pass

        if adapter and source:
            try:
                status_msg = (
                    f"✅ Cron job completed\n"
                    f"Name: {result['job_name']}\n"
                    f"ID: {result['job_id']}\n"
                    f"Output: {result['output_file']}"
                ) if result.get("success") else (
                    f"❌ Cron job failed\n"
                    f"Name: {result['job_name']}\n"
                    f"ID: {result['job_id']}\n"
                    f"Error: {result.get('error')}\n"
                    f"Output: {result['output_file']}"
                )
                await adapter.send(chat_id=source.chat_id, content=status_msg)
            except Exception:
                pass

        if not result.get("success"):
            return (
                f"⚠️ Cron job `{result['job_id']}` failed while running now.\n"
                f"Error: `{result.get('error')}`\n"
                f"Output saved to: `{result['output_file']}`"
            )

        preview = (result.get("final_response") or "(No response)").strip()
        if len(preview) > 2800:
            preview = preview[:2775] + "..."
        return (
            f"Ran `{result['job_id']}` now.\n"
            f"Output saved to: `{result['output_file']}`\n\n"
            f"**Response:**\n{preview}"
        )

    def _run_cron_job_once(
        self,
        job: dict,
        event: MessageEvent,
        tool_progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """Execute one job in a blocking thread context."""
        from cron.jobs import mark_job_run
        from cron.scheduler import run_job, save_job_output

        job_ctx = dict(job)
        # If the job has no origin stored, pin run output to this chat.
        if not job_ctx.get("origin") and event.source:
            job_ctx["origin"] = {
                "platform": event.source.platform.value if event.source.platform else "unknown",
                "chat_id": event.source.chat_id,
                "chat_name": event.source.chat_name,
            }

        success, output, final_response, error = run_job(
            job_ctx,
            tool_progress_callback=tool_progress_callback,
        )

        output_file = "unknown"
        try:
            saved = save_job_output(job_ctx["id"], output)
            output_file = str(saved)
        except Exception:
            pass

        mark_job_run(job_ctx["id"], success, error)

        return {
            "success": success,
            "job_id": job_ctx["id"],
            "job_name": job_ctx.get("name", job_ctx["id"]),
            "output_file": output_file,
            "error": error,
            "final_response": final_response,
        }
    
    async def _handle_model_command(self, event: MessageEvent) -> str:
        """Handle /model command - show or change the current model."""
        import yaml

        args = event.get_command_args().strip()
        config_path = _hermes_home / 'config.yaml'

        # Resolve current model the same way the agent init does:
        # env vars first, then config.yaml always overrides.
        current = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or "anthropic/claude-opus-4.6"
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, str):
                    current = model_cfg
                elif isinstance(model_cfg, dict):
                    current = model_cfg.get("default", current)
        except Exception:
            pass

        if not args:
            return f"🤖 **Current model:** `{current}`\n\nTo change: `/model provider/model-name`"

        if "/" not in args:
            return (
                f"🤖 Invalid model format: `{args}`\n\n"
                f"Use `provider/model-name` format, e.g.:\n"
                f"• `anthropic/claude-sonnet-4`\n"
                f"• `google/gemini-2.5-pro`\n"
                f"• `openai/gpt-4o`"
            )

        # Write to config.yaml (source of truth), same pattern as CLI save_config_value.
        try:
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            if "model" not in user_config or not isinstance(user_config["model"], dict):
                user_config["model"] = {}
            user_config["model"]["default"] = args
            with open(config_path, "w", encoding="utf-8", newline="") as f:
                yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            return f"⚠️ Failed to save model change: {e}"

        # Also set env var so code reading it before the next agent init sees the update.
        os.environ["HERMES_MODEL"] = args

        return f"🤖 Model changed to `{args}`\n_(takes effect on next message)_"

    async def _handle_image_defaults_command(self, event: MessageEvent) -> str:
        """Handle /invokeai-defaults command - show or change InvokeAI image defaults."""
        aspect_presets = {
            "1:1": (1024, 1024),
            "16:9": (1344, 768),
            "9:16": (768, 1344),
            "4:3": (1152, 896),
            "3:4": (896, 1152),
        }

        args = event.get_command_args().strip()
        current = _get_image_generation_defaults()

        if not args:
            if current:
                return (
                    f"{_format_image_defaults(current)}\n"
                    "\nTo change these, use `/invokeai-defaults <model> <aspect_ratio>`."
                )
            return (
                "🖼️ No image defaults saved yet.\n\n"
                "Use `/invokeai-defaults <model> <aspect_ratio>`.\n"
                "Supported aspect ratios: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`."
            )

        try:
            tokens = shlex.split(args)
        except ValueError as e:
            return f"⚠️ Could not parse image defaults: {e}"

        if len(tokens) > 2:
            return (
                "⚠️ Too many arguments for `/invokeai-defaults`.\n\n"
                "Usage: `/invokeai-defaults <model> <aspect_ratio>`\n"
                "Example: `/invokeai-defaults \"Z-Image Turbo (quantized)\" 1:1`"
            )

        model = current.get("model") if current else None
        aspect_ratio = current.get("aspect_ratio") if current else None

        if len(tokens) == 1:
            token = tokens[0]
            if token in aspect_presets:
                aspect_ratio = token
            else:
                model = token
        elif len(tokens) == 2:
            model, aspect_ratio = tokens

        if not model:
            return (
                "⚠️ Please pick a model.\n\n"
                "The command can update just the aspect ratio if a model is already saved."
            )

        if not aspect_ratio:
            aspect_ratio = "1:1"

        if aspect_ratio not in aspect_presets:
            valid = ", ".join(f"`{key}`" for key in aspect_presets)
            return (
                f"⚠️ Unknown aspect ratio: `{aspect_ratio}`\n\n"
                f"Valid options: {valid}"
            )

        width, height = aspect_presets[aspect_ratio]

        try:
            _save_image_generation_defaults(
                model=model,
                aspect_ratio=aspect_ratio,
                width=width,
                height=height,
            )
        except Exception as e:
            return f"⚠️ Failed to save image defaults: {e}"

        os.environ["HERMES_IMAGE_DEFAULT_MODEL"] = model
        os.environ["HERMES_IMAGE_DEFAULT_ASPECT_RATIO"] = aspect_ratio
        os.environ["HERMES_IMAGE_DEFAULT_WIDTH"] = str(width)
        os.environ["HERMES_IMAGE_DEFAULT_HEIGHT"] = str(height)

        saved = {
            "model": model,
            "aspect_ratio": aspect_ratio,
            "width": width,
            "height": height,
        }
        return (
            f"✅ Saved image defaults.\n\n"
            f"{_format_image_defaults(saved)}\n"
            "\nThese will be used as the starting image settings after your next message unless you override them."
        )

    async def _handle_wiki_host_command(self, event: MessageEvent) -> str:
        """Handle /wiki-host command for LAN wiki hosting."""
        args = event.get_command_args().strip()
        if not args:
            return self._wiki_host_status_message()

        try:
            tokens = shlex.split(args)
        except ValueError as e:
            return f"⚠️ Could not parse wiki-host command: {e}"

        action = (tokens[0] or "").strip().lower()
        port_token = tokens[1].strip() if len(tokens) > 1 else ""

        if action in {"status", "show"}:
            return self._wiki_host_status_message()

        if action in {"disable", "off", "stop"}:
            self._stop_wiki_host()
            _save_wiki_host_config(enabled=False, port=None)
            return "📚 Wiki hosting disabled."

        if action in {"enable", "on", "start"}:
            port = self._wiki_host_port or int((_get_wiki_host_config().get("port") or 8008))
            if port_token:
                try:
                    port = int(port_token)
                except ValueError:
                    return f"⚠️ Invalid port: `{port_token}`"
            if not (1 <= int(port) <= 65535):
                return f"⚠️ Invalid port: `{port}`"

            try:
                return self._start_wiki_host(int(port))
            except OSError as e:
                return f"⚠️ Could not start wiki host on port `{port}`: {e}"
            except Exception as e:
                return f"⚠️ Failed to start wiki host: {e}"

        return (
            f"⚠️ Unknown wiki-host action: `{action}`\n\n"
            "Usage:\n"
            "- `/wiki-host status`\n"
            "- `/wiki-host enable 8008`\n"
            "- `/wiki-host disable`"
        )
    
    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """Handle /personality command - list or set a personality."""
        import yaml

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / 'config.yaml'

        try:
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                personalities = config.get("agent", {}).get("personalities", {})
            else:
                config = {}
                personalities = {}
        except Exception:
            config = {}
            personalities = {}

        if not personalities:
            return "No personalities configured in `~/.hermes/config.yaml`"

        if not args:
            lines = ["🎭 **Available Personalities**\n"]
            for name, prompt in personalities.items():
                preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                lines.append(f"• `{name}` — {preview}")
            lines.append(f"\nUsage: `/personality <name>`")
            return "\n".join(lines)

        if args in personalities:
            selected_prompt = personalities[args]
            # Backward-compatible legacy env var (not read by current runtime).
            os.environ["HERMES_PERSONALITY"] = selected_prompt
            # Runtime-consumed ephemeral prompt env var.
            os.environ["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = selected_prompt
            # Keep current GatewayRunner instance in sync immediately.
            self._ephemeral_system_prompt = selected_prompt

            # Persist as agent.system_prompt so restarts keep the selected personality.
            try:
                if config_path.exists():
                    with open(config_path, 'r', encoding='utf-8') as f:
                        persisted = yaml.safe_load(f) or {}
                else:
                    persisted = {}
                persisted.setdefault("agent", {})
                persisted["agent"]["system_prompt"] = selected_prompt
                with open(config_path, 'w', encoding='utf-8', newline='') as f:
                    yaml.dump(persisted, f, default_flow_style=False)
            except Exception as e:
                logger.warning("Failed to persist selected personality prompt: %s", e)

            return f"🎭 Personality set to **{args}**\n_(takes effect on next message)_"

        available = ", ".join(f"`{n}`" for n in personalities.keys())
        return f"Unknown personality: `{args}`\n\nAvailable: {available}"
    
    async def _handle_terminal_command(self, event: MessageEvent) -> str:
        """Handle /terminal command - show or change the current Windows shell mode.

        Syntax (Windows only):
          /terminal windows   -> HERMES_WINDOWS_SHELL=powershell
          /terminal wsl       -> HERMES_WINDOWS_SHELL=wsl
          /terminal auto      -> HERMES_WINDOWS_SHELL=auto (WSL > PowerShell > cmd)
          /terminal cmd       -> HERMES_WINDOWS_SHELL=cmd
        
        On non-Windows hosts this command is a no-op and reports that only POSIX
        shells are available.
        """
        args = event.get_command_args().strip().lower()

        if os.name != "nt":
            return (
                "Terminal mode only applies when the **gateway** runs on Windows. "
                "Right now the gateway is running on Linux/WSL, so local commands "
                "always use your default (POSIX) shell. Setting PowerShell here has no effect; "
                "to use PowerShell for commands, start the gateway from Windows (e.g. PowerShell or cmd)."
            )

        current = os.getenv("HERMES_WINDOWS_SHELL", "auto")
        if not args:
            return (
                "🖥️ **Current terminal mode:** "
                f"`{current}`\n\n"
                "Usage: `/terminal powershell`, `/terminal wsl`, `/terminal auto`, `/terminal cmd`"
            )

        mode_map = {
            "windows": "powershell",
            "pwsh": "powershell",
            "powershell": "powershell",
            "wsl": "wsl",
            "linux": "wsl",
            "auto": "auto",
            "cmd": "cmd",
            "cmd.exe": "cmd",
        }

        if args not in mode_map:
            return (
                f"Unknown terminal mode: `{args}`\n\n"
                "Valid options on Windows are: `powershell`, `wsl`, `auto`, `cmd` "
                "(aliases like `windows` and `pwsh` also map to `powershell`)."
            )

        new_value = mode_map[args]
        os.environ["HERMES_WINDOWS_SHELL"] = new_value
        logger.info(
            "Terminal mode switched to %s (HERMES_WINDOWS_SHELL=%s) by user",
            new_value,
            new_value,
        )

        # Persist into ~/.hermes/config.yaml so restarts keep the selected mode.
        try:
            import yaml
            config_path = Path.home() / ".hermes" / "config.yaml"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    persisted = yaml.safe_load(f) or {}
            else:
                persisted = {}

            # Top-level key so shell_utils can see it even without nested terminal config.
            persisted["HERMES_WINDOWS_SHELL"] = new_value

            with open(config_path, "w", encoding="utf-8", newline="") as f:
                yaml.dump(persisted, f, default_flow_style=False)
        except Exception as e:
            logger.warning("Failed to persist terminal mode to config.yaml: %s", e)

        human_mode = {
            "powershell": "PowerShell",
            "wsl": "WSL (Linux shell)",
            "cmd": "cmd.exe",
            "auto": "auto (WSL > PowerShell > cmd)",
        }.get(new_value, new_value)

        return (
            f"🖥️ Terminal mode set to **{human_mode}** "
            f"(`HERMES_WINDOWS_SHELL={new_value}`).\n"
            "Future *local* commands will use this shell. "
            "Remote/Unix environments (Docker/SSH/etc.) still use POSIX shells."
        )
    
    async def _handle_retry_command(self, event: MessageEvent) -> str:
        """Handle /retry command - re-send the last user message."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # Find the last user message
        last_user_msg = None
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_msg = history[i].get("content", "")
                last_user_idx = i
                break
        
        if not last_user_msg:
            return "No previous message to retry."
        
        # Truncate history to before the last user message and persist
        truncated = history[:last_user_idx]
        self.session_store.rewrite_transcript(session_entry.session_id, truncated)
        
        # Re-send by creating a fake text event with the old message
        retry_event = MessageEvent(
            text=last_user_msg,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=event.raw_message,
        )
        
        # Let the normal message handler process it
        await self._handle_message(retry_event)
        return None  # Response sent through normal flow
    
    async def _handle_undo_command(self, event: MessageEvent) -> str:
        """Handle /undo command - remove the last user/assistant exchange."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # Find the last user message and remove everything from it onward
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_idx = i
                break
        
        if last_user_idx is None:
            return "Nothing to undo."
        
        removed_msg = history[last_user_idx].get("content", "")
        removed_count = len(history) - last_user_idx
        self.session_store.rewrite_transcript(session_entry.session_id, history[:last_user_idx])
        
        preview = removed_msg[:40] + "..." if len(removed_msg) > 40 else removed_msg
        return f"↩️ Undid {removed_count} message(s).\nRemoved: \"{preview}\""
    
    async def _handle_set_home_command(self, event: MessageEvent) -> str:
        """Handle /sethome command -- set the current chat as the platform's home channel."""
        source = event.source
        platform_name = source.platform.value if source.platform else "unknown"
        chat_id = source.chat_id
        chat_name = source.chat_name or chat_id
        
        env_key = f"{platform_name.upper()}_HOME_CHANNEL"
        
        # Save to config.yaml
        try:
            import yaml
            config_path = _hermes_home / 'config.yaml'
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding='utf-8') as f:
                    user_config = yaml.safe_load(f) or {}
            user_config[env_key] = chat_id
            with open(config_path, 'w', encoding='utf-8', newline='') as f:
                yaml.dump(user_config, f, default_flow_style=False)
            # Also set in the current environment so it takes effect immediately
            os.environ[env_key] = str(chat_id)
        except Exception as e:
            return f"Failed to save home channel: {e}"
        
        return (
            f"✅ Home channel set to **{chat_name}** (ID: {chat_id}).\n"
            f"Cron jobs and cross-platform messages will be delivered here."
        )
    
    async def _handle_compress_command(self, event: MessageEvent) -> str:
        """Handle /compress command -- manually compress conversation context."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)

        if not history or len(history) < 4:
            return "Not enough conversation to compress (need at least 4 messages)."

        try:
            from run_agent import AIAgent
            from agent.model_metadata import estimate_messages_tokens_rough

            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                return "No provider configured -- cannot compress."

            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            original_count = len(msgs)
            approx_tokens = estimate_messages_tokens_rough(msgs)

            tmp_agent = AIAgent(
                **runtime_kwargs,
                max_iterations=4,
                quiet_mode=True,
                enabled_toolsets=["memory"],
                session_id=session_entry.session_id,
            )

            loop = asyncio.get_event_loop()
            compressed, _ = await loop.run_in_executor(
                None,
                lambda: tmp_agent._compress_context(msgs, "", approx_tokens=approx_tokens),
            )

            self.session_store.rewrite_transcript(session_entry.session_id, compressed)
            new_count = len(compressed)
            new_tokens = estimate_messages_tokens_rough(compressed)

            return (
                f"🗜️ Compressed: {original_count} → {new_count} messages\n"
                f"~{approx_tokens:,} → ~{new_tokens:,} tokens"
            )
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return f"Compression failed: {e}"

    async def _handle_usage_command(self, event: MessageEvent) -> str:
        """Handle /usage command -- show token usage for the session's last agent run."""
        source = event.source
        session_key = build_session_key(source)

        agent = self._running_agents.get(session_key)
        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = [
                "📊 **Session Token Usage**",
                f"Prompt (input): {agent.session_prompt_tokens:,}",
                f"Completion (output): {agent.session_completion_tokens:,}",
                f"Total: {agent.session_total_tokens:,}",
                f"API calls: {agent.session_api_calls}",
            ]
            ctx = agent.context_compressor
            if ctx.last_prompt_tokens:
                pct = ctx.last_prompt_tokens / ctx.context_length * 100 if ctx.context_length else 0
                lines.append(f"Context: {ctx.last_prompt_tokens:,} / {ctx.context_length:,} ({pct:.0f}%)")
            if ctx.compression_count:
                lines.append(f"Compressions: {ctx.compression_count}")
            return "\n".join(lines)

        # No running agent -- check session history for a rough count
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        if history:
            from agent.model_metadata import estimate_messages_tokens_rough
            msgs = [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
            approx = estimate_messages_tokens_rough(msgs)
            return (
                f"📊 **Session Info**\n"
                f"Messages: {len(msgs)}\n"
                f"Estimated context: ~{approx:,} tokens\n"
                f"_(Detailed usage available during active conversations)_"
            )
        return "No usage data available for this session."

    async def _handle_reload_mcp_command(self, event: MessageEvent) -> str:
        """Handle /reload-mcp command -- disconnect and reconnect all MCP servers."""
        loop = asyncio.get_event_loop()
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _load_mcp_config, _servers, _lock

            # Capture old server names before shutdown
            with _lock:
                old_servers = set(_servers.keys())

            # Read new config before shutting down, so we know what will be added/removed
            new_config = _load_mcp_config()
            new_server_names = set(new_config.keys())

            # Shutdown existing connections
            await loop.run_in_executor(None, shutdown_mcp_servers)

            # Reconnect by discovering tools (reads config.yaml fresh)
            new_tools = await loop.run_in_executor(None, discover_mcp_tools)

            # Compute what changed
            with _lock:
                connected_servers = set(_servers.keys())

            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers

            lines = ["🔄 **MCP Servers Reloaded**\n"]
            if reconnected:
                lines.append(f"♻️ Reconnected: {', '.join(sorted(reconnected))}")
            if added:
                lines.append(f"➕ Added: {', '.join(sorted(added))}")
            if removed:
                lines.append(f"➖ Removed: {', '.join(sorted(removed))}")
            if not connected_servers:
                lines.append("No MCP servers connected.")
            else:
                lines.append(f"\n🔧 {len(new_tools)} tool(s) available from {len(connected_servers)} server(s)")

            # Inject a message at the END of the session history so the
            # model knows tools changed on its next turn.  Appended after
            # all existing messages to preserve prompt-cache for the prefix.
            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[SYSTEM: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            try:
                session_entry = self.session_store.get_or_create_session(event.source)
                self.session_store.append_to_transcript(
                    session_entry.session_id, reload_msg
                )
            except Exception:
                pass  # Best-effort; don't fail the reload over a transcript write

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return f"❌ MCP reload failed: {e}"

    def _set_session_env(self, context: SessionContext) -> None:
        """Set environment variables for the current session."""
        os.environ["HERMES_SESSION_PLATFORM"] = context.source.platform.value
        os.environ["HERMES_SESSION_CHAT_ID"] = context.source.chat_id
        if context.source.chat_name:
            os.environ["HERMES_SESSION_CHAT_NAME"] = context.source.chat_name
    
    def _clear_session_env(self) -> None:
        """Clear session environment variables."""
        for var in ["HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_NAME"]:
            if var in os.environ:
                del os.environ[var]
    
    async def _enrich_message_with_vision(
        self,
        user_text: str,
        image_paths: List[str],
    ) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from tools.vision_tools import vision_analyze_tool
        import json as _json

        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result_json = await vision_analyze_tool(
                    image_url=path,
                    user_prompt=analysis_prompt,
                )
                result = _json.loads(result_json)
                if result.get("success"):
                    description = result.get("analysis", "")
                    enriched_parts.append(
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    enriched_parts.append(
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                enriched_parts.append(
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )

        # Combine: vision descriptions first, then the user's original text
        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> str:
        """
        Auto-transcribe user voice/audio messages using OpenAI Whisper API
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            The enriched message string with transcriptions prepended.
        """
        from tools.transcription_tools import transcribe_audio
        import asyncio

        enriched_parts = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path)
                if result["success"]:
                    transcript = result["transcript"]
                    enriched_parts.append(
                        f'[The user sent a voice message~ '
                        f'Here\'s what they said: "{transcript}"]'
                    )
                else:
                    error = result.get("error", "unknown error")
                    if "OPENAI_API_KEY" in error or "VOICE_TOOLS_OPENAI_KEY" in error:
                        enriched_parts.append(
                            "[The user sent a voice message but I can't listen "
                            "to it right now~ VOICE_TOOLS_OPENAI_KEY isn't set up yet "
                            "(';w;') Let them know!]"
                        )
                    else:
                        enriched_parts.append(
                            "[The user sent a voice message but I had trouble "
                            f"transcribing it~ ({error})]"
                        )
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append(
                    "[The user sent a voice message but something went wrong "
                    "when I tried to listen to it~ Let them know!]"
                )

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    async def _run_process_watcher(self, watcher: dict) -> None:
        """
        Periodically check a background process and push updates to the user.

        Runs as an asyncio task. Stays silent when nothing changed.
        Auto-removes when the process exits or is killed.
        """
        from tools.process_registry import process_registry

        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        session_key = watcher.get("session_key", "")
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")

        logger.debug("Process watcher started: %s (every %ss)", session_id, interval)

        last_output_len = 0
        while True:
            await asyncio.sleep(interval)

            session = process_registry.get(session_id)
            if session is None:
                break

            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len

            if session.exited:
                # Process finished -- deliver final update
                new_output = session.output_buffer[-1000:] if session.output_buffer else ""
                message_text = (
                    f"[Background process {session_id} finished with exit code {session.exit_code}~ "
                    f"Here's the final output:\n{new_output}]"
                )
                # Try to deliver to the originating platform
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        await adapter.send(chat_id, message_text)
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)
                break

            elif has_new_output:
                # New output available -- deliver status update
                new_output = session.output_buffer[-500:] if session.output_buffer else ""
                message_text = (
                    f"[Background process {session_id} is still running~ "
                    f"New output:\n{new_output}]"
                )
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        await adapter.send(chat_id, message_text)
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)

        logger.debug("Process watcher ended: %s", session_id)

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.
        
        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool
        
        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        from run_agent import AIAgent
        import queue
        
        # Determine toolset based on platform.
        # Check config.yaml for per-platform overrides, fallback to hardcoded defaults.
        default_toolset_map = {
            Platform.LOCAL: "hermes-cli",
            Platform.TELEGRAM: "hermes-telegram",
            Platform.DISCORD: "hermes-discord",
            Platform.WHATSAPP: "hermes-whatsapp",
            Platform.SLACK: "hermes-slack",
        }
        
        # Try to load platform_toolsets from config
        platform_toolsets_config = {}
        try:
            config_path = _hermes_home / 'config.yaml'
            if config_path.exists():
                import yaml
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = yaml.safe_load(f) or {}
                platform_toolsets_config = user_config.get("platform_toolsets", {})
        except Exception as e:
            logger.debug("Could not load platform_toolsets config: %s", e)
        
        # Map platform enum to config key
        platform_config_key = {
            Platform.LOCAL: "cli",
            Platform.TELEGRAM: "telegram",
            Platform.DISCORD: "discord",
            Platform.WHATSAPP: "whatsapp",
            Platform.SLACK: "slack",
        }.get(source.platform, "telegram")
        
        # Use config override if present (list of toolsets), otherwise hardcoded default
        config_toolsets = platform_toolsets_config.get(platform_config_key)
        if config_toolsets and isinstance(config_toolsets, list):
            enabled_toolsets = config_toolsets
        else:
            default_toolset = default_toolset_map.get(source.platform, "hermes-telegram")
            enabled_toolsets = [default_toolset]
        
        # Tool progress and log configuration
        _progress_cfg = {}
        try:
            _tp_cfg_path = _hermes_home / "config.yaml"
            if _tp_cfg_path.exists():
                import yaml as _tp_yaml
                with open(_tp_cfg_path, encoding="utf-8") as _tp_f:
                    _tp_data = _tp_yaml.safe_load(_tp_f) or {}
                _progress_cfg = _tp_data.get("display", {})
        except Exception:
            pass

        def _as_bool(val: Any, default: bool = False) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                v = val.strip().lower()
                if v in {"1", "true", "yes", "on"}:
                    return True
                if v in {"0", "false", "no", "off"}:
                    return False
            return default

        progress_mode = (
            _progress_cfg.get("tool_progress")
            or os.getenv("HERMES_TOOL_PROGRESS_MODE")
            or "all"
        )
        progress_style = (
            _progress_cfg.get("tool_progress_style")
            or os.getenv("HERMES_TOOL_PROGRESS_STYLE")
            or "single"
        ).strip().lower()
        if progress_style not in {"feed", "single"}:
            progress_style = "single"
        tool_progress_enabled = progress_mode != "off"
        try:
            progress_rolling_entries = int(
                _progress_cfg.get("tool_progress_rolling_entries")
                or os.getenv("HERMES_TOOL_PROGRESS_ROLLING_ENTRIES", "3")
            )
        except Exception:
            progress_rolling_entries = 3
        progress_rolling_entries = max(1, min(progress_rolling_entries, 8))
        try:
            progress_embed_max_chars = int(
                os.getenv("HERMES_TOOL_PROGRESS_EMBED_MAX_CHARS", "3800")
            )
        except Exception:
            progress_embed_max_chars = 3800
        progress_embed_max_chars = max(1200, min(progress_embed_max_chars, 3900))

        thread_logs_enabled = _as_bool(
            _progress_cfg.get("tool_thread_logs", os.getenv("HERMES_TOOL_THREAD_LOGS", "true")),
            default=True,
        )
        artifact_logs_enabled = _as_bool(
            _progress_cfg.get("tool_log_artifacts", os.getenv("HERMES_TOOL_LOG_ARTIFACTS", "true")),
            default=True,
        )

        # Discord only supports threads in server channels, not DMs.
        thread_logs_enabled = bool(
            thread_logs_enabled
            and source.platform == Platform.DISCORD
            and source.chat_type != "dm"
        )

        tool_log_path: Optional[Path] = None
        if artifact_logs_enabled:
            try:
                tool_log_dir = _hermes_home / "logs" / "tool_runs"
                tool_log_dir.mkdir(parents=True, exist_ok=True)
                tool_log_path = tool_log_dir / f"{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                tool_log_path.write_text(
                    f"session_id={session_id}\n"
                    f"session_key={session_key or ''}\n"
                    f"platform={source.platform.value if source.platform else ''}\n"
                    f"chat_id={source.chat_id}\n"
                    f"started_at={datetime.now().isoformat()}\n"
                    f"user_message={message}\n"
                    "----\n",
                    encoding="utf-8",
                )
            except Exception as e:
                logger.debug("Could not create tool log artifact file: %s", e)
                tool_log_path = None

        def _append_artifact(line: str) -> None:
            if not tool_log_path:
                return
            try:
                with tool_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line.rstrip() + "\n")
            except Exception as e:
                logger.debug("Tool log artifact append failed: %s", e)

        # Queue for progress/status ticks and detailed log entries (thread-safe)
        progress_queue = queue.Queue() if tool_progress_enabled else None
        detail_queue = queue.Queue() if tool_progress_enabled else None
        detail_backlog_max = max(
            50,
            int(os.getenv("HERMES_TOOL_DETAIL_BACKLOG_MAX", "400")),
        )
        thread_create_max_attempts = max(
            1,
            int(os.getenv("HERMES_TOOL_THREAD_CREATE_MAX_ATTEMPTS", "3")),
        )
        last_tool = [None]
        progress_state = {
            "started_at": time.monotonic(),
            "phase": "thinking",
            "last_detail": "Queued request...",
            "tool_calls": 0,
            "updates": 0,
            "recent_events": [],
        }
        heartbeat_faces_by_phase = {
            "starting": ["(^_^)/", "(^o^)/", "(o^▽^o)"],
            "thinking": ["(·_·)", "(•‿•)", "(^‿^)", "(ᵔ◡ᵔ)", "(^-^*)"],
            "tool": ["(⌐■_■)", "(^_^)b", "(•̀ᴗ•́)و", "(｡•̀ᴗ-)✧", "(ง •_•)ง"],
            "finalizing": ["(•ᴗ•)", "(≧◡≦)", "(¬‿¬)", "(づ｡◕‿‿◕｡)づ"],
            "default": ["(•‿•)", "(^_^)/", "(^‿^)"],
        }
        phase_labels = {
            "starting": "starting",
            "thinking": "thinking",
            "tool": "using tools",
            "finalizing": "finalizing",
        }
        phase_emojis = {
            "starting": "🚀",
            "thinking": "💡",
            "tool": "🛠️",
            "finalizing": "🧾",
        }

        def _push_recent_event(detail: str) -> None:
            text = (detail or "").strip()
            if not text:
                return
            ts = datetime.now().strftime("%H:%M:%S")
            event_line = f"`{ts}` {text}"
            recent_events = progress_state.setdefault("recent_events", [])
            if recent_events and recent_events[-1] == event_line:
                return
            recent_events.append(event_line)
            while len(recent_events) > progress_rolling_entries:
                recent_events.pop(0)

        def _set_progress_state(*, phase: str | None = None, detail: str | None = None, tool_call: bool = False):
            if phase:
                progress_state["phase"] = phase
            if detail:
                progress_state["last_detail"] = detail
                _push_recent_event(detail)
            if tool_call:
                progress_state["tool_calls"] += 1
            progress_state["updates"] += 1
            if progress_queue:
                progress_queue.put("__tick__")

        def _build_progress_message() -> str:
            elapsed = int(max(0, time.monotonic() - progress_state["started_at"]))
            phase = progress_state.get("phase", "thinking")
            pool = heartbeat_faces_by_phase.get(phase, heartbeat_faces_by_phase["default"])
            face = pool[elapsed % len(pool)]
            phase_text = phase_labels.get(phase, "working")
            phase_emoji = phase_emojis.get(phase, "⚙️")
            header = f"{phase_emoji} {face} Hermes is {phase_text}... ({elapsed}s)"
            footer = f"Tools: {progress_state['tool_calls']} | Updates: {progress_state['updates']}"

            events = list(progress_state.get("recent_events") or [])
            if not events:
                fallback = (progress_state.get("last_detail") or "Working...").strip()
                events = [f"`{datetime.now().strftime('%H:%M:%S')}` {fallback}"]

            def _render(lines: list[str]) -> str:
                body = "\n".join(f"• {line}" for line in lines)
                return f"{header}\n{body}\n{footer}"

            msg = _render(events[-progress_rolling_entries:])
            if len(msg) <= progress_embed_max_chars:
                return msg

            lines = events[-progress_rolling_entries:]
            while len(lines) > 1:
                lines = lines[1:]
                msg = _render(lines)
                if len(msg) <= progress_embed_max_chars:
                    return msg

            lone = lines[0] if lines else ""
            overhead = len(f"{header}\n• \n{footer}")
            available = max(80, progress_embed_max_chars - overhead)
            if len(lone) > available:
                lone = lone[: max(0, available - 24)] + "... [trimmed for embed]"
            return _render([lone])

        def _summarize_result_for_status(result_text: str, is_error: bool) -> str:
            text = (result_text or "").strip()
            if not text:
                return ""
            try:
                parsed = json.loads(text)
            except Exception:
                return text if is_error else ""

            if isinstance(parsed, dict):
                parts: list[str] = []
                if "exit_code" in parsed:
                    parts.append(f"exit={parsed.get('exit_code')}")
                err = parsed.get("error")
                if err:
                    parts.append(f"error={err}")
                stderr = parsed.get("stderr")
                if not err and is_error and isinstance(stderr, str) and stderr.strip():
                    parts.append(f"stderr={stderr.strip()}")
                if not parts and is_error:
                    message_text = parsed.get("message")
                    if isinstance(message_text, str) and message_text.strip():
                        parts.append(message_text.strip())
                return " | ".join(parts)
            return text if is_error else ""

        def progress_callback(tool_name: str, preview: str = None, args: dict = None):
            if not progress_queue:
                return

            # Tool completion event (emitted after each tool finishes).
            if tool_name == "_tool_result":
                payload = args or {}
                completed_tool = str(payload.get("tool") or preview or "tool")
                duration = payload.get("duration_seconds")
                is_error = bool(payload.get("is_error"))
                result_text = str(payload.get("result") or "").strip()
                status_emoji = "❌" if is_error else "✅"
                if isinstance(duration, (int, float)):
                    duration_text = f" in {float(duration):.2f}s"
                else:
                    duration_text = ""
                summary_suffix = _summarize_result_for_status(result_text, is_error)
                done_msg = f"{status_emoji} {completed_tool} finished{duration_text}"
                if summary_suffix:
                    done_msg = f"{done_msg} | {summary_suffix}"
                _set_progress_state(phase="tool", detail=done_msg, tool_call=False)

                detail_payload = f"{status_emoji} RESULT {completed_tool}{duration_text}"
                if result_text:
                    detail_payload = f"{detail_payload}\n{result_text}"
                ts = datetime.now().strftime("%H:%M:%S")
                if detail_queue:
                    detail_queue.put((ts, detail_payload))
                _append_artifact(f"[{datetime.now().isoformat()}] {detail_payload}")
                return

            if progress_mode == "new" and tool_name == last_tool[0]:
                return
            last_tool[0] = tool_name

            tool_emojis = {
                "terminal": "💻",
                "process": "⚙️",
                "web_search": "🔍",
                "web_extract": "📄",
                "read_file": "📖",
                "write_file": "✍️",
                "patch": "🔧",
                "search": "🔎",
                "search_files": "🔎",
                "list_directory": "📂",
                "image_generate": "🎨",
                "text_to_speech": "🔊",
                "browser_navigate": "🌐",
                "browser_click": "👆",
                "browser_type": "⌨️",
                "browser_snapshot": "📸",
                "browser_scroll": "📜",
                "browser_back": "◀️",
                "browser_press": "⌨️",
                "browser_close": "🚪",
                "browser_get_images": "🖼️",
                "browser_vision": "👁️",
                "moa_query": "🧠",
                "mixture_of_agents": "🧠",
                "vision_analyze": "👁️",
                "skill_view": "📚",
                "skills_list": "📋",
                "todo": "📋",
                "memory": "🧠",
                "session_search": "🔍",
                "send_message": "📨",
                "schedule_cronjob": "⏰",
                "list_cronjobs": "⏰",
                "remove_cronjob": "⏰",
                "execute_code": "🐍",
                "delegate_task": "🔀",
                "clarify": "❓",
                "skill_manage": "📝",
                "_thinking": "💡",
            }
            emoji = tool_emojis.get(tool_name, "⚙️")

            args_text = ""
            if args:
                try:
                    args_text = json.dumps(args, ensure_ascii=False, default=str)
                except Exception:
                    args_text = str(args)

            if preview:
                msg = f"{emoji} {tool_name}: \"{preview}\""
            else:
                msg = f"{emoji} {tool_name}..."

            live_detail = msg
            if args_text and tool_name != "_thinking":
                live_detail = f"{emoji} CALL {tool_name}\n{args_text}"

            if tool_name == "_thinking":
                _set_progress_state(phase="thinking", detail=f"💡 thinking: {preview or 'working...'}")
            else:
                _set_progress_state(phase="tool", detail=live_detail, tool_call=True)

            detail_payload = msg
            if args_text:
                detail_payload = f"{emoji} CALL {tool_name}\n{args_text}"

            if detail_queue:
                ts = datetime.now().strftime("%H:%M:%S")
                detail_queue.put((ts, detail_payload))
            _append_artifact(f"[{datetime.now().isoformat()}] {detail_payload}")

        async def send_progress_messages():
            if not progress_queue:
                return

            adapter = self.adapters.get(source.platform)
            if not adapter:
                return

            progress_message_id: str | None = None
            progress_thread_chat_id: str | None = None
            thread_announced = False
            thread_log_failed = False
            thread_create_attempts = 0
            detail_backlog: list[tuple[str, str]] = []
            dropped_detail_entries = 0
            last_heartbeat_at = 0.0
            last_rendered = ""

            while True:
                while True:
                    try:
                        progress_queue.get_nowait()
                    except queue.Empty:
                        break
                while detail_queue:
                    try:
                        entry = detail_queue.get_nowait()
                        if isinstance(entry, tuple) and len(entry) == 2:
                            ts, text = entry
                        else:
                            ts, text = datetime.now().strftime("%H:%M:%S"), str(entry)
                        if len(detail_backlog) >= detail_backlog_max:
                            dropped_detail_entries += 1
                            continue
                        detail_backlog.append((str(ts), str(text)))
                    except queue.Empty:
                        break
                if dropped_detail_entries:
                    note = (
                        f"[{datetime.now().isoformat()}] "
                        f"dropped_detail_entries={dropped_detail_entries} "
                        f"(backlog_limit={detail_backlog_max})"
                    )
                    _append_artifact(note)
                    dropped_detail_entries = 0

                try:
                    now = time.monotonic()
                    heartbeat_due = (now - last_heartbeat_at) >= 1.0

                    msg = _build_progress_message()

                    if heartbeat_due or msg != last_rendered:
                        if progress_style == "single" and progress_message_id and hasattr(adapter, "edit_message"):
                            try:
                                await adapter.edit_message(
                                    chat_id=source.chat_id,
                                    message_id=progress_message_id,
                                    content=msg,
                                )
                            except Exception:
                                result = await adapter.send(chat_id=source.chat_id, content=msg)
                                if result and result.success and result.message_id:
                                    progress_message_id = result.message_id
                        else:
                            result = await adapter.send(chat_id=source.chat_id, content=msg)
                            if result and result.success and result.message_id and progress_style == "single":
                                progress_message_id = result.message_id

                        last_rendered = msg
                        last_heartbeat_at = now
                        await adapter.send_typing(source.chat_id)

                    # Server-channel detailed logs: create thread from live status msg.
                    if (
                        thread_logs_enabled
                        and not thread_log_failed
                        and progress_thread_chat_id is None
                        and progress_message_id
                        and hasattr(adapter, "create_tool_log_thread")
                    ):
                        thread_create_attempts += 1
                        title = f"Hermes Tool Log {datetime.now().strftime('%H:%M:%S')}"
                        progress_thread_chat_id = await adapter.create_tool_log_thread(
                            chat_id=source.chat_id,
                            seed_message_id=progress_message_id,
                            title=title,
                        )
                        if progress_thread_chat_id and not thread_announced:
                            try:
                                await adapter.send(
                                    chat_id=source.chat_id,
                                    content=f"🧵 Detailed tool log: <#{progress_thread_chat_id}>",
                                )
                            except Exception:
                                pass
                            thread_announced = True
                            _append_artifact(
                                f"[{datetime.now().isoformat()}] thread_created={progress_thread_chat_id}"
                            )
                        elif (
                            not progress_thread_chat_id
                            and thread_create_attempts >= thread_create_max_attempts
                        ):
                            thread_log_failed = True
                            _append_artifact(
                                f"[{datetime.now().isoformat()}] "
                                f"thread_create_failed attempts={thread_create_attempts}"
                            )
                            detail_backlog.clear()
                            try:
                                await adapter.send(
                                    chat_id=source.chat_id,
                                    content="⚠️ Detailed tool log thread unavailable for this run. "
                                    "Progress is still shown here.",
                                )
                            except Exception:
                                pass

                    if detail_backlog:
                        if progress_thread_chat_id:
                            while detail_backlog:
                                ts, entry = detail_backlog.pop(0)
                                await adapter.send(
                                    chat_id=progress_thread_chat_id,
                                    content=f"`{ts}` {entry}",
                                )
                        elif not thread_logs_enabled or thread_log_failed:
                            detail_backlog.clear()

                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    if tool_log_path:
                        _append_artifact(f"[{datetime.now().isoformat()}] finished")
                        _append_artifact(f"log_file={tool_log_path}")
                    return
                except Exception as e:
                    logger.error("Progress message error: %s", e)
                    await asyncio.sleep(1)
        
        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        
        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_event_loop()
        _hooks_ref = self.hooks

        def _step_callback_sync(iteration: int, tool_names: list) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    _hooks_ref.emit("agent:step", {
                        "platform": source.platform.value if source.platform else "",
                        "user_id": source.user_id,
                        "session_id": session_id,
                        "iteration": iteration,
                        "tool_names": tool_names,
                    }),
                    _loop_for_step,
                )
            except Exception as _e:
                logger.debug("agent:step hook error: %s", _e)

        def run_sync():
            if tool_progress_enabled:
                _set_progress_state(phase="starting", detail="Preparing gateway session...")

            # Do NOT overwrite mode from config on every turn.
            # /terminal updates os.environ immediately; config is only persistence for restarts.
            if os.name == "nt":
                current_shell = os.environ.get("HERMES_WINDOWS_SHELL", "auto")
                logger.info(
                    "Agent run: using in-memory HERMES_WINDOWS_SHELL=%s",
                    current_shell,
                )
                # Force next get_local_shell_mode() call to emit a mode log entry.
                try:
                    import tools.environments.shell_utils as _sh
                    _sh._last_logged_mode = None
                except Exception:
                    pass

            # Pass session_key to process registry via env var so background
            # processes can be mapped back to this gateway session
            os.environ["HERMES_SESSION_KEY"] = session_key or ""

            # Read from env var or use default (same as CLI)
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "60"))
            max_tokens_env = os.getenv("HERMES_MAX_TOKENS", "").strip()
            if max_tokens_env == "0":
                max_tokens = None  # use model default
            elif max_tokens_env:
                max_tokens = int(max_tokens_env)
            else:
                max_tokens = 32768
            # Default 32768 when unset so long replies aren't cut off when the model
            # uses reasoning/thinking (which consumes tokens). Set HERMES_MAX_TOKENS=0
            # to use the model default; set to a number (e.g. 8192, 32768) to override.
            
            # Map platform enum to the platform hint key the agent understands.
            # Platform.LOCAL ("local") maps to "cli"; others pass through as-is.
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            
            # Combine platform context with user-configured ephemeral system prompt
            combined_ephemeral = context_prompt or ""
            if self._ephemeral_system_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + self._ephemeral_system_prompt).strip()

            # Re-read .env and config for fresh credentials (gateway is long-lived,
            # keys may change without restart). Use encoding-safe loader.
            try:
                load_dotenv_with_fallback(_env_path, override=True, logger=logging.getLogger(__name__))
            except Exception:
                pass

            model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or "anthropic/claude-opus-4.6"

            try:
                import yaml as _y
                _cfg_path = _hermes_home / "config.yaml"
                if _cfg_path.exists():
                    with open(_cfg_path, encoding="utf-8") as _f:
                        _cfg = _y.safe_load(_f) or {}
                    _model_cfg = _cfg.get("model", {})
                    if isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        model = _model_cfg.get("default", model)
                    # Apply persisted terminal mode so /terminal choice wins over .env.
                    # Otherwise load_dotenv(override=True) can overwrite in-memory wsl with .env's value.
                    _shell = _cfg.get("HERMES_WINDOWS_SHELL")
                    if isinstance(_shell, str) and _shell.strip():
                        os.environ["HERMES_WINDOWS_SHELL"] = _shell.strip().lower()
            except Exception:
                pass

            try:
                runtime_kwargs = _resolve_runtime_agent_kwargs()
            except Exception as exc:
                if tool_progress_enabled:
                    _set_progress_state(
                        phase="finalizing",
                        detail=f"❌ provider auth/config error: {exc}",
                        tool_call=False,
                    )
                return {
                    "final_response": f"⚠️ Provider authentication failed: {exc}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                    "session_id": session_id,
                }

            pr = self._provider_routing
            agent = AIAgent(
                model=model,
                **runtime_kwargs,
                max_iterations=max_iterations,
                max_tokens=max_tokens,
                quiet_mode=True,
                verbose_logging=False,
                enabled_toolsets=enabled_toolsets,
                ephemeral_system_prompt=combined_ephemeral or None,
                prefill_messages=self._prefill_messages or None,
                reasoning_config=self._reasoning_config,
                providers_allowed=pr.get("only"),
                providers_ignored=pr.get("ignore"),
                providers_order=pr.get("order"),
                provider_sort=pr.get("sort"),
                provider_require_parameters=pr.get("require_parameters", False),
                provider_data_collection=pr.get("data_collection"),
                session_id=session_id,
                tool_progress_callback=progress_callback if tool_progress_enabled else None,
                step_callback=_step_callback_sync if _hooks_ref.loaded_hooks else None,
                platform=platform_key,
                honcho_session_key=session_key,
                session_db=self._session_db,
            )
            
            # Store agent reference for interrupt support
            agent_holder[0] = agent
            # Capture the full tool definitions for transcript logging
            tools_holder[0] = agent.tools if hasattr(agent, 'tools') else None
            
            # Convert history to agent format.
            # Two cases:
            #   1. Normal path (from transcript): simple {role, content, timestamp} dicts
            #      - Strip timestamps, keep role+content
            #   2. Interrupt path (from agent result["messages"]): full agent messages
            #      that may include tool_calls, tool_call_id, reasoning, etc.
            #      - These must be passed through intact so the API sees valid
            #        assistant→tool sequences (dropping tool_calls causes 500 errors)
            agent_history = []
            for msg in history:
                role = msg.get("role")
                if not role:
                    continue
                
                # Skip metadata entries (tool definitions, session info)
                # -- these are for transcript logging, not for the LLM
                if role in ("session_meta",):
                    continue
                
                # Skip system messages -- the agent rebuilds its own system prompt
                if role == "system":
                    continue
                
                # Rich agent messages (tool_calls, tool results) must be passed
                # through intact so the API sees valid assistant→tool sequences
                has_tool_calls = "tool_calls" in msg
                has_tool_call_id = "tool_call_id" in msg
                is_tool_message = role == "tool"
                
                if has_tool_calls or has_tool_call_id or is_tool_message:
                    clean_msg = {k: v for k, v in msg.items() if k != "timestamp"}
                    agent_history.append(clean_msg)
                else:
                    # Simple text message - just need role and content
                    content = msg.get("content")
                    if content:
                        # Tag cross-platform mirror messages so the agent knows their origin
                        if msg.get("mirror"):
                            mirror_src = msg.get("mirror_source", "another session")
                            content = f"[Delivered from {mirror_src}] {content}"
                        agent_history.append({"role": role, "content": content})
            
            # Collect MEDIA paths already in history so we can exclude them
            # from the current turn's extraction. This is compression-safe:
            # even if the message list shrinks, we know which paths are old.
            _history_media_paths: set = set()
            for _hm in agent_history:
                if _hm.get("role") in ("tool", "function"):
                    _hc = _hm.get("content", "")
                    if "MEDIA:" in _hc:
                        for _match in re.finditer(r'MEDIA:(\S+)', _hc):
                            _p = _match.group(1).strip().rstrip('",}')
                            if _p:
                                _history_media_paths.add(_p)
            
            if tool_progress_enabled:
                _set_progress_state(phase="thinking", detail="Calling model...")
            result = agent.run_conversation(message, conversation_history=agent_history)
            result_holder[0] = result
            if tool_progress_enabled:
                _set_progress_state(phase="finalizing", detail="Preparing final response...")
            
            # Return final response, or a message if something went wrong
            final_response = result.get("final_response")
            if not final_response:
                error_msg = f"⚠️ {result['error']}" if result.get("error") else "(No response generated)"
                return {
                    "final_response": error_msg,
                    "messages": result.get("messages", []),
                    "api_calls": result.get("api_calls", 0),
                    "tools": tools_holder[0] or [],
                    "session_id": result.get("session_id") or getattr(agent, "session_id", session_id),
                }
            
            # Scan tool results for MEDIA:<path> tags that need to be delivered
            # as native audio/file attachments.  The TTS tool embeds MEDIA: tags
            # in its JSON response, but the model's final text reply usually
            # doesn't include them.  We collect unique tags from tool results and
            # append any that aren't already present in the final response, so the
            # adapter's extract_media() can find and deliver the files exactly once.
            #
            # Uses path-based deduplication against _history_media_paths (collected
            # before run_conversation) instead of index slicing. This is safe even
            # when context compression shrinks the message list. (Fixes #160)
            if "MEDIA:" not in final_response:
                media_tags = []
                has_voice_directive = False
                for msg in result.get("messages", []):
                    if msg.get("role") in ("tool", "function"):
                        content = msg.get("content", "")
                        if "MEDIA:" in content:
                            for match in re.finditer(r'MEDIA:(\S+)', content):
                                path = match.group(1).strip().rstrip('",}')
                                if path and path not in _history_media_paths:
                                    media_tags.append(f"MEDIA:{path}")
                            if "[[audio_as_voice]]" in content:
                                has_voice_directive = True
                
                if media_tags:
                    seen = set()
                    unique_tags = []
                    for tag in media_tags:
                        if tag not in seen:
                            seen.add(tag)
                            unique_tags.append(tag)

                    # Drop media tags that point to files no longer on disk.
                    # This prevents stale-session replay from repeatedly trying
                    # to send deleted TTS artifacts on startup/new turns.
                    existing_tags = []
                    missing_count = 0
                    for tag in unique_tags:
                        raw_path = tag.removeprefix("MEDIA:")
                        if os.path.exists(raw_path):
                            existing_tags.append(tag)
                        else:
                            missing_count += 1
                    if missing_count:
                        logger.warning(
                            "Dropped %d stale media tag(s) with missing file path(s)",
                            missing_count,
                        )
                    unique_tags = existing_tags

                    # Safety valve: cap auto-appended audio attachments to one
                    # per response (keep the most recent) to prevent TTS floods
                    # when the model triggers multiple text_to_speech calls.
                    audio_idx = []
                    for i, tag in enumerate(unique_tags):
                        path = tag.removeprefix("MEDIA:").lower()
                        if path.endswith((".ogg", ".opus", ".mp3", ".wav", ".m4a", ".webm")):
                            audio_idx.append(i)
                    if len(audio_idx) > 1:
                        keep = audio_idx[-1]
                        unique_tags = [
                            tag for i, tag in enumerate(unique_tags)
                            if i == keep or i not in audio_idx
                        ]
                        logger.warning(
                            "Capped auto media delivery: dropped %d extra audio tag(s)",
                            len(audio_idx) - 1,
                        )

                    if has_voice_directive:
                        unique_tags.insert(0, "[[audio_as_voice]]")
                    final_response = final_response + "\n" + "\n".join(unique_tags)
            
            return {
                "final_response": final_response,
                "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                "api_calls": result_holder[0].get("api_calls", 0) if result_holder[0] else 0,
                "tools": tools_holder[0] or [],
                "session_id": (
                    result_holder[0].get("session_id")
                    if isinstance(result_holder[0], dict)
                    else getattr(agent, "session_id", session_id)
                ),
                "history_offset": len(agent_history),
            }
        
        # Start progress message sender if enabled
        progress_task = None
        if tool_progress_enabled:
            _set_progress_state(phase="thinking", detail="Hermes is thinking...")
            progress_task = asyncio.create_task(send_progress_messages())
        
        # Track this agent as running for this session (for interrupt support)
        # We do this in a callback after the agent is created
        async def track_agent():
            # Wait for agent to be created
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if session_key:
                self._running_agents[session_key] = agent_holder[0]
        
        tracking_task = asyncio.create_task(track_agent())
        
        # Monitor for interrupts from the adapter (new messages arriving)
        async def monitor_for_interrupt():
            adapter = self.adapters.get(source.platform)
            if not adapter:
                return
            
            chat_id = source.chat_id
            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                # Check if adapter has a pending interrupt for this session
                if hasattr(adapter, 'has_pending_interrupt') and adapter.has_pending_interrupt(chat_id):
                    agent = agent_holder[0]
                    if agent:
                        pending_event = adapter.get_pending_message(chat_id)
                        pending_text = pending_event.text if pending_event else None
                        logger.debug("Interrupt detected from adapter, signaling agent...")
                        agent.interrupt(pending_text)
                        break
        
        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())
        
        try:
            # Run in thread pool to not block
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, run_sync)
            
            # Check if we were interrupted and have a pending message
            result = result_holder[0]
            adapter = self.adapters.get(source.platform)
            
            # Get pending message from adapter if interrupted
            pending = None
            if result and result.get("interrupted") and adapter:
                pending_event = adapter.get_pending_message(source.chat_id)
                if pending_event:
                    pending = pending_event.text
                elif result.get("interrupt_message"):
                    pending = result.get("interrupt_message")
            
            if pending:
                logger.debug("Processing interrupted message: '%s...'", pending[:40])
                
                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and source.chat_id in adapter._active_sessions:
                    adapter._active_sessions[source.chat_id].clear()
                
                # Don't send the interrupted response to the user — it's just noise
                # like "Operation interrupted." They already know they sent a new
                # message, so go straight to processing it.
                
                # Now process the pending message with updated history
                updated_history = result.get("messages", history)
                return await self._run_agent(
                    message=pending,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=source,
                    session_id=session_id,
                    session_key=session_key
                )
        finally:
            # Stop progress sender and interrupt monitor
            if progress_task:
                progress_task.cancel()
            interrupt_monitor.cancel()
            
            # Clean up tracking
            tracking_task.cancel()
            if session_key and session_key in self._running_agents:
                del self._running_agents[session_key]
            
            # Wait for cancelled tasks
            for task in [progress_task, interrupt_monitor, tracking_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        
        return response


def _start_cron_ticker(stop_event: threading.Event, adapters=None, interval: int = 60):
    """
    Background thread that ticks the cron scheduler at a regular interval.
    
    Runs inside the gateway process so cronjobs fire automatically without
    needing a separate `hermes cron daemon` or system cron entry.

    Also refreshes the channel directory every 5 minutes and prunes the
    image/audio/document cache once per hour.
    """
    from cron.scheduler import tick as cron_tick
    from gateway.platforms.base import cleanup_image_cache, cleanup_document_cache

    IMAGE_CACHE_EVERY = 60   # ticks — once per hour at default 60s interval
    CHANNEL_DIR_EVERY = 5    # ticks — every 5 minutes

    logger.info("Cron ticker started (interval=%ds)", interval)
    tick_count = 0
    while not stop_event.is_set():
        try:
            cron_tick(verbose=False)
        except Exception as e:
            logger.debug("Cron tick error: %s", e)

        tick_count += 1

        if tick_count % CHANNEL_DIR_EVERY == 0 and adapters:
            try:
                from gateway.channel_directory import build_channel_directory
                build_channel_directory(adapters)
            except Exception as e:
                logger.debug("Channel directory refresh error: %s", e)

        if tick_count % IMAGE_CACHE_EVERY == 0:
            try:
                removed = cleanup_image_cache(max_age_hours=24)
                if removed:
                    logger.info("Image cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Image cache cleanup error: %s", e)
            try:
                removed = cleanup_document_cache(max_age_hours=24)
                if removed:
                    logger.info("Document cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Document cache cleanup error: %s", e)

        stop_event.wait(timeout=interval)
    logger.info("Cron ticker stopped")


async def start_gateway(config: Optional[GatewayConfig] = None) -> bool:
    """
    Start the gateway and run until interrupted.
    
    This is the main entry point for running the gateway.
    Returns True if the gateway ran successfully, False if it failed to start.
    A False return causes a non-zero exit code so systemd can auto-restart.
    """
    # Configure rotating file log so gateway output is persisted for debugging.
    # On Windows, reconfigure stderr to UTF-8 so emoji/Unicode in log messages don't cause UnicodeEncodeError.
    if sys.stderr and getattr(sys.stderr, 'reconfigure', None) is not None:
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    log_dir = _hermes_home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / 'gateway.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8',
    )
    from agent.redact import RedactingFormatter
    file_handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)

    # Separate errors-only log for easy debugging
    error_handler = RotatingFileHandler(
        log_dir / 'errors.log',
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(error_handler)

    runner = GatewayRunner(config)
    
    # Set up signal handlers
    def signal_handler():
        asyncio.create_task(runner.stop())
    
    loop = asyncio.get_event_loop()
    # On POSIX, integrate SIGINT/SIGTERM with the event loop so a signal
    # triggers a graceful shutdown. On Windows we deliberately do NOT
    # override SIGINT handling here and instead rely on the default
    # KeyboardInterrupt behaviour in `hermes_cli.gateway.run_gateway`,
    # which already catches Ctrl+C and exits cleanly.
    if os.name != "nt":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass
    
    # Start the gateway
    success = await runner.start()
    if not success:
        return False
    
    # Write PID file so CLI can detect gateway is running
    import atexit
    from gateway.status import write_pid_file, remove_pid_file
    write_pid_file()
    atexit.register(remove_pid_file)
    
    # Start background cron ticker so scheduled jobs fire automatically
    cron_stop = threading.Event()
    cron_thread = threading.Thread(
        target=_start_cron_ticker,
        args=(cron_stop,),
        kwargs={"adapters": runner.adapters},
        daemon=True,
        name="cron-ticker",
    )
    cron_thread.start()
    
    # Wait for shutdown
    await runner.wait_for_shutdown()
    
    # Stop cron ticker cleanly
    cron_stop.set()
    cron_thread.join(timeout=5)

    # Close MCP server connections
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except Exception:
        pass

    return True


def main():
    """CLI entry point for the gateway."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Hermes Gateway - Multi-platform messaging")
    parser.add_argument("--config", "-c", help="Path to gateway config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    config = None
    if args.config:
        import json
        with open(args.config, encoding="utf-8") as f:
            data = json.load(f)
            config = GatewayConfig.from_dict(data)
    
    # Run the gateway - exit with code 1 if no platforms connected,
    # so systemd Restart=on-failure will retry on transient errors (e.g. DNS)
    success = asyncio.run(start_gateway(config))
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
