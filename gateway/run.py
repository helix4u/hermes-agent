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
import base64
import contextlib
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

# ---------------------------------------------------------------------------
# SSL certificate auto-detection for NixOS and other non-standard systems.
# Must run BEFORE any HTTP library (discord, aiohttp, etc.) is imported.
# ---------------------------------------------------------------------------
def _ensure_ssl_certs() -> None:
    """Set SSL_CERT_FILE if the system doesn't expose CA certs to Python."""
    if "SSL_CERT_FILE" in os.environ:
        return  # user already configured it

    import ssl

    # 1. Python's compiled-in defaults
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return

    # 2. certifi (ships its own Mozilla bundle)
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        return
    except ImportError:
        pass

    # 3. Common distro / macOS locations
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",               # Debian/Ubuntu/Gentoo
        "/etc/pki/tls/certs/ca-bundle.crt",                 # RHEL/CentOS 7
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", # RHEL/CentOS 8+
        "/etc/ssl/ca-bundle.pem",                            # SUSE/OpenSUSE
        "/etc/ssl/cert.pem",                                 # Alpine / macOS
        "/etc/pki/tls/cert.pem",                             # Fedora
        "/usr/local/etc/openssl@1.1/cert.pem",               # macOS Homebrew Intel
        "/opt/homebrew/etc/openssl@1.1/cert.pem",            # macOS Homebrew ARM
    ):
        if os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return

_ensure_ssl_certs()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Resolve Hermes home directory (respects HERMES_HOME override)
from hermes_constants import get_hermes_home

_hermes_home = get_hermes_home()
_DEFAULT_SIDECAR_SYNC_TIMEOUT_SECONDS = 300.0


def _load_sidecar_sync_timeout_seconds() -> float:
    raw = (os.getenv("HERMES_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_SIDECAR_SYNC_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid HERMES_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS=%r. Falling back to %.1f.",
            raw,
            _DEFAULT_SIDECAR_SYNC_TIMEOUT_SECONDS,
        )
        return _DEFAULT_SIDECAR_SYNC_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_SIDECAR_SYNC_TIMEOUT_SECONDS


_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS = _load_sidecar_sync_timeout_seconds()

# Mirror agent tool/thinking lines into the browser bridge poll payload (sidecar UI parity).
_BROWSER_BRIDGE_PROGRESS_EVENT_CAP = 128


def _format_timeout_seconds(value: float) -> str:
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"

# Load environment variables from ~/.hermes/.env first.
# User-managed env files should override stale shell exports on restart.
from dotenv import load_dotenv  # backward-compat for tests that monkeypatch this symbol
from hermes_cli.env_loader import load_hermes_dotenv
_env_path = _hermes_home / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / '.env')

# Bridge config.yaml values into the environment so os.getenv() picks them up.
# config.yaml is authoritative for terminal settings — overrides .env.
_config_path = _hermes_home / 'config.yaml'
if _config_path.exists():
    try:
        import yaml as _yaml
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        # Expand ${ENV_VAR} references before bridging to env vars.
        from hermes_cli.config import _expand_env_vars
        _cfg = _expand_env_vars(_cfg)
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
                "daytona_image": "TERMINAL_DAYTONA_IMAGE",
                "ssh_host": "TERMINAL_SSH_HOST",
                "ssh_user": "TERMINAL_SSH_USER",
                "ssh_port": "TERMINAL_SSH_PORT",
                "ssh_key": "TERMINAL_SSH_KEY",
                "container_cpu": "TERMINAL_CONTAINER_CPU",
                "container_memory": "TERMINAL_CONTAINER_MEMORY",
                "container_disk": "TERMINAL_CONTAINER_DISK",
                "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
                "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
                "sandbox_dir": "TERMINAL_SANDBOX_DIR",
                "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
            }
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    _val = _terminal_cfg[_cfg_key]
                    if isinstance(_val, list):
                        os.environ[_env_var] = json.dumps(_val)
                    else:
                        os.environ[_env_var] = str(_val)
        # Auxiliary model/direct-endpoint overrides (vision, web_extract).
        # Each task has provider/model/base_url/api_key; bridge non-default values to env vars.
        _auxiliary_cfg = _cfg.get("auxiliary", {})
        if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
            _aux_task_env = {
                "vision": {
                    "provider": "AUXILIARY_VISION_PROVIDER",
                    "model": "AUXILIARY_VISION_MODEL",
                    "base_url": "AUXILIARY_VISION_BASE_URL",
                    "api_key": "AUXILIARY_VISION_API_KEY",
                },
                "web_extract": {
                    "provider": "AUXILIARY_WEB_EXTRACT_PROVIDER",
                    "model": "AUXILIARY_WEB_EXTRACT_MODEL",
                    "base_url": "AUXILIARY_WEB_EXTRACT_BASE_URL",
                    "api_key": "AUXILIARY_WEB_EXTRACT_API_KEY",
                },
                "approval": {
                    "provider": "AUXILIARY_APPROVAL_PROVIDER",
                    "model": "AUXILIARY_APPROVAL_MODEL",
                    "base_url": "AUXILIARY_APPROVAL_BASE_URL",
                    "api_key": "AUXILIARY_APPROVAL_API_KEY",
                },
            }
            for _task_key, _env_map in _aux_task_env.items():
                _task_cfg = _auxiliary_cfg.get(_task_key, {})
                if not isinstance(_task_cfg, dict):
                    continue
                _prov = str(_task_cfg.get("provider", "")).strip()
                _model = str(_task_cfg.get("model", "")).strip()
                _base_url = str(_task_cfg.get("base_url", "")).strip()
                _api_key = str(_task_cfg.get("api_key", "")).strip()
                if _prov and _prov != "auto":
                    os.environ[_env_map["provider"]] = _prov
                if _model:
                    os.environ[_env_map["model"]] = _model
                if _base_url:
                    os.environ[_env_map["base_url"]] = _base_url
                if _api_key:
                    os.environ[_env_map["api_key"]] = _api_key
        _browser_cfg = _cfg.get("browser", {})
        if _browser_cfg and isinstance(_browser_cfg, dict):
            _browser_env_map = {
                "backend": "BROWSER_BACKEND",
                "inactivity_timeout": "BROWSER_INACTIVITY_TIMEOUT",
                "navigate_timeout": "BROWSER_NAVIGATE_TIMEOUT",
                "headless": "BROWSER_HEADLESS",
                "profile_dir": "BROWSER_PROFILE_DIR",
                "user_agent": "BROWSER_USER_AGENT",
                "cdp_url": "BROWSER_CDP_URL",
                "cdp_browser": "BROWSER_CDP_BROWSER",
                "cdp_port": "BROWSER_CDP_PORT",
                "cdp_user_data_dir": "BROWSER_CDP_USER_DATA_DIR",
            }
            for _cfg_key, _env_var in _browser_env_map.items():
                if _cfg_key in _browser_cfg:
                    _val = _browser_cfg[_cfg_key]
                    if isinstance(_val, str):
                        if _val.strip():
                            os.environ[_env_var] = _val.strip()
                    elif _val is not None:
                        os.environ[_env_var] = str(_val)
        _agent_cfg = _cfg.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "max_turns" in _agent_cfg:
                os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
            if "max_wall_clock_seconds" in _agent_cfg:
                os.environ["HERMES_MAX_WALL_CLOCK_SECONDS"] = str(_agent_cfg["max_wall_clock_seconds"])
        # Timezone: bridge config.yaml → HERMES_TIMEZONE env var.
        # HERMES_TIMEZONE from .env takes precedence (already in os.environ).
        _tz_cfg = _cfg.get("timezone", "")
        if _tz_cfg and isinstance(_tz_cfg, str) and "HERMES_TIMEZONE" not in os.environ:
            os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
        # Security settings
        _security_cfg = _cfg.get("security", {})
        if isinstance(_security_cfg, dict):
            _redact = _security_cfg.get("redact_secrets")
            if _redact is not None:
                os.environ["HERMES_REDACT_SECRETS"] = str(_redact).lower()
    except Exception:
        pass  # Non-fatal; gateway can still run with .env values

# Gateway runs in quiet mode - suppress debug output and use cwd directly (no temp dirs)
os.environ["HERMES_QUIET"] = "1"

# Enable interactive exec approval for dangerous commands on messaging platforms
os.environ["HERMES_EXEC_ASK"] = "1"

# Set terminal working directory for messaging platforms.
# If the user set an explicit path in config.yaml (not "." or "auto"),
# respect it. Otherwise use MESSAGING_CWD or default to home directory.
_configured_cwd = os.environ.get("TERMINAL_CWD", "")
if not _configured_cwd or _configured_cwd in (".", "auto", "cwd"):
    messaging_cwd = os.getenv("MESSAGING_CWD") or str(Path.home())
    os.environ["TERMINAL_CWD"] = messaging_cwd

from gateway.config import (
    Platform,
    GatewayConfig,
    load_gateway_config,
)
from gateway.session import (
    SessionStore,
    SessionSource,
    SessionEntry,
    SessionContext,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
)
from gateway.delivery import DeliveryRouter
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.browser_bridge import (
    BrowserBridgeConfig,
    BrowserBridgeServer,
    build_bridge_chat_id,
    build_browser_chat_message,
    build_browser_context_message,
    fetch_pdf_page_images,
    fetch_pdf_text,
)

logger = logging.getLogger(__name__)

# Sentinel placed into _running_agents immediately when a session starts
# processing, *before* any await.  Prevents a second message for the same
# session from bypassing the "already running" guard during the async gap
# between the guard check and actual agent creation.
_AGENT_PENDING_SENTINEL = object()


class BrowserBridgeTranscriptUnavailable(Exception):
    """Raised when a YouTube transcript exists but can't be fetched usefully."""

    def __init__(
        self,
        message: str,
        *,
        video_id: str = "",
        requested_language: str = "",
        available_languages: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.video_id = video_id
        self.requested_language = requested_language
        self.available_languages = list(available_languages or [])


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


def _platform_config_key(platform: "Platform") -> str:
    """Map a Platform enum to its config.yaml key (LOCAL→"cli", rest→enum value)."""
    return "cli" if platform == Platform.LOCAL else platform.value


def _is_wsl_runtime() -> bool:
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return False
    if os.getenv("WSL_DISTRO_NAME") or os.getenv("WSL_INTEROP"):
        return True
    try:
        release = os.uname().release
    except Exception:
        release = ""
    return "microsoft" in str(release or "").lower()


def _looks_like_local_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def _parse_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_windows_wsl_localhost_collision_warning(
    config: "GatewayConfig",
    *,
    os_name: str | None = None,
    is_wsl: bool | None = None,
) -> str | None:
    effective_os_name = os_name if os_name is not None else os.name
    effective_is_wsl = _is_wsl_runtime() if is_wsl is None else bool(is_wsl)
    if effective_os_name != "nt" and not effective_is_wsl:
        return None

    from gateway.browser_bridge import DEFAULT_HOST as _BB_DEFAULT_HOST
    from gateway.browser_bridge import DEFAULT_PORT as _BB_DEFAULT_PORT
    from gateway.platforms.api_server import DEFAULT_HOST as _API_DEFAULT_HOST
    from gateway.platforms.api_server import DEFAULT_PORT as _API_DEFAULT_PORT

    warnings: list[str] = []

    browser_bridge_enabled_raw = str(os.getenv("HERMES_BROWSER_BRIDGE_ENABLED", "true") or "").strip().lower()
    browser_bridge_enabled = browser_bridge_enabled_raw not in {"0", "false", "no", "off"}
    browser_bridge_host = str(os.getenv("HERMES_BROWSER_BRIDGE_HOST", _BB_DEFAULT_HOST) or "").strip() or _BB_DEFAULT_HOST
    browser_bridge_port = _parse_int_env("HERMES_BROWSER_BRIDGE_PORT", _BB_DEFAULT_PORT)
    if browser_bridge_enabled and _looks_like_local_bind_host(browser_bridge_host) and browser_bridge_port == _BB_DEFAULT_PORT:
        warnings.append(
            f"- Browser bridge is using default local bind `{browser_bridge_host}:{browser_bridge_port}`. "
            "Change `HERMES_BROWSER_BRIDGE_PORT` in one environment."
        )

    api_platform_cfg = (config.platforms or {}).get(Platform.API_SERVER)
    api_server_enabled = bool(api_platform_cfg and api_platform_cfg.enabled)
    if api_server_enabled:
        api_extra = api_platform_cfg.extra or {}
        api_host = str(api_extra.get("host", os.getenv("API_SERVER_HOST", _API_DEFAULT_HOST)) or "").strip() or _API_DEFAULT_HOST
        api_port_raw = api_extra.get("port", os.getenv("API_SERVER_PORT", str(_API_DEFAULT_PORT)))
        try:
            api_port = int(api_port_raw)
        except (TypeError, ValueError):
            api_port = _API_DEFAULT_PORT
        if _looks_like_local_bind_host(api_host) and api_port == _API_DEFAULT_PORT:
            warnings.append(
                f"- API server is using default local bind `{api_host}:{api_port}`. "
                "Change `API_SERVER_PORT` in one environment."
            )

    cdp_port = _parse_int_env("BROWSER_CDP_PORT", 9222)
    if 9222 <= cdp_port <= 9226:
        warnings.append(
            f"- Live CDP default is collision-prone at port `{cdp_port}`. "
            "Set `BROWSER_CDP_PORT` differently, or connect with `/browser connect <port>`."
        )

    if not warnings:
        return None

    runtime_label = "WSL" if effective_is_wsl and effective_os_name != "nt" else "Windows"
    body = "\n".join(warnings)
    return (
        f"{runtime_label} shares localhost with sibling Hermes instances across Windows/WSL. "
        "If you run Hermes in both environments at once, default local ports can collide:\n"
        f"{body}\n"
        "Suggested split: Windows keeps `8765/8642/9222`, WSL uses `8766/8643/9332`."
    )


def _load_gateway_config() -> dict:
    """Load and parse ~/.hermes/config.yaml, returning {} on any error."""
    try:
        config_path = _hermes_home / 'config.yaml'
        if config_path.exists():
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
    except Exception:
        logger.debug("Could not load gateway config from %s", _hermes_home / 'config.yaml')
    return {}


def _normalize_gateway_model_for_provider(model: str, provider: str) -> str:
    """Normalize model names for providers whose APIs reject aggregator slugs."""
    normalized_model = (model or "").strip()
    normalized_provider = (provider or "").strip().lower()

    if normalized_provider == "openai-codex" and "/" in normalized_model:
        return normalized_model.split("/", 1)[1].strip()

    return normalized_model


def _resolve_gateway_model(config: dict | None = None) -> str:
    """Read model from env/config — mirrors the resolution in _run_agent_sync.

    Without this, temporary AIAgent instances (memory flush, /compress) fall
    back to the hardcoded default ("anthropic/claude-opus-4.6") which fails
    when the active provider is openai-codex.
    """
    model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or ""
    cfg = config if config is not None else _load_gateway_config()
    requested_provider = ""
    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    if isinstance(model_cfg, str):
        model = model_cfg
    elif isinstance(model_cfg, dict):
        model = model_cfg.get("default") or model_cfg.get("model") or model
        cfg_provider = model_cfg.get("provider", "")
        if isinstance(cfg_provider, str) and cfg_provider.strip():
            requested_provider = cfg_provider.strip()

    if not requested_provider:
        requested_provider = os.getenv("HERMES_INFERENCE_PROVIDER", "").strip()

    try:
        from hermes_cli.auth import resolve_provider as _resolve_provider
        from hermes_cli.models import normalize_provider as _normalize_provider

        resolved_provider = _normalize_provider(requested_provider or "auto")
        if resolved_provider == "auto":
            resolved_provider = _resolve_provider(resolved_provider)
    except Exception:
        resolved_provider = (requested_provider or "").strip().lower()

    if not model:
        try:
            from hermes_cli.config import fallbacks_enabled as _fallbacks_enabled
            allow_fallbacks = _fallbacks_enabled(cfg if isinstance(cfg, dict) else None)
        except Exception:
            allow_fallbacks = True
        if allow_fallbacks:
            model = "anthropic/claude-opus-4.6"
        else:
            raise RuntimeError(
                "No model is configured for the gateway and implicit fallbacks are disabled. "
                "Set model.default explicitly in config.yaml."
            )

    return _normalize_gateway_model_for_provider(model, resolved_provider)


def _resolve_hermes_bin() -> Optional[list[str]]:
    """Resolve the Hermes update command as argv parts.

    Tries in order:
    1. ``shutil.which("hermes")`` — standard PATH lookup
    2. ``sys.executable -m hermes_cli.main`` — fallback when Hermes is running
       from a venv/module invocation and the ``hermes`` shim is not on PATH

    Returns argv parts ready for quoting/joining, or ``None`` if neither works.
    """
    import shutil

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]

    try:
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is not None:
            return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        pass

    return None


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

    defaults.update(
        {
            "provider": "invokeai-local-api",
            "model": model,
            "aspect_ratio": aspect_ratio,
            "width": int(width),
            "height": int(height),
        }
    )
    _save_user_config(config)


def _format_image_defaults(defaults: dict) -> str:
    """Human-readable summary of saved image defaults."""
    model = defaults.get("model", "")
    aspect_ratio = defaults.get("aspect_ratio", "1:1")
    width = defaults.get("width")
    height = defaults.get("height")
    lines = [
        "Image defaults",
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
    """Return the parent directory used for generated LAN wiki roots.

    Keep this outside workspace so agent file-editing scopes do not treat
    transient serving artifacts as wiki source content.
    """
    return _hermes_home / ".runtime" / "wiki_serve"


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

    candidates: List[str] = []
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


class _EncodingSafeStream:
    """Best-effort wrapper that degrades unencodable writes with replacement."""

    def __init__(self, stream: Any):
        self._stream = stream
        self.encoding = getattr(stream, "encoding", None)

    def write(self, data: str) -> Any:
        try:
            return self._stream.write(data)
        except UnicodeEncodeError:
            encoding = self.encoding or "utf-8"
            safe = str(data).encode(encoding, errors="replace").decode(encoding, errors="replace")
            return self._stream.write(safe)

    def flush(self) -> Any:
        return self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


_gateway_console_progress_lock = threading.Lock()


def _write_gateway_console_progress_line(text: str) -> None:
    """Best-effort direct console mirror for live gateway progress updates."""
    line = str(text or "").rstrip()
    if not line:
        return
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    safe_stream = stream
    if os.name == "nt":
        safe_stream = _EncodingSafeStream(stream)
    try:
        with _gateway_console_progress_lock:
            safe_stream.write(line + "\n")
            safe_stream.flush()
    except Exception:
        pass


def _harden_windows_console_logging() -> None:
    """Prevent logging UnicodeEncodeError on legacy Windows console codepages."""
    if os.name != "nt":
        return

    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.StreamHandler):
            continue
        # File handlers have their own encoding handling; only patch live streams.
        if isinstance(handler, RotatingFileHandler):
            continue

        stream = getattr(handler, "stream", None)
        if stream is None:
            continue

        reconfigured = False
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
                reconfigured = True
        except Exception:
            reconfigured = False

        if not reconfigured:
            try:
                handler.stream = _EncodingSafeStream(stream)
            except Exception:
                pass


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
        self._show_reasoning = self._load_show_reasoning()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()

        # Wire process registry into session store for reset protection
        from tools.process_registry import process_registry
        self.session_store = SessionStore(
            self.config.sessions_dir, self.config,
            has_active_processes_fn=lambda key: process_registry.has_active_for_session(key),
        )
        self.delivery_router = DeliveryRouter(self.config)
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._exit_cleanly = False
        self._exit_with_failure = False
        self._exit_reason: Optional[str] = None
        
        # Track running agents per session for interrupt support
        # Key: session_key, Value: AIAgent instance
        self._running_agents: Dict[str, Any] = {}
        self._pending_messages: Dict[str, str] = {}  # Queued messages during interrupt

        # Cache AIAgent instances per session to preserve prompt caching.
        # Without this, a new AIAgent is created per message, rebuilding the
        # system prompt (including memory) every turn — breaking prefix cache
        # and costing ~10x more on providers with prompt caching (Anthropic).
        # Key: session_key, Value: (AIAgent, config_signature_str)
        import threading as _threading
        self._agent_cache: Dict[str, tuple] = {}
        self._agent_cache_lock = _threading.Lock()

        # Track active fallback model/provider when primary is rate-limited.
        # Set after an agent run where fallback was activated; cleared when
        # the primary model succeeds again or the user switches via /model.
        self._effective_model: Optional[str] = None
        self._effective_provider: Optional[str] = None
        # Track pending exec approvals per session
        # Key: session_key, Value: {"command": str, "pattern_key": str, ...}
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}
        self._wiki_server: Optional[ThreadingHTTPServer] = None
        self._wiki_server_thread: Optional[threading.Thread] = None
        self._wiki_host_port: Optional[int] = None
        self._wiki_web_root: Optional[Path] = None
        self._update_notification_task: Optional[asyncio.Task] = None

        # Browser extension sidecar bridge state
        self._browser_bridge: Optional[BrowserBridgeServer] = None
        self._browser_bridge_tasks: Dict[str, asyncio.Task] = {}
        self._browser_bridge_progress: Dict[str, Dict[str, Any]] = {}
        self._browser_bridge_pending_interrupts: set[str] = set()

        # Track platforms that failed to connect for background reconnection.
        # Key: Platform enum, Value: {"config": platform_config, "attempts": int, "next_retry": float}
        self._failed_platforms: Dict[Platform, Dict[str, Any]] = {}

        # Persistent Honcho managers keyed by gateway session key.
        # This preserves write_frequency="session" semantics across short-lived
        # per-message AIAgent instances.
        self._honcho_managers: Dict[str, Any] = {}
        self._honcho_configs: Dict[str, Any] = {}

        # Ensure tirith security scanner is available (downloads if needed)
        try:
            from tools.tirith_security import ensure_installed
            ensure_installed(log_failures=False)
        except Exception:
            pass  # Non-fatal — fail-open at scan time if unavailable
        
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

        # Per-chat voice reply mode: "off" | "voice_only" | "all"
        self._voice_mode: Dict[str, str] = self._load_voice_modes()

        # Track background tasks to prevent garbage collection mid-execution
        self._background_tasks: set = set()

    def _get_or_create_gateway_honcho(self, session_key: str):
        """Return a persistent Honcho manager/config pair for this gateway session."""
        if not hasattr(self, "_honcho_managers"):
            self._honcho_managers = {}
        if not hasattr(self, "_honcho_configs"):
            self._honcho_configs = {}

        if session_key in self._honcho_managers:
            return self._honcho_managers[session_key], self._honcho_configs.get(session_key)

        try:
            from honcho_integration.client import HonchoClientConfig, get_honcho_client
            from honcho_integration.session import HonchoSessionManager

            hcfg = HonchoClientConfig.from_global_config()
            if not hcfg.enabled or not (hcfg.api_key or hcfg.base_url):
                return None, hcfg

            client = get_honcho_client(hcfg)
            manager = HonchoSessionManager(
                honcho=client,
                config=hcfg,
                context_tokens=hcfg.context_tokens,
            )
            self._honcho_managers[session_key] = manager
            self._honcho_configs[session_key] = hcfg
            return manager, hcfg
        except Exception as e:
            logger.debug("Gateway Honcho init failed for %s: %s", session_key, e)
            return None, None

    def _shutdown_gateway_honcho(self, session_key: str) -> None:
        """Flush and close the persistent Honcho manager for a gateway session."""
        managers = getattr(self, "_honcho_managers", None)
        configs = getattr(self, "_honcho_configs", None)
        if managers is None or configs is None:
            return

        manager = managers.pop(session_key, None)
        configs.pop(session_key, None)
        if not manager:
            return
        try:
            manager.shutdown()
        except Exception as e:
            logger.debug("Gateway Honcho shutdown failed for %s: %s", session_key, e)

    def _shutdown_all_gateway_honcho(self) -> None:
        """Flush and close all persistent Honcho managers."""
        managers = getattr(self, "_honcho_managers", None)
        if not managers:
            return
        for session_key in list(managers.keys()):
            self._shutdown_gateway_honcho(session_key)
    
    # -- Voice mode persistence ------------------------------------------

    _VOICE_MODE_PATH = _hermes_home / "gateway_voice_mode.json"

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        return {
            str(chat_id): mode
            for chat_id, mode in data.items()
            if mode in valid_modes
        }

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice off state into a live platform adapter."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        disabled_chats.clear()
        disabled_chats.update(
            chat_id for chat_id, mode in self._voice_mode.items() if mode == "off"
        )

    # -----------------------------------------------------------------

    def _flush_memories_for_session(
        self,
        old_session_id: str,
        honcho_session_key: Optional[str] = None,
    ):
        """Prompt the agent to save memories/skills before context is lost.

        Synchronous worker — meant to be called via run_in_executor from
        an async context so it doesn't block the event loop.
        """
        # Skip cron sessions — they run headless with no meaningful user
        # conversation to extract memories from.
        if old_session_id and old_session_id.startswith("cron_"):
            logger.debug("Skipping memory flush for cron session: %s", old_session_id)
            return

        try:
            history = self.session_store.load_transcript(old_session_id)
            if not history or len(history) < 4:
                return

            from run_agent import AIAgent
            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                return

            # Resolve model from config — AIAgent's default is OpenRouter-
            # formatted ("anthropic/claude-opus-4.6") which fails when the
            # active provider is openai-codex.
            model = _resolve_gateway_model()

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=8,
                quiet_mode=True,
                enabled_toolsets=["memory", "skills"],
                session_id=old_session_id,
                honcho_session_key=honcho_session_key,
            )
            # Fully silence the flush agent — quiet_mode only suppresses init
            # messages; tool call output still leaks to the terminal through
            # _safe_print → _print_fn.  Set a no-op to prevent that.
            tmp_agent._print_fn = lambda *a, **kw: None

            # Build conversation history from transcript
            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]

            # Read live memory state from disk so the flush agent can see
            # what's already saved and avoid overwriting newer entries.
            _current_memory = ""
            try:
                memory_module = sys.modules.get("tools.memory_tool")
                if memory_module is None:
                    from tools import memory_tool as memory_module
                memory_dir = Path(getattr(memory_module, "MEMORY_DIR"))
                for fname, label in [
                    ("MEMORY.md", "MEMORY (your personal notes)"),
                    ("USER.md", "USER PROFILE (who the user is)"),
                ]:
                    fpath = memory_dir / fname
                    if fpath.exists():
                        content = fpath.read_text(encoding="utf-8").strip()
                        if content:
                            _current_memory += f"\n\n## Current {label}:\n{content}"
            except Exception:
                pass  # Non-fatal — flush still works, just without the guard

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
            )

            if _current_memory:
                flush_prompt += (
                    "IMPORTANT — here is the current live state of memory. Other "
                    "sessions, cron jobs, or the user may have updated it since this "
                    "conversation ended. Do NOT overwrite or remove entries unless "
                    "the conversation above reveals something that genuinely "
                    "supersedes them. Only add new information that is not already "
                    "captured below."
                    f"{_current_memory}\n\n"
                )

            flush_prompt += (
                "Do NOT respond to the user. Just use the memory and skill_manage "
                "tools if needed, then stop.]"
            )

            tmp_agent.run_conversation(
                user_message=flush_prompt,
                conversation_history=msgs,
                sync_honcho=False,
            )
            logger.info("Pre-reset memory flush completed for session %s", old_session_id)
            # Flush any queued Honcho writes before the session is dropped
            if getattr(tmp_agent, '_honcho', None):
                try:
                    tmp_agent._honcho.shutdown()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Pre-reset memory flush failed for session %s: %s", old_session_id, e)

    async def _async_flush_memories(
        self,
        old_session_id: str,
        honcho_session_key: Optional[str] = None,
    ):
        """Run the sync memory flush in a thread pool so it won't block the event loop."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._flush_memories_for_session,
            old_session_id,
            honcho_session_key,
        )

    @property
    def should_exit_cleanly(self) -> bool:
        return self._exit_cleanly

    @property
    def should_exit_with_failure(self) -> bool:
        return self._exit_with_failure

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    def _session_key_for_source(self, source: SessionSource) -> str:
        """Resolve the current session key for a source, honoring gateway config when available."""
        if hasattr(self, "session_store") and self.session_store is not None:
            try:
                session_key = self.session_store._generate_session_key(source)
                if isinstance(session_key, str) and session_key:
                    return session_key
            except Exception:
                pass
        config = getattr(self, "config", None)
        return build_session_key(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
        )

    def _resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        from agent.smart_model_routing import resolve_turn_route

        primary = {
            "model": model,
            "api_key": runtime_kwargs.get("api_key"),
            "base_url": runtime_kwargs.get("base_url"),
            "provider": runtime_kwargs.get("provider"),
            "api_mode": runtime_kwargs.get("api_mode"),
            "command": runtime_kwargs.get("command"),
            "args": list(runtime_kwargs.get("args") or []),
        }
        return resolve_turn_route(user_message, getattr(self, "_smart_model_routing", {}), primary)

    @staticmethod
    def _is_browser_bridge_source(source: Optional[SessionSource]) -> bool:
        """Return True when the session source came from the browser sidecar bridge."""
        if not source:
            return False
        chat_id = str(getattr(source, "chat_id", "") or "")
        return source.platform == Platform.LOCAL and chat_id.startswith("browser-bridge:")

    def _build_browser_bridge_source(
        self,
        browser_label: str,
        client_session_id: str = "",
    ) -> SessionSource:
        """Build a stable local source for browser sidecar conversations."""
        label = str(browser_label or "").strip() or "Chrome Extension"
        chat_id = build_bridge_chat_id(label, str(client_session_id or "").strip())
        return SessionSource(
            platform=Platform.LOCAL,
            chat_id=chat_id,
            chat_name=label,
            chat_type="dm",
            user_id="local-browser",
            user_name=label,
        )

    def _resolve_browser_bridge_source(
        self,
        payload: Dict[str, Any],
    ) -> SessionSource:
        """Resolve sidecar source from payload session key or label/session-id fields."""
        requested_session_key = str(payload.get("sessionKey") or "").strip()
        if requested_session_key:
            try:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(requested_session_key)
                if not entry or not self._is_browser_bridge_source(entry.origin):
                    raise ValueError("Unknown browser sidecar session.")
                return entry.origin
            except Exception as exc:
                raise ValueError("Unknown browser sidecar session.") from exc

        browser_label = str(payload.get("browserLabel") or "").strip() or "Chrome Extension"
        client_session_id = str(payload.get("clientSessionId") or "").strip()
        return self._build_browser_bridge_source(browser_label, client_session_id)

    def _append_browser_bridge_progress_event(
        self,
        session_key: str,
        message: str,
        *,
        limit=None,
    ) -> List[str]:
        """Append a progress event and return the bounded event list (sidecar activity log)."""
        cap = (
            _BROWSER_BRIDGE_PROGRESS_EVENT_CAP
            if limit is None
            else max(1, min(int(limit), 500))
        )
        normalized = str(message or "").strip()
        if not normalized:
            normalized = "Working..."
        events = list(self._browser_bridge_progress.get(session_key, {}).get("recent_events") or [])
        stamp = datetime.now().strftime("%H:%M:%S")
        events.append(f"[{stamp}] {normalized}")
        if len(events) > cap:
            events = events[-cap:]
        return events

    def _wiki_host_status_message(self) -> str:
        """Human-readable current wiki hosting state."""
        if not self._wiki_server or not self._wiki_host_port:
            return (
                "Wiki hosting is currently disabled.\n\n"
                "Use `/wiki-host enable <port>` to expose the local wiki on your LAN."
            )

        lan_ip = _resolve_lan_ip()
        url = f"http://{lan_ip}:{self._wiki_host_port}/knowledge_wiki.html"
        web_root = self._wiki_web_root or _get_wiki_web_root_base()
        return (
            "Wiki hosting is enabled.\n"
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
        last_error: Optional[Exception] = None
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
        server = getattr(self, "_wiki_server", None)
        thread = getattr(self, "_wiki_server_thread", None)
        web_root = getattr(self, "_wiki_web_root", None)
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

    def _set_browser_bridge_progress(
        self,
        session_key: str,
        *,
        running: bool,
        detail: str = "",
        error: str = "",
        interrupt_requested: bool = False,
        recent_events: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Update in-memory sidecar progress state for one session."""
        state = dict(self._browser_bridge_progress.get(session_key, {}))
        now = time.time()
        if running and not state.get("started_at"):
            state["started_at"] = now
        if not running:
            state["finished_at"] = now
        state["running"] = bool(running)
        state["detail"] = str(detail or state.get("detail") or "").strip()
        state["error"] = str(error or "").strip()
        state["interrupt_requested"] = bool(interrupt_requested)
        if recent_events is not None:
            state["recent_events"] = list(recent_events)
        else:
            state["recent_events"] = list(state.get("recent_events") or [])
        if not running and not state["detail"]:
            state["detail"] = "Reply ready."
        self._browser_bridge_progress[session_key] = state
        return state

    def _get_browser_bridge_progress_snapshot(self, session_key: str) -> Dict[str, Any]:
        """Return serialized progress data for browser sidecar polling."""
        state = dict(self._browser_bridge_progress.get(session_key, {}))
        task = self._browser_bridge_tasks.get(session_key)
        if task and task.done():
            self._browser_bridge_tasks.pop(session_key, None)
            if state.get("running"):
                state["running"] = False
                state["detail"] = state.get("detail") or "Reply ready."
                state["finished_at"] = time.time()
                self._browser_bridge_progress[session_key] = state

        started_at = state.get("started_at")
        finished_at = state.get("finished_at")
        elapsed = 0
        if isinstance(started_at, (int, float)):
            end = finished_at if isinstance(finished_at, (int, float)) and not state.get("running") else time.time()
            elapsed = max(0, int(end - started_at))

        recent = list(state.get("recent_events") or [])
        return {
            "running": bool(state.get("running")),
            "detail": str(state.get("detail") or "").strip(),
            "error": str(state.get("error") or "").strip(),
            "interrupt_requested": bool(state.get("interrupt_requested")),
            "elapsed_seconds": elapsed,
            "recent_events": recent,
            # Alias for extension UI (full activity log; same backing store as recent_events).
            "activity_log": list(recent),
        }

    def _normalize_browser_bridge_public_host(self, host: str) -> str:
        normalized = str(host or "").strip()
        if normalized in {"0.0.0.0", "::", "[::]"}:
            return "127.0.0.1"
        return normalized or "127.0.0.1"

    def _build_browser_bridge_media_url(self, media_path: str) -> str:
        bridge = self._browser_bridge
        if not bridge:
            return ""
        try:
            resolved = Path(media_path).expanduser().resolve()
        except Exception:
            return ""
        if not resolved.exists() or not resolved.is_file():
            return ""
        mime_type = mimetypes.guess_type(str(resolved))[0] or ""
        if not mime_type.startswith("image/"):
            return ""
        host = self._normalize_browser_bridge_public_host(bridge.config.host)
        token = quote(bridge.config.token, safe="")
        encoded_path = quote(str(resolved), safe="")
        return f"http://{host}:{bridge.config.port}/media?path={encoded_path}&token={token}"

    @staticmethod
    def _extract_browser_bridge_content(content: Any) -> str:
        """Flatten message content from either plain strings or rich content blocks."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                    if "content" in item and isinstance(item["content"], str):
                        parts.append(item["content"])
            return "\n".join(part for part in parts if part).strip()
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            if isinstance(text, str):
                return text
        return str(content or "").strip()

    @staticmethod
    def _extract_browser_bridge_label(message: str, prefix: str) -> str:
        for line in str(message or "").splitlines():
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return ""

    @staticmethod
    def _extract_youtube_video_id(url_or_id: str) -> str:
        value = str(url_or_id or "").strip()
        if not value:
            return ""

        patterns = (
            r"(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})",
            r"^([a-zA-Z0-9_-]{11})$",
        )

        pending = [value]
        seen = set()

        while pending:
            candidate = str(pending.pop(0) or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)

            for pattern in patterns:
                match = re.search(pattern, candidate)
                if match:
                    return match.group(1)

            parsed = urlparse(candidate)
            host = parsed.netloc.lower()
            if not host:
                continue

            query = parse_qs(parsed.query or "", keep_blank_values=False)
            pending.extend(query.get("v", []))

            path_parts = [part for part in parsed.path.split("/") if part]
            if "youtu.be" in host and path_parts:
                pending.append(path_parts[0])
            if (
                ("youtube.com" in host or "youtube-nocookie.com" in host)
                and len(path_parts) >= 2
                and path_parts[0] in {"embed", "shorts", "live"}
            ):
                pending.append(path_parts[1])

        return ""

    @staticmethod
    def _run_gateway_python_install_command(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    @classmethod
    def _ensure_gateway_python_package(
        cls,
        *,
        import_name: str,
        package_spec: str,
    ) -> None:
        try:
            __import__(import_name)
            return
        except Exception:
            pass

        uv_exe = shutil.which("uv")
        if uv_exe:
            install = subprocess.run(
                [
                    uv_exe,
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    package_spec,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if install.returncode != 0:
                details = (install.stderr or install.stdout or "").strip()
                raise ValueError(
                    f"Failed to install {package_spec} in the Hermes gateway environment with uv pip."
                    + (f" Details: {details}" if details else "")
                )
        else:
            pip_probe = cls._run_gateway_python_install_command(["-m", "pip", "--version"], timeout=30)
            if pip_probe.returncode != 0:
                ensurepip = cls._run_gateway_python_install_command(["-m", "ensurepip", "--upgrade"], timeout=180)
                if ensurepip.returncode != 0:
                    details = (ensurepip.stderr or ensurepip.stdout or "").strip()
                    raise ValueError(
                        f"Failed to bootstrap pip in the Hermes gateway environment."
                        + (f" Details: {details}" if details else "")
                    )

            install = cls._run_gateway_python_install_command(
                [
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    package_spec,
                ],
                timeout=180,
            )
            if install.returncode != 0:
                details = (install.stderr or install.stdout or "").strip()
                raise ValueError(
                    f"Failed to install {package_spec} in the Hermes gateway environment."
                    + (f" Details: {details}" if details else "")
                )

        try:
            __import__(import_name)
        except Exception as exc:
            raise ValueError(
                f"{package_spec} install completed but import still failed."
            ) from exc

    @classmethod
    def _fetch_bridge_youtube_transcript(
        cls,
        url_or_id: str,
        language: str = "",
    ) -> Dict[str, Any]:
        """Fetch transcript text for browser bridge sidebar previews."""
        video_id = cls._extract_youtube_video_id(url_or_id)
        if not video_id:
            raise ValueError("A YouTube URL or video ID is required.")

        def _load_transcript_api():
            from youtube_transcript_api import YouTubeTranscriptApi

            return YouTubeTranscriptApi

        def _clean_language(value: str) -> str:
            return str(value or "").strip().replace("_", "-")

        def _build_language_preferences(requested_language: str, available_codes: List[str]) -> List[str]:
            requested = _clean_language(requested_language)
            lowered_available = {
                _clean_language(code).lower(): _clean_language(code)
                for code in available_codes
                if code
            }
            preferred: List[str] = []

            def _append(code: str) -> None:
                cleaned = _clean_language(code)
                if not cleaned:
                    return
                for existing in preferred:
                    if existing.lower() == cleaned.lower():
                        return
                preferred.append(cleaned)

            if requested:
                base = requested.split("-", 1)[0].lower()
                if base == "en":
                    _append("en")
                    if "en-us" in lowered_available:
                        _append(lowered_available["en-us"])
                    elif requested.lower() == "en-us":
                        _append("en-US")
                    for code in available_codes:
                        cleaned = _clean_language(code)
                        if cleaned.lower().split("-", 1)[0] == "en":
                            _append(cleaned)
                    if requested.lower() != "en":
                        _append(requested)
                else:
                    _append(requested)
                    for code in available_codes:
                        cleaned = _clean_language(code)
                        if cleaned.lower() == requested.lower():
                            _append(cleaned)
                    for code in available_codes:
                        cleaned = _clean_language(code)
                        if cleaned.lower().split("-", 1)[0] == base:
                            _append(cleaned)
            else:
                if "en" in lowered_available:
                    _append(lowered_available["en"])
                if "en-us" in lowered_available:
                    _append(lowered_available["en-us"])
                for code in available_codes:
                    cleaned = _clean_language(code)
                    if cleaned.lower().split("-", 1)[0] == "en":
                        _append(cleaned)
                for code in available_codes:
                    _append(code)

            return preferred

        def _extract_segments(fetched: Any) -> List[str]:
            segments: List[str] = []
            for segment in fetched:
                if isinstance(segment, dict):
                    text = str(segment.get("text") or "").strip()
                else:
                    text = str(getattr(segment, "text", "") or "").strip()
                if text:
                    segments.append(text)
            return segments

        try:
            YouTubeTranscriptApi = _load_transcript_api()
        except Exception:
            cls._ensure_gateway_python_package(
                import_name="youtube_transcript_api",
                package_spec="youtube-transcript-api>=1.2.0",
            )
            try:
                YouTubeTranscriptApi = _load_transcript_api()
            except Exception as exc:
                raise ValueError(
                    "youtube-transcript-api install completed but import still failed."
                ) from exc

        requested_language = _clean_language(language)
        api = YouTubeTranscriptApi()
        try:
            transcript_list = api.list(video_id) if hasattr(api, "list") else None
            available_codes: List[str] = []
            if transcript_list is not None:
                try:
                    available_codes = [
                        str(getattr(transcript, "language_code", "") or "").strip()
                        for transcript in list(transcript_list)
                        if str(getattr(transcript, "language_code", "") or "").strip()
                    ]
                except Exception:
                    available_codes = []
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise BrowserBridgeTranscriptUnavailable(
                message,
                video_id=video_id,
                requested_language=requested_language,
                available_languages=[],
            ) from exc

        languages = _build_language_preferences(requested_language, available_codes)
        matched_language = ""

        try:
            if transcript_list is not None:
                lookup_languages = languages or available_codes
                chosen_transcript = transcript_list.find_transcript(lookup_languages)
                matched_language = str(getattr(chosen_transcript, "language_code", "") or "").strip()
                segments = _extract_segments(chosen_transcript.fetch())
            elif hasattr(api, "fetch"):
                kwargs = {"languages": languages} if languages else {}
                segments = _extract_segments(api.fetch(video_id, **kwargs))
            else:
                raw_segments = (
                    YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
                    if languages
                    else YouTubeTranscriptApi.get_transcript(video_id)
                )
                segments = [
                    str(segment.get("text") or "").strip()
                    for segment in raw_segments
                    if isinstance(segment, dict) and str(segment.get("text") or "").strip()
                ]
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise BrowserBridgeTranscriptUnavailable(
                message,
                video_id=video_id,
                requested_language=requested_language,
                available_languages=available_codes,
            ) from exc

        full_text = " ".join(segments).strip()
        resolved_language = (
            matched_language
            or (languages[0] if languages else (available_codes[0] if available_codes else requested_language))
        )
        return {
            "video_id": video_id,
            "language": resolved_language,
            "segment_count": len(segments),
            "transcript_text": full_text,
            "char_count": len(full_text),
        }

    def _serialize_browser_bridge_history(
        self,
        history: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert transcript messages to sidepanel-friendly message entries."""
        result: List[Dict[str, Any]] = []
        for message in history:
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = self._extract_browser_bridge_content(message.get("content"))
            if not content.strip():
                continue

            media_paths = re.findall(r"MEDIA:(\S+)", content)
            cleaned_content = re.sub(r"\n?MEDIA:\S+\s*", "\n", content).strip()
            images = []
            for raw_path in media_paths:
                media_url = self._build_browser_bridge_media_url(raw_path.strip().rstrip('",}'))
                if not media_url:
                    continue
                images.append(
                    {
                        "source": "local",
                        "media_url": media_url,
                        "mime_type": mimetypes.guess_type(raw_path)[0] or "image/png",
                        "alt_text": Path(raw_path).name,
                        "local_path": raw_path,
                    }
                )

            kind = "chat"
            page_title = ""
            page_url = ""
            display_content = cleaned_content
            if role == "user" and content.startswith("[Injected browser context from the local Chrome extension]"):
                kind = "page_context"
                page_title = self._extract_browser_bridge_label(content, "- Title:")
                page_url = self._extract_browser_bridge_label(content, "- URL:")
                if "User request:" in content:
                    request_chunk = content.split("User request:", 1)[1].strip()
                    display_content = request_chunk.split("\n\n", 1)[0].strip()
                if not display_content:
                    display_content = "Shared browser page context."

            timestamp = message.get("timestamp")
            if isinstance(timestamp, (int, float)):
                timestamp_iso = datetime.fromtimestamp(timestamp).isoformat()
            else:
                timestamp_iso = str(timestamp or "").strip()

            result.append(
                {
                    "role": role,
                    "kind": kind,
                    "display_content": display_content,
                    "content": cleaned_content,
                    "page_title": page_title,
                    "page_url": page_url,
                    "images": images,
                    "timestamp": timestamp_iso,
                }
            )
        return result

    def _get_browser_bridge_session_snapshot(self, source: SessionSource) -> Dict[str, Any]:
        """Load current browser sidecar session state for extension polling."""
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        progress = self._get_browser_bridge_progress_snapshot(session_entry.session_key)
        return {
            "session_key": session_entry.session_key,
            "session_id": session_entry.session_id,
            "browser_label": source.chat_name or source.user_name or "Chrome Extension",
            "messages": self._serialize_browser_bridge_history(history),
            "progress": progress,
            # Browser sidecar sessions remain locally controllable while running:
            # send is still blocked by busy-state in the sidepanel, but interrupt
            # should stay available instead of downgrading the session to read-only.
            "can_send": True,
        }

    def _list_browser_bridge_sessions(
        self,
        *,
        limit: int = 25,
        preferred_session_key: str = "",
    ) -> Dict[str, Any]:
        """Return sidecar session list for the extension history picker."""
        self.session_store._ensure_loaded()
        entries = [
            entry
            for entry in self.session_store._entries.values()
            if self._is_browser_bridge_source(entry.origin)
        ]
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        sessions = []
        for entry in entries[: max(1, min(limit, 100))]:
            history = self.session_store.load_transcript(entry.session_id)
            rich = self._serialize_browser_bridge_history(history)
            last_message = rich[-1] if rich else {}
            progress = self._get_browser_bridge_progress_snapshot(entry.session_key)
            sessions.append(
                {
                    "session_key": entry.session_key,
                    "session_id": entry.session_id,
                    "browser_label": (entry.origin.chat_name or entry.origin.user_name or "Chrome Extension")
                    if entry.origin
                    else "Chrome Extension",
                    "updated_at": entry.updated_at.isoformat() if entry.updated_at else "",
                    "created_at": entry.created_at.isoformat() if entry.created_at else "",
                    "message_count": len(rich),
                    "last_message_role": last_message.get("role") or "",
                    "last_message_preview": (last_message.get("display_content") or "")[:140],
                    "running": bool(progress.get("running")),
                }
            )

        active_session_key = str(preferred_session_key or "").strip()
        if active_session_key and not any(s["session_key"] == active_session_key for s in sessions):
            active_session_key = ""
        if not active_session_key and sessions:
            active_session_key = sessions[0]["session_key"]

        return {
            "active_session_key": active_session_key,
            "sessions": sessions,
        }

    def _get_active_session_entry_by_session_id(self, session_id: str) -> Optional[SessionEntry]:
        self.session_store._ensure_loaded()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        for entry in self.session_store._entries.values():
            if entry.session_id == normalized_session_id:
                return entry
        return None

    def _get_active_session_entry_by_key_or_session_id(
        self,
        session_ref: str,
    ) -> Optional[SessionEntry]:
        self.session_store._ensure_loaded()
        normalized_session_ref = str(session_ref or "").strip()
        if not normalized_session_ref:
            return None

        active_entry = self.session_store._entries.get(normalized_session_ref)
        if active_entry:
            return active_entry

        return self._get_active_session_entry_by_session_id(normalized_session_ref)

    @staticmethod
    def _empty_sidebar_session_snapshot() -> Dict[str, Any]:
        return {
            "session_key": "",
            "session_id": "",
            "messages": [],
            "progress": {},
            "running": False,
            "browser_label": "",
            "source": "",
            "is_browser_session": False,
            "can_send": False,
        }

    @staticmethod
    def _format_sidebar_source_label(source_name: str) -> str:
        value = str(source_name or "").strip().lower()
        if value == "cli":
            return "CLI terminal"
        if value == "local":
            return "Local session"
        if not value:
            return "Hermes session"
        return value.replace("_", " ").title()

    def _format_sidebar_session_label(
        self,
        *,
        session_entry: Optional[SessionEntry] = None,
        session_record: Optional[Dict[str, Any]] = None,
    ) -> str:
        if session_entry and session_entry.origin:
            source = session_entry.origin
            if self._is_browser_bridge_source(source):
                return source.chat_name or source.user_name or session_entry.display_name or "Browser Sidecar"
            if source.platform == Platform.LOCAL and str(source.chat_id or "") == "cli":
                return session_entry.display_name or "CLI terminal"

            platform_label = self._format_sidebar_source_label(source.platform.value)
            target_label = (
                source.chat_name
                or source.user_name
                or session_entry.display_name
                or source.chat_id
                or ""
            )
            if target_label and target_label != platform_label:
                return f"{platform_label}: {target_label}"
            return platform_label

        session_record = session_record or {}
        return self._format_sidebar_source_label(session_record.get("source") or "")

    def _list_sidebar_sessions(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        db = self.session_store._db
        if not db:
            return self._list_browser_bridge_sessions(limit=limit).get("sessions", [])

        session_rows = db.search_sessions(limit=limit)
        sessions: List[Dict[str, Any]] = []
        for row in session_rows:
            session_id = str(row.get("id") or "").strip()
            if not session_id:
                continue

            active_entry = self._get_active_session_entry_by_session_id(session_id)
            active_origin = active_entry.origin if active_entry else None
            is_browser_session = bool(active_origin and self._is_browser_bridge_source(active_origin))
            progress = (
                self._get_browser_bridge_progress_snapshot(active_entry.session_key)
                if active_entry and is_browser_session
                else {}
            )
            updated_at = (
                active_entry.updated_at.isoformat()
                if active_entry and active_entry.updated_at
                else datetime.fromtimestamp(float(row.get("started_at") or 0)).isoformat()
                if row.get("started_at")
                else ""
            )

            sessions.append(
                {
                    "session_key": active_entry.session_key if active_entry else session_id,
                    "session_id": session_id,
                    "browser_label": self._format_sidebar_session_label(
                        session_entry=active_entry,
                        session_record=row,
                    ),
                    "source": str(row.get("source") or ""),
                    "updated_at": updated_at,
                    "created_at": updated_at,
                    "message_count": int(row.get("message_count") or 0),
                    "last_message_role": "",
                    "last_message_preview": "",
                    "running": bool(progress.get("running")),
                    "is_browser_session": is_browser_session,
                    "can_send": is_browser_session,
                }
            )

        return sessions

    def _get_sidebar_session_snapshot(self, session_ref: str) -> Dict[str, Any]:
        normalized_session_ref = str(session_ref or "").strip()
        if not normalized_session_ref:
            return self._empty_sidebar_session_snapshot()

        active_entry = self._get_active_session_entry_by_key_or_session_id(normalized_session_ref)
        resolved_session_id = active_entry.session_id if active_entry else normalized_session_ref
        session_record = self.session_store._db.get_session(resolved_session_id) if self.session_store._db else None
        if not active_entry and not session_record:
            raise ValueError("Unknown Hermes session.")

        history = self.session_store.load_transcript(resolved_session_id)
        is_browser_session = bool(active_entry and active_entry.origin and self._is_browser_bridge_source(active_entry.origin))
        progress = (
            self._get_browser_bridge_progress_snapshot(active_entry.session_key)
            if active_entry and is_browser_session
            else {}
        )
        source_name = ""
        if active_entry and active_entry.platform:
            source_name = active_entry.platform.value
        elif session_record:
            source_name = str(session_record.get("source") or "")

        return {
            "session_key": active_entry.session_key if active_entry else resolved_session_id,
            "session_id": resolved_session_id,
            "messages": self._serialize_browser_bridge_history(history),
            "progress": progress,
            "running": bool(progress.get("running")),
            "browser_label": self._format_sidebar_session_label(
                session_entry=active_entry,
                session_record=session_record or {},
            ),
            "source": source_name,
            "is_browser_session": is_browser_session,
            "can_send": is_browser_session,
        }

    @staticmethod
    def _serialize_browser_bridge_timestamp(value: Any) -> str:
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value).isoformat()
            except Exception:
                return ""
        return str(value or "").strip()

    def _serialize_browser_bridge_transcript(
        self,
        history: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for index, message in enumerate(history, start=1):
            if isinstance(message, dict):
                raw_message: Any = message
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list):
                    tool_calls = []
                result.append(
                    {
                        "index": index,
                        "role": str(message.get("role") or "").strip(),
                        "timestamp": self._serialize_browser_bridge_timestamp(message.get("timestamp")),
                        "content_text": self._extract_browser_bridge_content(message.get("content")),
                        "tool_name": str(message.get("tool_name") or "").strip(),
                        "tool_call_count": len(tool_calls),
                        "raw": raw_message,
                    }
                )
                continue

            result.append(
                {
                    "index": index,
                    "role": "",
                    "timestamp": "",
                    "content_text": self._extract_browser_bridge_content(message),
                    "tool_name": "",
                    "tool_call_count": 0,
                    "raw": message,
                }
            )
        return result

    def _load_browser_bridge_session_log(self, session_id: str) -> Dict[str, Any]:
        normalized_session_id = str(session_id or "").strip()
        path = _hermes_home / "sessions" / f"session_{normalized_session_id}.json"
        result = {
            "path": str(path),
            "exists": path.exists(),
            "data": None,
            "error": "",
        }
        if not normalized_session_id or not path.exists():
            return result
        try:
            result["data"] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result["error"] = str(exc)
        return result

    @staticmethod
    def _extract_session_log_tool_name(tool_call: Any) -> str:
        if not isinstance(tool_call, dict):
            return ""
        function_info = tool_call.get("function")
        if isinstance(function_info, dict):
            name = str(function_info.get("name") or "").strip()
            if name:
                return name
        return str(tool_call.get("name") or "").strip()

    @staticmethod
    def _deep_merge_browser_bridge_config(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in patch.items():
            if isinstance(base.get(key), dict) and isinstance(value, dict):
                GatewayRunner._deep_merge_browser_bridge_config(base[key], value)
            else:
                base[key] = value
        return base

    @staticmethod
    def _safe_json_loads(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {}

    def _build_delegate_branch_summary(self, audit_events: List[Dict[str, Any]]) -> Dict[str, Any]:
        calls: List[Dict[str, Any]] = []
        graph_nodes: List[Dict[str, Any]] = [
            {"id": "root", "label": "Hermes session", "kind": "root", "status": "ok"}
        ]
        graph_edges: List[Dict[str, Any]] = []

        delegate_events = [
            event for event in (audit_events or [])
            if event.get("kind") == "tool_finish" and event.get("tool_name") == "delegate_task"
        ]
        for index, event in enumerate(delegate_events, start=1):
            parsed = self._safe_json_loads((event.get("payload") or {}).get("result"))
            tasks = parsed.get("results") if isinstance(parsed, dict) else []
            if not isinstance(tasks, list):
                tasks = []
            call_id = f"delegate-{index}"
            call_status = "error" if event.get("is_error") else "ok"
            call_summary = {
                "id": call_id,
                "status": call_status,
                "duration_ms": int(event.get("duration_ms") or 0),
                "started_at": event.get("ts"),
                "branch_count": len(tasks),
                "ok_count": sum(1 for task in tasks if str(task.get("status") or "").strip().lower() == "completed"),
                "error_count": sum(1 for task in tasks if str(task.get("status") or "").strip().lower() not in {"", "completed"}),
                "tasks": [],
            }
            graph_nodes.append({
                "id": call_id,
                "label": f"delegate_task #{index}",
                "kind": "delegate_call",
                "status": call_status,
                "duration_ms": call_summary["duration_ms"],
            })
            graph_edges.append({"from": "root", "to": call_id})

            for task_index, task in enumerate(tasks, start=1):
                task_status = str(task.get("status") or "unknown").strip().lower() or "unknown"
                summary = str(task.get("summary") or task.get("error") or "").strip()
                label = summary[:96] if summary else f"Branch {task_index}"
                node_id = f"{call_id}-task-{task_index}"
                call_summary["tasks"].append({
                    "task_index": int(task.get("task_index") or task_index - 1),
                    "status": task_status,
                    "duration_seconds": float(task.get("duration_seconds") or 0),
                    "summary": summary,
                    "error": str(task.get("error") or "").strip(),
                })
                graph_nodes.append({
                    "id": node_id,
                    "label": label,
                    "kind": "delegate_branch",
                    "status": task_status,
                    "duration_seconds": float(task.get("duration_seconds") or 0),
                })
                graph_edges.append({"from": call_id, "to": node_id})
            calls.append(call_summary)

        return {
            "total_delegate_calls": len(calls),
            "total_branches": sum(call["branch_count"] for call in calls),
            "calls": calls,
            "graph": {"nodes": graph_nodes, "edges": graph_edges},
        }

    def _build_tool_benchmark_summary(
        self,
        audit_events: List[Dict[str, Any]],
        audit_metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        tool_finish = [
            event for event in (audit_events or [])
            if event.get("kind") == "tool_finish"
        ]
        tool_stats: Dict[str, Dict[str, Any]] = {}
        for event in tool_finish:
            tool_name = str(event.get("tool_name") or "tool").strip() or "tool"
            entry = tool_stats.setdefault(
                tool_name,
                {
                    "tool_name": tool_name,
                    "count": 0,
                    "error_count": 0,
                    "total_duration_ms": 0,
                    "max_duration_ms": 0,
                },
            )
            entry["count"] += 1
            if event.get("is_error"):
                entry["error_count"] += 1
            duration_ms = int(event.get("duration_ms") or 0)
            entry["total_duration_ms"] += duration_ms
            entry["max_duration_ms"] = max(entry["max_duration_ms"], duration_ms)

        tools = []
        for entry in tool_stats.values():
            count = int(entry["count"] or 0)
            error_count = int(entry["error_count"] or 0)
            total_duration_ms = int(entry["total_duration_ms"] or 0)
            tools.append({
                **entry,
                "avg_duration_ms": int(total_duration_ms / count) if count else 0,
                "error_rate": round(error_count / count, 3) if count else 0.0,
            })
        tools.sort(key=lambda item: (item["avg_duration_ms"], item["count"], item["max_duration_ms"]), reverse=True)

        return {
            "runtime_ms": audit_metrics.get("runtime_ms"),
            "error_count": audit_metrics.get("error_count", 0),
            "tool_count": audit_metrics.get("tool_count", 0),
            "message_count": audit_metrics.get("message_count", 0),
            "slowest_tools": audit_metrics.get("slowest_tools", []),
            "tools": tools,
        }

    def _build_runtime_config_snapshot(self, selected_provider: str = "") -> Dict[str, Any]:
        from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status
        from hermes_cli.config import get_env_value, load_config
        from hermes_cli.models import curated_models_for_provider, list_available_providers

        config = load_config()
        model_cfg = config.get("model")
        if isinstance(model_cfg, str):
            model_settings = {
                "default": model_cfg.strip(),
                "provider": "auto",
                "base_url": "",
                "api_mode": "",
            }
        elif isinstance(model_cfg, dict):
            model_settings = {
                "default": str(model_cfg.get("default") or "").strip(),
                "provider": str(model_cfg.get("provider") or "auto").strip() or "auto",
                "base_url": str(model_cfg.get("base_url") or "").strip(),
                "api_mode": str(model_cfg.get("api_mode") or "").strip(),
            }
        else:
            model_settings = {"default": "", "provider": "auto", "base_url": "", "api_mode": ""}

        selected_provider_id = str(selected_provider or model_settings.get("provider") or "openrouter").strip() or "openrouter"
        providers: List[Dict[str, Any]] = []
        for provider in list_available_providers():
            provider_id = str(provider.get("id") or "").strip()
            pconfig = PROVIDER_REGISTRY.get(provider_id)
            status = get_auth_status(provider_id)
            base_url_env_var = str(getattr(pconfig, "base_url_env_var", "") or "").strip()
            providers.append({
                **provider,
                "auth_type": str(getattr(pconfig, "auth_type", "") or "").strip(),
                "api_key_env_vars": list(getattr(pconfig, "api_key_env_vars", ()) or ()),
                "base_url_env_var": base_url_env_var,
                "base_url_value": get_env_value(base_url_env_var) if base_url_env_var else "",
                "status": status,
            })

        provider_models = curated_models_for_provider(selected_provider_id if selected_provider_id != "auto" else "openrouter")
        terminal_settings = dict(config.get("terminal") or {})
        env_windows_shell = str(os.getenv("HERMES_WINDOWS_SHELL") or "").strip()
        if env_windows_shell:
            terminal_settings["windows_shell"] = env_windows_shell
        config_path = _hermes_home / "config.yaml"
        env_path = _hermes_home / ".env"
        config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

        return {
            "config": {
                "model": model_settings,
                "control_room": dict(config.get("control_room") or {}),
                "terminal": terminal_settings,
                "web": dict(config.get("web") or {}),
                "tts": dict(config.get("tts") or {}),
                "stt": dict(config.get("stt") or {}),
                "delegation": dict(config.get("delegation") or {}),
            },
            "providers": providers,
            "provider_models": [
                {"id": model_id, "description": description}
                for model_id, description in provider_models
            ],
            "catalog": {
                "tts_providers": ["edge", "elevenlabs", "openai", "kokoro", "neutts", "f5"],
                "stt_providers": ["local", "groq", "openai"],
                "date_awareness_modes": [
                    {"id": "smart", "label": "Smart date grounding"},
                    {"id": "off", "label": "Literal dates only"},
                ],
            },
            "paths": {
                "config_path": str(config_path),
                "env_path": str(env_path),
            },
            "raw_config_text": config_text,
        }

    def _save_runtime_config_snapshot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        from hermes_cli.config import load_config, save_config, save_env_value, save_env_value_secure
        from tools.environments.shell_utils import normalize_windows_shell_override

        config = load_config()
        config_patch = payload.get("config_patch")
        if isinstance(config_patch, dict):
            self._deep_merge_browser_bridge_config(config, config_patch)
            save_config(config)
            terminal_patch = config_patch.get("terminal")
            if isinstance(terminal_patch, dict) and "windows_shell" in terminal_patch:
                os.environ["HERMES_WINDOWS_SHELL"] = normalize_windows_shell_override(
                    str(terminal_patch.get("windows_shell") or "auto")
                )

        env_updates = payload.get("env_updates")
        if isinstance(env_updates, dict):
            for key, raw_value in env_updates.items():
                env_key = str(key or "").strip()
                if not env_key:
                    continue
                value = str(raw_value or "")
                if any(token in env_key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                    save_env_value_secure(env_key, value)
                else:
                    save_env_value(env_key, value)

        selected_provider = str(payload.get("selected_provider") or "").strip()
        return self._build_runtime_config_snapshot(selected_provider=selected_provider)

    def _build_sidebar_session_inspection(self, session_ref: str) -> Dict[str, Any]:
        snapshot = self._get_sidebar_session_snapshot(session_ref)
        resolved_session_id = str(snapshot.get("session_id") or "").strip()
        resolved_session_key = str(snapshot.get("session_key") or resolved_session_id).strip()
        active_entry = self._get_active_session_entry_by_key_or_session_id(resolved_session_key)
        if not active_entry and resolved_session_id:
            active_entry = self._get_active_session_entry_by_key_or_session_id(resolved_session_id)
        session_record = self.session_store._db.get_session(resolved_session_id) if self.session_store._db else None
        history = self.session_store.load_transcript(resolved_session_id)
        transcript = self._serialize_browser_bridge_transcript(history)
        session_log_snapshot = self._load_browser_bridge_session_log(resolved_session_id)
        session_log = session_log_snapshot.get("data") if isinstance(session_log_snapshot.get("data"), dict) else None
        raw_messages = session_log.get("messages") if isinstance(session_log, dict) else None
        if not isinstance(raw_messages, list):
            raw_messages = []

        role_counts: Dict[str, int] = {}
        tool_names: List[str] = []
        configured_tools: List[str] = []
        tool_call_count = 0
        tool_result_count = 0
        reasoning_message_count = 0

        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            if role:
                role_counts[role] = role_counts.get(role, 0) + 1
            if role == "tool":
                tool_result_count += 1
            if message.get("reasoning"):
                reasoning_message_count += 1
            tool_name = str(message.get("tool_name") or "").strip()
            if tool_name and tool_name not in tool_names:
                tool_names.append(tool_name)
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                tool_calls = []
            tool_call_count += len(tool_calls)
            for tool_call in tool_calls:
                call_name = self._extract_session_log_tool_name(tool_call)
                if call_name and call_name not in tool_names:
                    tool_names.append(call_name)

        raw_configured_tools = session_log.get("tools") if isinstance(session_log, dict) else None
        if isinstance(raw_configured_tools, list):
            for item in raw_configured_tools:
                if not isinstance(item, dict):
                    continue
                name = self._extract_session_log_tool_name(item)
                if name and name not in configured_tools:
                    configured_tools.append(name)

        source_details: Dict[str, Any] = {}
        if active_entry and active_entry.origin:
            source_details = {
                "platform": active_entry.origin.platform.value,
                "chat_id": str(active_entry.origin.chat_id or ""),
                "chat_name": str(active_entry.origin.chat_name or ""),
                "user_id": str(active_entry.origin.user_id or ""),
                "user_name": str(active_entry.origin.user_name or ""),
                "session_key": active_entry.session_key,
            }
        elif session_record:
            source_details = {
                "platform": str(session_record.get("source") or ""),
                "session_key": resolved_session_key,
            }

        progress = snapshot.get("progress") if isinstance(snapshot.get("progress"), dict) else {}
        activity_log = progress.get("activity_log") if isinstance(progress, dict) else None
        if not isinstance(activity_log, list):
            activity_log = []
        audit_events: List[Dict[str, Any]] = []
        audit_metrics: Dict[str, Any] = {}
        if self.session_store._db and resolved_session_id:
            try:
                audit_events = self.session_store._db.get_audit_events(resolved_session_id, limit=500)
                audit_metrics = self.session_store._db.get_session_metrics(resolved_session_id)
            except Exception:
                audit_events = []
                audit_metrics = {}
        delegate_summary = self._build_delegate_branch_summary(audit_events)
        benchmark_summary = self._build_tool_benchmark_summary(audit_events, audit_metrics)

        return {
            **snapshot,
            "inspect_generated_at": datetime.now().isoformat(),
            "transcript": transcript,
            "session_log": session_log,
            "session_log_available": bool(session_log),
            "session_log_exists": bool(session_log_snapshot.get("exists")),
            "session_log_error": str(session_log_snapshot.get("error") or "").strip(),
            "configured_tools": configured_tools,
            "source_details": source_details,
            "session_record": session_record or {},
            "audit_events": audit_events,
            "audit_metrics": audit_metrics,
            "delegate_summary": delegate_summary,
            "benchmark_summary": benchmark_summary,
            "paths": {
                "hermes_home": str(_hermes_home),
                "config_path": str(_hermes_home / "config.yaml"),
                "logs_dir": str(_hermes_home / "logs"),
                "sessions_dir": str(_hermes_home / "sessions"),
                "session_log_path": str(session_log_snapshot.get("path") or ""),
            },
            "stats": {
                "visible_message_count": len(snapshot.get("messages") or []),
                "transcript_message_count": len(transcript),
                "raw_message_count": len(raw_messages),
                "activity_event_count": len(activity_log),
                "system_prompt_chars": len(str(session_log.get("system_prompt") or "")) if session_log else 0,
                "configured_tool_count": len(configured_tools),
                "tool_call_count": tool_call_count,
                "tool_result_count": tool_result_count,
                "reasoning_message_count": reasoning_message_count,
                "audit_event_count": len(audit_events),
                "delegate_call_count": delegate_summary.get("total_delegate_calls", 0),
                "delegate_branch_count": delegate_summary.get("total_branches", 0),
                "roles": role_counts,
                "tool_names": tool_names,
                "elapsed_seconds": int(progress.get("elapsed_seconds") or 0),
                "last_updated": str(session_log.get("last_updated") or "").strip() if session_log else "",
            },
        }

    def _resolve_sidebar_active_session_key(
        self,
        *,
        browser_label: str,
        client_session_id: str = "",
        session_key: str = "",
    ) -> str:
        normalized_session_key = str(session_key or "").strip()
        if normalized_session_key:
            try:
                snapshot = self._get_sidebar_session_snapshot(normalized_session_key)
                resolved = str(snapshot.get("session_key") or "").strip()
                if resolved:
                    return resolved
            except ValueError:
                pass

        try:
            source = self._build_browser_bridge_source(browser_label, client_session_id)
            session_entry = self.session_store.get_or_create_session(source)
            return session_entry.session_key
        except Exception:
            return ""

    def _extract_browser_bridge_image_attachments(
        self,
        payload: Dict[str, Any],
    ) -> tuple[List[str], List[str]]:
        """Decode sidepanel image attachments to local files for vision-capable turns."""
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            return [], []

        target_dir = _hermes_home / "browser_bridge_uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        media_urls: List[str] = []
        media_types: List[str] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            data_url = str(item.get("data_url") or "").strip()
            if not data_url:
                continue
            mime_type = str(item.get("mime_type") or "image/png").strip().lower()
            if not mime_type.startswith("image/"):
                continue
            try:
                if data_url.startswith("data:"):
                    if "," not in data_url:
                        continue
                    _, encoded = data_url.split(",", 1)
                else:
                    encoded = data_url
                raw_bytes = base64.b64decode(encoded, validate=False)
            except Exception:
                continue
            ext = mimetypes.guess_extension(mime_type) or ".png"
            filename = f"sidecar_{uuid.uuid4().hex[:12]}{ext}"
            out_path = target_dir / filename
            try:
                out_path.write_bytes(raw_bytes)
            except Exception:
                continue
            media_urls.append(str(out_path))
            media_types.append("photo")
        return media_urls, media_types

    async def _handle_browser_bridge_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        route = str(payload.get("_bridge_route") or "").strip()
        if route == "/session":
            return await self._handle_browser_bridge_session(payload)
        return await self._handle_browser_bridge_payload(payload)

    async def _handle_browser_bridge_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source = self._build_browser_bridge_source(
            str(payload.get("browserLabel") or "").strip() or "Chrome Extension",
            str(payload.get("clientSessionId") or "").strip(),
        )
        message = build_browser_context_message(payload)
        event = MessageEvent(text=message, source=source, message_type=MessageType.TEXT)
        await self._handle_message(event)
        snapshot = self._get_browser_bridge_session_snapshot(source)
        snapshot["accepted"] = True
        snapshot["detail"] = "Page context queued."
        return snapshot

    async def _handle_browser_bridge_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action") or "state").strip().lower()
        browser_label = str(
            payload.get("browserLabel") or payload.get("browser_label") or "Chrome Extension"
        ).strip() or "Chrome Extension"
        client_session_id = str(
            payload.get("clientSessionId") or payload.get("client_session_id") or ""
        ).strip()
        selected_session_key = str(payload.get("sessionKey") or payload.get("session_key") or "").strip()

        if action == "fetch_pdf_text":
            pdf_url = str(payload.get("url") or "").strip()
            return {"pdf_text": fetch_pdf_text(pdf_url)}

        if action == "fetch_pdf_preview_info":
            pdf_url = str(payload.get("url") or "").strip()
            images = fetch_pdf_page_images(pdf_url, max_pages=2)
            return {"image_count": len(images)}

        if action == "fetch_transcript":
            target = str(payload.get("url") or payload.get("video_id") or "").strip()
            language = str(payload.get("language") or "").strip()
            if not target:
                raise ValueError("fetch_transcript requires a YouTube URL or video_id.")

            try:
                result = await asyncio.to_thread(
                    self._fetch_bridge_youtube_transcript,
                    target,
                    language,
                )
            except BrowserBridgeTranscriptUnavailable as exc:
                return {
                    "ok": True,
                    "available": False,
                    "video_id": exc.video_id or self._extract_youtube_video_id(target),
                    "language": exc.requested_language,
                    "transcript_text": "",
                    "char_count": 0,
                    "segment_count": 0,
                    "error": str(exc),
                    "available_languages": exc.available_languages,
                }
            return {
                "ok": True,
                "available": True,
                **result,
            }

        if action == "tts":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ValueError("Text is required for tts.")
            from tools.tts_tool import text_to_speech_tool

            raw = await asyncio.to_thread(text_to_speech_tool, text)
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                parsed = {}
            if not parsed.get("success"):
                raise ValueError(parsed.get("error") or "TTS generation failed.")
            file_path = str(parsed.get("file_path") or "").strip()
            if not file_path:
                media_tag = str(parsed.get("media_tag") or "")
                match = re.search(r"MEDIA:(\S+)", media_tag)
                file_path = match.group(1) if match else ""
            if not file_path:
                raise ValueError("TTS output path missing.")
            audio_path = Path(file_path).expanduser()
            if not audio_path.exists():
                raise ValueError("TTS output file is missing.")
            audio_bytes = audio_path.read_bytes()
            mime_type = mimetypes.guess_type(str(audio_path))[0] or "audio/mpeg"
            return {
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "mime_type": mime_type,
                "provider": str(parsed.get("provider") or "").strip(),
            }

        if action == "transcribe_audio":
            raw_audio = str(payload.get("audio_base64") or "").strip()
            if not raw_audio:
                raise ValueError("audio_base64 is required for transcribe_audio.")
            mime_type = str(payload.get("mime_type") or "audio/webm").strip().lower()
            if raw_audio.startswith("data:") and "," in raw_audio:
                _, raw_audio = raw_audio.split(",", 1)
            try:
                audio_bytes = base64.b64decode(raw_audio, validate=False)
            except Exception as exc:
                raise ValueError("Invalid audio_base64 payload.") from exc

            ext_map = {
                "audio/webm": ".webm",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/mp3": ".mp3",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/mp4": ".m4a",
            }
            ext = ext_map.get(mime_type) or mimetypes.guess_extension(mime_type) or ".webm"
            if ext == ".weba":
                ext = ".webm"
            upload_dir = _hermes_home / "browser_bridge_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            audio_path = upload_dir / f"sidecar_audio_{uuid.uuid4().hex[:12]}{ext}"
            audio_path.write_bytes(audio_bytes)

            from tools.transcription_tools import transcribe_audio

            result = await asyncio.to_thread(transcribe_audio, str(audio_path))
            if not result.get("success"):
                raise ValueError(result.get("error") or "Audio transcription failed.")
            return {
                "transcript": str(result.get("transcript") or "").strip(),
                "provider": str(result.get("provider") or "").strip(),
            }

        if action == "runtime_config_get":
            return {
                "ok": True,
                **self._build_runtime_config_snapshot(
                    selected_provider=str(payload.get("selected_provider") or "").strip()
                ),
            }

        if action == "runtime_config_save":
            return {
                "ok": True,
                **self._save_runtime_config_snapshot(payload),
            }

        if action == "runtime_provider_models":
            from hermes_cli.models import curated_models_for_provider

            selected_provider = str(payload.get("selected_provider") or "openrouter").strip() or "openrouter"
            models = curated_models_for_provider(selected_provider if selected_provider != "auto" else "openrouter")
            return {
                "ok": True,
                "provider_models": [
                    {"id": model_id, "description": description}
                    for model_id, description in models
                ],
            }

        if action == "recall_search":
            from tools.session_search_tool import session_search

            query = str(payload.get("query") or "").strip()
            if not query:
                raise ValueError("Query is required for recall_search.")
            limit = max(1, min(8, int(payload.get("limit") or 5)))
            date_awareness = str(payload.get("date_awareness") or "smart").strip().lower() or "smart"
            recall_session_key = str(payload.get("sessionKey") or payload.get("session_key") or selected_session_key).strip()
            recall_session_id = ""
            if recall_session_key:
                try:
                    recall_snapshot = self._get_sidebar_session_snapshot(recall_session_key)
                    recall_session_id = str(recall_snapshot.get("session_id") or "").strip()
                except Exception:
                    recall_session_id = ""
            raw_result = session_search(
                query=query,
                limit=limit,
                db=self.session_store._db,
                current_session_id=recall_session_id,
                date_awareness=date_awareness,
            )
            parsed = json.loads(raw_result) if isinstance(raw_result, str) else {}
            return {"ok": True, **parsed}

        if action == "list":
            active_session_key = self._resolve_sidebar_active_session_key(
                browser_label=browser_label,
                client_session_id=client_session_id,
                session_key=selected_session_key,
            )
            return {
                "ok": True,
                "sessions": self._list_sidebar_sessions(limit=int(payload.get("limit") or 25)),
                "active_session_key": active_session_key,
                **self._empty_sidebar_session_snapshot(),
            }

        if selected_session_key:
            sidebar_snapshot = None
            try:
                sidebar_snapshot = self._get_sidebar_session_snapshot(selected_session_key)
            except ValueError:
                sidebar_snapshot = None

            if sidebar_snapshot and not sidebar_snapshot.get("is_browser_session"):
                if action == "state":
                    return {
                        "ok": True,
                        **sidebar_snapshot,
                    }
                if action == "inspect":
                    return {
                        "ok": True,
                        **self._build_sidebar_session_inspection(selected_session_key),
                    }
                raise ValueError(
                    "This session is read-only in the browser side panel right now. "
                    "You can browse it here, but sending new turns is only supported for browser sidecar sessions."
                )

        source = self._resolve_browser_bridge_source(payload)
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key

        if action == "state":
            return self._get_browser_bridge_session_snapshot(source)

        if action == "inspect":
            return {
                "ok": True,
                **self._build_sidebar_session_inspection(session_key),
            }

        if action == "reset":
            session_entry = self.session_store.get_or_create_session(source, force_new=True)
            self._browser_bridge_tasks.pop(session_entry.session_key, None)
            self._set_browser_bridge_progress(
                session_entry.session_key,
                running=False,
                detail="Started a fresh sidecar session.",
                recent_events=self._append_browser_bridge_progress_event(
                    session_entry.session_key,
                    "Started a fresh sidecar session.",
                ),
            )
            snapshot = self._get_browser_bridge_session_snapshot(source)
            snapshot["detail"] = "Started a fresh sidecar session."
            return snapshot

        if action == "interrupt":
            task = self._browser_bridge_tasks.get(session_key)
            if task and not task.done():
                self._browser_bridge_pending_interrupts.add(session_key)
                running_agent = self._running_agents.get(session_key)
                if running_agent:
                    running_agent.interrupt("Browser sidecar requested interrupt.")
                self._set_browser_bridge_progress(
                    session_key,
                    running=True,
                    detail="Interrupt requested. Hermes will stop after the current step.",
                    interrupt_requested=True,
                    recent_events=self._append_browser_bridge_progress_event(
                        session_key,
                        "Interrupt requested.",
                    ),
                )
                snapshot = self._get_browser_bridge_session_snapshot(source)
                snapshot["interrupt_requested"] = True
                snapshot["detail"] = "Interrupt requested. Hermes will stop after the current step."
                return snapshot
            snapshot = self._get_browser_bridge_session_snapshot(source)
            snapshot["interrupt_requested"] = False
            snapshot["detail"] = "No active Hermes turn to interrupt."
            return snapshot

        if action in {"send", "send_async"}:
            return await self._handle_browser_bridge_send(payload, source, async_mode=(action == "send_async"))

        raise ValueError(f"Unsupported browser bridge action: {action}")

    async def _handle_browser_bridge_send(
        self,
        payload: Dict[str, Any],
        source: SessionSource,
        *,
        async_mode: bool,
    ) -> Dict[str, Any]:
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        existing_task = self._browser_bridge_tasks.get(session_key)
        if existing_task and not existing_task.done():
            snapshot = self._get_browser_bridge_session_snapshot(source)
            snapshot["accepted"] = False
            snapshot["busy"] = True
            snapshot["detail"] = "Hermes is already working on this sidecar session."
            return snapshot

        page_payload = payload.get("pageContext")
        if not isinstance(page_payload, dict):
            page_payload = None
        user_message = str(payload.get("message") or payload.get("note") or "").strip()
        media_urls, media_types = self._extract_browser_bridge_image_attachments(payload)
        if not user_message and not page_payload and media_urls:
            user_message = "Please analyze the attached image(s)."
        message = build_browser_chat_message(user_message, page_payload)
        if not message.strip():
            raise ValueError("Sidecar message is empty.")

        async def _run_turn() -> None:
            self._set_browser_bridge_progress(
                session_key,
                running=True,
                detail="Hermes is thinking...",
                recent_events=self._append_browser_bridge_progress_event(
                    session_key,
                    "Turn started.",
                ),
            )
            try:
                is_slash_command_turn = message.strip().startswith("/")
                history_len_before = 0
                if is_slash_command_turn:
                    try:
                        history_len_before = len(self.session_store.load_transcript(session_entry.session_id))
                    except Exception:
                        history_len_before = 0

                event = MessageEvent(
                    text=message,
                    message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
                    source=source,
                    media_urls=media_urls,
                    media_types=media_types,
                )
                command_result = await self._handle_message(event)

                # Sidecar slash commands return text immediately from _handle_message
                # and do not pass through adapter send/transcript hooks. Persist a
                # minimal user/assistant exchange when the command handler did not
                # already append to transcript so sidepanel queue sync can clear.
                if is_slash_command_turn:
                    try:
                        history_len_after = len(self.session_store.load_transcript(session_entry.session_id))
                    except Exception:
                        history_len_after = history_len_before
                    transcript_already_updated = history_len_after > history_len_before
                    result_text = str(command_result or "").strip()
                    if result_text and not transcript_already_updated:
                        ts = datetime.now().isoformat()
                        self.session_store.append_to_transcript(
                            session_entry.session_id,
                            {"role": "user", "content": message, "timestamp": ts},
                        )
                        self.session_store.append_to_transcript(
                            session_entry.session_id,
                            {"role": "assistant", "content": result_text, "timestamp": ts},
                        )

                interrupted = session_key in self._browser_bridge_pending_interrupts
                if interrupted:
                    self._browser_bridge_pending_interrupts.discard(session_key)
                self._set_browser_bridge_progress(
                    session_key,
                    running=False,
                    detail="Interrupted." if interrupted else "Reply ready.",
                    interrupt_requested=False,
                    recent_events=self._append_browser_bridge_progress_event(
                        session_key,
                        "Interrupted." if interrupted else "Turn finished.",
                    ),
                )
            except asyncio.CancelledError:
                self._browser_bridge_pending_interrupts.discard(session_key)
                self._set_browser_bridge_progress(
                    session_key,
                    running=False,
                    detail="Interrupted.",
                    interrupt_requested=False,
                    recent_events=self._append_browser_bridge_progress_event(
                        session_key,
                        "Interrupted.",
                    ),
                )
                raise
            except Exception as exc:
                logger.exception("Browser sidecar turn failed")
                self._browser_bridge_pending_interrupts.discard(session_key)
                self._set_browser_bridge_progress(
                    session_key,
                    running=False,
                    detail="Sidecar turn failed.",
                    error=str(exc),
                    interrupt_requested=False,
                    recent_events=self._append_browser_bridge_progress_event(
                        session_key,
                        f"Error: {exc}",
                    ),
                )
            except BaseException as exc:
                # Guard against fatal worker exceptions (e.g. SystemExit/
                # KeyboardInterrupt escaping from tool internals). These should
                # fail the sidecar turn, not terminate the whole gateway.
                logger.exception("Browser sidecar turn crashed with fatal error")
                self._browser_bridge_pending_interrupts.discard(session_key)
                self._set_browser_bridge_progress(
                    session_key,
                    running=False,
                    detail="Sidecar turn failed.",
                    error=f"{type(exc).__name__}: {exc}",
                    interrupt_requested=False,
                    recent_events=self._append_browser_bridge_progress_event(
                        session_key,
                        f"Fatal error: {type(exc).__name__}: {exc}",
                    ),
                )
            finally:
                self._browser_bridge_tasks.pop(session_key, None)

        task = asyncio.create_task(_run_turn(), name=f"browser-bridge-{session_key}")
        self._browser_bridge_tasks[session_key] = task

        if async_mode:
            snapshot = self._get_browser_bridge_session_snapshot(source)
            snapshot["accepted"] = True
            snapshot["busy"] = True
            snapshot["detail"] = "Turn accepted."
            return snapshot

        try:
            await asyncio.wait_for(task, timeout=_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._browser_bridge_pending_interrupts.discard(session_key)
            self._set_browser_bridge_progress(
                session_key,
                running=False,
                detail="Sidecar turn timed out.",
                error=(
                    "Sidecar turn exceeded "
                    f"{_format_timeout_seconds(_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS)} seconds and was cancelled."
                ),
                interrupt_requested=False,
                recent_events=self._append_browser_bridge_progress_event(
                    session_key,
                    (
                        "Timed out after "
                        f"{_format_timeout_seconds(_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS)} seconds."
                    ),
                ),
            )
            raise TimeoutError(
                f"Sidecar turn exceeded {_format_timeout_seconds(_BROWSER_SIDECAR_SYNC_TIMEOUT_SECONDS)} seconds."
            )
        snapshot = self._get_browser_bridge_session_snapshot(source)
        snapshot["accepted"] = True
        snapshot["busy"] = False
        snapshot["detail"] = snapshot.get("progress", {}).get("detail") or "Reply ready."
        return snapshot

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.
        """
        logger.error(
            "Fatal %s adapter error (%s): %s",
            adapter.platform.value,
            adapter.fatal_error_code or "unknown",
            adapter.fatal_error_message or "unknown error",
        )

        existing = self.adapters.get(adapter.platform)
        if existing is adapter:
            try:
                await adapter.disconnect()
            finally:
                self.adapters.pop(adapter.platform, None)
                self.delivery_router.adapters = self.adapters

        # Queue retryable failures for background reconnection
        if adapter.fatal_error_retryable:
            platform_config = self.config.platforms.get(adapter.platform)
            if platform_config and adapter.platform not in self._failed_platforms:
                self._failed_platforms[adapter.platform] = {
                    "config": platform_config,
                    "attempts": 0,
                    "next_retry": time.monotonic() + 30,
                }
                logger.info(
                    "%s queued for background reconnection",
                    adapter.platform.value,
                )

        if not self.adapters and not self._failed_platforms:
            self._exit_reason = adapter.fatal_error_message or "All messaging adapters disconnected"
            if adapter.fatal_error_retryable:
                self._exit_with_failure = True
                logger.error("No connected messaging platforms remain. Shutting down gateway for service restart.")
            else:
                logger.error("No connected messaging platforms remain. Shutting down gateway cleanly.")
            await self.stop()
        elif not self.adapters and self._failed_platforms:
            # All platforms are down and queued for background reconnection.
            # If the error is retryable, exit with failure so systemd Restart=on-failure
            # can restart the process. Otherwise stay alive and keep retrying in background.
            if adapter.fatal_error_retryable:
                self._exit_reason = adapter.fatal_error_message or "All messaging platforms failed with retryable errors"
                self._exit_with_failure = True
                logger.error(
                    "All messaging platforms failed with retryable errors. "
                    "Shutting down gateway for service restart (systemd will retry)."
                )
                await self.stop()
            else:
                logger.warning(
                    "No connected messaging platforms remain, but %d platform(s) queued for reconnection",
                    len(self._failed_platforms),
                )

    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()
    
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
        """Load reasoning effort from config with env fallback.

        Checks agent.reasoning_effort in config.yaml first, then
        HERMES_REASONING_EFFORT as a fallback. Valid: "xhigh", "high",
        "medium", "low", "minimal", "none". Returns None to use default
        (medium).
        """
        from hermes_constants import parse_reasoning_effort
        effort = ""
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
            effort = os.getenv("HERMES_REASONING_EFFORT", "")
        result = parse_reasoning_effort(effort)
        if effort and effort.strip() and result is None:
            logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
        return result

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return bool(cfg.get("display", {}).get("show_reasoning", False))
        except Exception:
            pass
        return False

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var.

        Modes:
          - ``all``    — push running-output updates *and* the final message (default)
          - ``result`` — only the final completion message (regardless of exit code)
          - ``error``  — only the final message when exit code is non-zero
          - ``off``    — no watcher messages at all
        """
        mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
        if not mode:
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    raw = cfg.get("display", {}).get("background_process_notifications")
                    if raw is False:
                        mode = "off"
                    elif raw not in (None, ""):
                        mode = str(raw)
            except Exception:
                pass
        mode = (mode or "all").strip().lower()
        valid = {"all", "result", "error", "off"}
        if mode not in valid:
            logger.warning(
                "Unknown background_process_notifications '%s', defaulting to 'all'",
                mode,
            )
            return "all"
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return cfg.get("provider_routing", {}) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _load_fallback_model() -> dict | None:
        """Load fallback model config from config.yaml.

        Returns a dict with 'provider' and 'model' keys, or None if
        not configured / both fields empty.
        """
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                fb = cfg.get("fallback_model", {}) or {}
                if fb.get("provider") and fb.get("model"):
                    return fb
        except Exception:
            pass
        return None

    async def start(self) -> bool:
        """
        Start the gateway and all configured platform adapters.
        
        Returns True if at least one adapter connected successfully.
        """
        logger.info("Starting Hermes Gateway...")
        logger.info("Session storage: %s", self.config.sessions_dir)
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(gateway_state="starting", exit_reason=None)
        except Exception:
            pass

        collision_warning = _build_windows_wsl_localhost_collision_warning(self.config)
        if collision_warning:
            logger.warning(collision_warning)

        # Warn if no user allowlists are configured and open access is not opted in
        _any_allowlist = any(
            os.getenv(v)
            for v in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
                       "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
                       "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
                       "EMAIL_ALLOWED_USERS",
                       "SMS_ALLOWED_USERS", "MATTERMOST_ALLOWED_USERS",
                       "MATRIX_ALLOWED_USERS", "DINGTALK_ALLOWED_USERS",
                       "GATEWAY_ALLOWED_USERS")
        )
        _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes") or any(
            os.getenv(v, "").lower() in ("true", "1", "yes")
            for v in ("TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
                       "WHATSAPP_ALLOW_ALL_USERS", "SLACK_ALLOW_ALL_USERS",
                       "SIGNAL_ALLOW_ALL_USERS", "EMAIL_ALLOW_ALL_USERS",
                       "SMS_ALLOW_ALL_USERS", "MATTERMOST_ALLOW_ALL_USERS",
                       "MATRIX_ALLOW_ALL_USERS", "DINGTALK_ALLOW_ALL_USERS")
        )
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
        enabled_platform_count = 0
        startup_nonretryable_errors: list[str] = []
        startup_retryable_errors: list[str] = []
        
        # Initialize and connect each configured platform
        for platform, platform_config in self.config.platforms.items():
            if not platform_config.enabled:
                continue
            enabled_platform_count += 1
            
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                logger.warning("No adapter available for %s", platform.value)
                continue
            
            # Set up message + fatal error handlers
            adapter.set_message_handler(self._handle_message)
            adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
            
            # Try to connect
            logger.info("Connecting to %s...", platform.value)
            try:
                success = await adapter.connect()
                if success:
                    self.adapters[platform] = adapter
                    self._sync_voice_mode_state_to_adapter(adapter)
                    connected_count += 1
                    logger.info("%s connected", platform.value)
                else:
                    logger.warning("%s failed to connect", platform.value)
                    if adapter.has_fatal_error:
                        target = (
                            startup_retryable_errors
                            if adapter.fatal_error_retryable
                            else startup_nonretryable_errors
                        )
                        target.append(
                            f"{platform.value}: {adapter.fatal_error_message}"
                        )
                        # Queue for reconnection if the error is retryable
                        if adapter.fatal_error_retryable:
                            self._failed_platforms[platform] = {
                                "config": platform_config,
                                "attempts": 1,
                                "next_retry": time.monotonic() + 30,
                            }
                    else:
                        startup_retryable_errors.append(
                            f"{platform.value}: failed to connect"
                        )
                        # No fatal error info means likely a transient issue — queue for retry
                        self._failed_platforms[platform] = {
                            "config": platform_config,
                            "attempts": 1,
                            "next_retry": time.monotonic() + 30,
                        }
            except Exception as e:
                logger.error("%s error: %s", platform.value, e)
                startup_retryable_errors.append(f"{platform.value}: {e}")
                # Unexpected exceptions are typically transient — queue for retry
                self._failed_platforms[platform] = {
                    "config": platform_config,
                    "attempts": 1,
                    "next_retry": time.monotonic() + 30,
                }
        
        if connected_count == 0:
            if startup_nonretryable_errors:
                reason = "; ".join(startup_nonretryable_errors)
                logger.error("Gateway hit a non-retryable startup conflict: %s", reason)
                try:
                    from gateway.status import write_runtime_status
                    write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
                except Exception:
                    pass
                self._request_clean_exit(reason)
                return True
            if enabled_platform_count > 0:
                reason = "; ".join(startup_retryable_errors) or "all configured messaging platforms failed to connect"
                logger.error("Gateway failed to connect any configured messaging platform: %s", reason)
                try:
                    from gateway.status import write_runtime_status
                    write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
                except Exception:
                    pass
                return False
            logger.warning("No messaging platforms enabled.")
            logger.info("Gateway will continue running for cron job execution.")
        
        # Update delivery router with adapters
        self.delivery_router.adapters = self.adapters
        
        self._running = True
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(gateway_state="running", exit_reason=None)
        except Exception:
            pass

        # Start localhost browser sidecar bridge (used by the Chrome extension).
        try:
            self._browser_bridge = BrowserBridgeServer(
                loop=asyncio.get_running_loop(),
                handle_payload=self._handle_browser_bridge_request,
                config=BrowserBridgeConfig.from_env(),
            )
            self._browser_bridge.start()
        except Exception as e:
            logger.warning("Failed to start browser bridge: %s", e)
            self._browser_bridge = None
        
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
        
        # Check if we're restarting after a /update command. If the update is
        # still running, keep watching so we notify once it actually finishes.
        notified = await self._send_update_notification()
        if not notified and any(
            path.exists()
            for path in (
                _hermes_home / ".update_pending.json",
                _hermes_home / ".update_pending.claimed.json",
            )
        ):
            self._schedule_update_notification_watch()

        wiki_cfg = _get_wiki_host_config()
        if wiki_cfg.get("enabled"):
            try:
                wiki_port = int(wiki_cfg.get("port") or 8008)
                logger.info("Restoring wiki host on port %s", wiki_port)
                self._start_wiki_host(wiki_port)
            except Exception as e:
                logger.warning("Failed to restore wiki host: %s", e)

        # Start background session expiry watcher for proactive memory flushing
        asyncio.create_task(self._session_expiry_watcher())

        # Start background reconnection watcher for platforms that failed at startup
        if self._failed_platforms:
            logger.info(
                "Starting reconnection watcher for %d failed platform(s): %s",
                len(self._failed_platforms),
                ", ".join(p.value for p in self._failed_platforms),
            )
        asyncio.create_task(self._platform_reconnect_watcher())

        logger.info("Press Ctrl+C to stop")
        
        return True
    
    async def _session_expiry_watcher(self, interval: int = 300):
        """Background task that proactively flushes memories for expired sessions.
        
        Runs every `interval` seconds (default 5 min).  For each session that
        has expired according to its reset policy, flushes memories in a thread
        pool and marks the session so it won't be flushed again.

        This means memories are already saved by the time the user sends their
        next message, so there's no blocking delay.
        """
        await asyncio.sleep(60)  # initial delay — let the gateway fully start
        while self._running:
            try:
                self.session_store._ensure_loaded()
                for key, entry in list(self.session_store._entries.items()):
                    if entry.session_id in self.session_store._pre_flushed_sessions:
                        continue  # already flushed this session
                    if not self.session_store._is_session_expired(entry):
                        continue  # session still active
                    # Session has expired — flush memories in the background
                    logger.info(
                        "Session %s expired (key=%s), flushing memories proactively",
                        entry.session_id, key,
                    )
                    try:
                        await self._async_flush_memories(entry.session_id, key)
                        self._shutdown_gateway_honcho(key)
                        self.session_store._pre_flushed_sessions.add(entry.session_id)
                    except Exception as e:
                        logger.debug("Proactive memory flush failed for %s: %s", entry.session_id, e)
            except Exception as e:
                logger.debug("Session expiry watcher error: %s", e)
            # Sleep in small increments so we can stop quickly
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms.

        Uses exponential backoff: 30s → 60s → 120s → 240s → 300s (cap).
        Stops retrying a platform after 20 failed attempts or if the error
        is non-retryable (e.g. bad auth token).
        """
        _MAX_ATTEMPTS = 20
        _BACKOFF_CAP = 300  # 5 minutes max between retries

        await asyncio.sleep(10)  # initial delay — let startup finish
        while self._running:
            if not self._failed_platforms:
                # Nothing to reconnect — sleep and check again
                for _ in range(30):
                    if not self._running:
                        return
                    await asyncio.sleep(1)
                continue

            now = time.monotonic()
            for platform in list(self._failed_platforms.keys()):
                if not self._running:
                    return
                info = self._failed_platforms[platform]
                if now < info["next_retry"]:
                    continue  # not time yet

                if info["attempts"] >= _MAX_ATTEMPTS:
                    logger.warning(
                        "Giving up reconnecting %s after %d attempts",
                        platform.value, info["attempts"],
                    )
                    del self._failed_platforms[platform]
                    continue

                platform_config = info["config"]
                attempt = info["attempts"] + 1
                logger.info(
                    "Reconnecting %s (attempt %d/%d)...",
                    platform.value, attempt, _MAX_ATTEMPTS,
                )

                try:
                    adapter = self._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning(
                            "Reconnect %s: adapter creation returned None, removing from retry queue",
                            platform.value,
                        )
                        del self._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(self._handle_message)
                    adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)

                    success = await adapter.connect()
                    if success:
                        self.adapters[platform] = adapter
                        self._sync_voice_mode_state_to_adapter(adapter)
                        self.delivery_router.adapters = self.adapters
                        del self._failed_platforms[platform]
                        logger.info("✓ %s reconnected successfully", platform.value)

                        # Rebuild channel directory with the new adapter
                        try:
                            from gateway.channel_directory import build_channel_directory
                            build_channel_directory(self.adapters)
                        except Exception:
                            pass
                    else:
                        # Check if the failure is non-retryable
                        if adapter.has_fatal_error and not adapter.fatal_error_retryable:
                            logger.warning(
                                "Reconnect %s: non-retryable error (%s), removing from retry queue",
                                platform.value, adapter.fatal_error_message,
                            )
                            del self._failed_platforms[platform]
                        else:
                            backoff = min(30 * (2 ** (attempt - 1)), _BACKOFF_CAP)
                            info["attempts"] = attempt
                            info["next_retry"] = time.monotonic() + backoff
                            logger.info(
                                "Reconnect %s failed, next retry in %ds",
                                platform.value, backoff,
                            )
                except Exception as e:
                    backoff = min(30 * (2 ** (attempt - 1)), _BACKOFF_CAP)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning(
                        "Reconnect %s error: %s, next retry in %ds",
                        platform.value, e, backoff,
                    )

            # Check every 10 seconds for platforms that need reconnection
            for _ in range(10):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the gateway and disconnect all adapters."""
        logger.info("Stopping gateway...")
        self._running = False
        self._stop_wiki_host()

        # Stop browser sidecar work first so no new sidecar requests are accepted.
        browser_bridge = getattr(self, "_browser_bridge", None)
        if browser_bridge:
            try:
                browser_bridge.stop()
            except Exception as e:
                logger.debug("Failed stopping browser bridge: %s", e)
            self._browser_bridge = None
        for session_key, task in list(getattr(self, "_browser_bridge_tasks", {}).items()):
            if not task.done():
                task.cancel()
            self._browser_bridge_tasks.pop(session_key, None)
        pending_interrupts = getattr(self, "_browser_bridge_pending_interrupts", None)
        if pending_interrupts is not None:
            pending_interrupts.clear()

        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                agent.interrupt("Gateway shutting down")
                logger.debug("Interrupted running agent for session %s during shutdown", session_key[:20])
            except Exception as e:
                logger.debug("Failed interrupting agent during shutdown: %s", e)

        for platform, adapter in list(self.adapters.items()):
            try:
                await adapter.cancel_background_tasks()
            except Exception as e:
                logger.debug("%s background-task cancel error: %s", platform.value, e)
            try:
                await adapter.disconnect()
                logger.info("%s disconnected", platform.value)
            except Exception as e:
                logger.error("%s disconnect error: %s", platform.value, e)

        # Cancel any pending background tasks
        for _task in list(self._background_tasks):
            _task.cancel()
        self._background_tasks.clear()

        self.adapters.clear()
        self._running_agents.clear()
        self._pending_messages.clear()
        self._pending_approvals.clear()
        self._shutdown_all_gateway_honcho()
        self._shutdown_event.set()
        
        from gateway.status import remove_pid_file, write_runtime_status
        remove_pid_file()
        try:
            write_runtime_status(gateway_state="stopped", exit_reason=self._exit_reason)
        except Exception:
            pass
        
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
        if hasattr(config, "extra") and isinstance(config.extra, dict):
            config.extra.setdefault(
                "group_sessions_per_user",
                self.config.group_sessions_per_user,
            )

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

        elif platform == Platform.SIGNAL:
            from gateway.platforms.signal import SignalAdapter, check_signal_requirements
            if not check_signal_requirements():
                logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
                return None
            return SignalAdapter(config)

        elif platform == Platform.HOMEASSISTANT:
            from gateway.platforms.homeassistant import HomeAssistantAdapter, check_ha_requirements
            if not check_ha_requirements():
                logger.warning("HomeAssistant: aiohttp not installed or HASS_TOKEN not set")
                return None
            return HomeAssistantAdapter(config)

        elif platform == Platform.EMAIL:
            from gateway.platforms.email import EmailAdapter, check_email_requirements
            if not check_email_requirements():
                logger.warning("Email: EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_HOST, or EMAIL_SMTP_HOST not set")
                return None
            return EmailAdapter(config)

        elif platform == Platform.SMS:
            from gateway.platforms.sms import SmsAdapter, check_sms_requirements
            if not check_sms_requirements():
                logger.warning("SMS: aiohttp not installed or TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set")
                return None
            return SmsAdapter(config)

        elif platform == Platform.DINGTALK:
            from gateway.platforms.dingtalk import DingTalkAdapter, check_dingtalk_requirements
            if not check_dingtalk_requirements():
                logger.warning("DingTalk: dingtalk-stream not installed or DINGTALK_CLIENT_ID/SECRET not set")
                return None
            return DingTalkAdapter(config)

        elif platform == Platform.MATTERMOST:
            from gateway.platforms.mattermost import MattermostAdapter, check_mattermost_requirements
            if not check_mattermost_requirements():
                logger.warning("Mattermost: MATTERMOST_TOKEN or MATTERMOST_URL not set, or aiohttp missing")
                return None
            return MattermostAdapter(config)

        elif platform == Platform.MATRIX:
            from gateway.platforms.matrix import MatrixAdapter, check_matrix_requirements
            if not check_matrix_requirements():
                logger.warning("Matrix: matrix-nio not installed or credentials not set. Run: pip install 'matrix-nio[e2e]'")
                return None
            return MatrixAdapter(config)

        elif platform == Platform.API_SERVER:
            from gateway.platforms.api_server import APIServerAdapter, check_api_server_requirements
            if not check_api_server_requirements():
                logger.warning("API Server: aiohttp not installed")
                return None
            return APIServerAdapter(config)

        elif platform == Platform.WEBHOOK:
            from gateway.platforms.webhook import WebhookAdapter, check_webhook_requirements
            if not check_webhook_requirements():
                logger.warning("Webhook: aiohttp not installed")
                return None
            adapter = WebhookAdapter(config)
            adapter.gateway_runner = self  # For cross-platform delivery
            return adapter
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
        # Home Assistant events are system-generated (state changes), not
        # user-initiated messages.  The HASS_TOKEN already authenticates the
        # connection, so HA events are always authorized.
        # Webhook events are authenticated via HMAC signature validation in
        # the adapter itself — no user allowlist applies.
        if source.platform in (Platform.HOMEASSISTANT, Platform.WEBHOOK):
            return True
        # Browser sidecar and other local-origin messages are trusted on this machine.
        if source.platform == Platform.LOCAL:
            return True

        user_id = source.user_id
        if not user_id:
            return False

        platform_env_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
            Platform.DISCORD: "DISCORD_ALLOWED_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
            Platform.SLACK: "SLACK_ALLOWED_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOWED_USERS",
            Platform.EMAIL: "EMAIL_ALLOWED_USERS",
        }
        platform_allow_all_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOW_ALL_USERS",
            Platform.DISCORD: "DISCORD_ALLOW_ALL_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOW_ALL_USERS",
            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOW_ALL_USERS",
            Platform.EMAIL: "EMAIL_ALLOW_ALL_USERS",
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

        # Check if user is authorized
        if not self._is_user_authorized(source):
            logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            # In DMs: offer pairing code unless config says to ignore. In groups:
            # silently ignore.
            unauthorized_behavior = "pair"
            try:
                if hasattr(self.config, "get_unauthorized_dm_behavior"):
                    unauthorized_behavior = self.config.get_unauthorized_dm_behavior(source.platform)
                elif isinstance(self.config, dict):
                    unauthorized_behavior = str(self.config.get("unauthorized_dm_behavior") or "pair")
            except Exception:
                unauthorized_behavior = "pair"

            if source.chat_type == "dm" and unauthorized_behavior != "ignore":
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
        
        # PRIORITY handling when an agent is already running for this session.
        # Default behavior is to interrupt immediately so user text/stop messages
        # are handled with minimal latency.
        #
        # Special case: Telegram/photo bursts often arrive as multiple near-
        # simultaneous updates. Do NOT interrupt for photo-only follow-ups here;
        # let the adapter-level batching/queueing logic absorb them.
        _quick_key = self._session_key_for_source(source)
        if _quick_key in self._running_agents:
            if event.get_command() == "status":
                return await self._handle_status_command(event)
            if event.get_command() in {"terminal", "shell"}:
                return await self._handle_terminal_command(event)
            if event.get_command() == "cron":
                return await self._handle_cron_command(event)
            if event.get_command() in {"invokeai-defaults", "invokeai_defaults"}:
                return await self._handle_image_defaults_command(event)
            if event.get_command() in {"wiki-host", "wiki_host"}:
                return await self._handle_wiki_host_command(event)

            # Resolve the command once for all early-intercept checks below.
            from hermes_cli.commands import resolve_command as _resolve_cmd_inner
            _evt_cmd = event.get_command()
            _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

            # /stop must hard-kill the session when an agent is running.
            # A soft interrupt (agent.interrupt()) doesn't help when the agent
            # is truly hung — the executor thread is blocked and never checks
            # _interrupt_requested.  Force-clean _running_agents so the session
            # is unlocked and subsequent messages are processed normally.
            if _cmd_def_inner and _cmd_def_inner.name == "stop":
                running_agent = self._running_agents.get(_quick_key)
                if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
                    running_agent.interrupt("Stop requested")
                # Force-clean: remove the session lock regardless of agent state
                adapter = self.adapters.get(source.platform)
                if adapter and hasattr(adapter, 'get_pending_message'):
                    adapter.get_pending_message(_quick_key)  # consume and discard
                self._pending_messages.pop(_quick_key, None)
                if _quick_key in self._running_agents:
                    del self._running_agents[_quick_key]
                logger.info("HARD STOP for session %s — session lock released", _quick_key[:20])
                return "⚡ Force-stopped. The session is unlocked — you can send a new message."

            # /reset and /new must bypass the running-agent guard so they
            # actually dispatch as commands instead of being queued as user
            # text (which would be fed back to the agent with the same
            # broken history — #2170).  Interrupt the agent first, then
            # clear the adapter's pending queue so the stale "/reset" text
            # doesn't get re-processed as a user message after the
            # interrupt completes.
            if _cmd_def_inner and _cmd_def_inner.name == "new":
                running_agent = self._running_agents.get(_quick_key)
                if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
                    running_agent.interrupt("Session reset requested")
                # Clear any pending messages so the old text doesn't replay
                adapter = self.adapters.get(source.platform)
                if adapter and hasattr(adapter, 'get_pending_message'):
                    adapter.get_pending_message(_quick_key)  # consume and discard
                self._pending_messages.pop(_quick_key, None)
                # Clean up the running agent entry so the reset handler
                # doesn't think an agent is still active.
                if _quick_key in self._running_agents:
                    del self._running_agents[_quick_key]
                return await self._handle_reset_command(event)

            # /queue <prompt> — queue without interrupting
            if event.get_command() in ("queue", "q"):
                queued_text = event.get_command_args().strip()
                if not queued_text:
                    return "Usage: /queue <prompt>"
                adapter = self.adapters.get(source.platform)
                if adapter:
                    from gateway.platforms.base import MessageEvent as _ME, MessageType as _MT
                    queued_event = _ME(
                        text=queued_text,
                        message_type=_MT.TEXT,
                        source=event.source,
                        message_id=event.message_id,
                    )
                    adapter._pending_messages[_quick_key] = queued_event
                return "Queued for the next turn."

            if event.message_type == MessageType.PHOTO:
                logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key[:20])
                adapter = self.adapters.get(source.platform)
                if adapter:
                    # Reuse adapter queue semantics so photo bursts merge cleanly.
                    if _quick_key in adapter._pending_messages:
                        existing = adapter._pending_messages[_quick_key]
                        if getattr(existing, "message_type", None) == MessageType.PHOTO:
                            existing.media_urls.extend(event.media_urls)
                            existing.media_types.extend(event.media_types)
                            if event.text:
                                if not existing.text:
                                    existing.text = event.text
                                elif event.text not in existing.text:
                                    existing.text = f"{existing.text}\n\n{event.text}".strip()
                        else:
                            adapter._pending_messages[_quick_key] = event
                    else:
                        adapter._pending_messages[_quick_key] = event
                return None

            running_agent = self._running_agents.get(_quick_key)
            if running_agent is _AGENT_PENDING_SENTINEL:
                # Agent is being set up but not ready yet.
                if event.get_command() == "stop":
                    # Force-clean the sentinel so the session is unlocked.
                    if _quick_key in self._running_agents:
                        del self._running_agents[_quick_key]
                    logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key[:20])
                    return "⚡ Force-stopped. The agent was still starting — session unlocked."
                # Queue the message so it will be picked up after the
                # agent starts.
                adapter = self.adapters.get(source.platform)
                if adapter:
                    adapter._pending_messages[_quick_key] = event
                return None
            logger.debug("PRIORITY interrupt for session %s", _quick_key[:20])
            running_agent.interrupt(event.text)
            if _quick_key in self._pending_messages:
                self._pending_messages[_quick_key] += "\n" + event.text
            else:
                self._pending_messages[_quick_key] = event.text
            return None

        # Check for commands
        command = event.get_command()
        
        # Emit command:* hook for any recognized slash command
        _known_commands = {"new", "reset", "help", "status", "stop", "model", "reasoning",
                           "personality", "plan", "retry", "undo", "sethome", "set-home",
                           "compress", "usage", "insights", "reload-mcp", "reload_mcp",
                           "update", "title", "resume", "provider", "verbose", "rollback",
                           "background", "reasoning", "voice", "terminal", "shell", "cron",
                           "invokeai-defaults", "invokeai_defaults", "wiki-host", "wiki_host",
                           "browser"}
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

        if command == "reasoning":
            return await self._handle_reasoning_command(event)

        if command == "verbose":
            return await self._handle_verbose_command(event)

        if command == "provider":
            return await self._handle_provider_command(event)
        
        if command == "personality":
            return await self._handle_personality_command(event)

        if command in ["terminal", "shell"]:
            return await self._handle_terminal_command(event)

        if command == "browser":
            return await self._handle_browser_command(event)

        if command == "cron":
            return await self._handle_cron_command(event)

        if command == "plan":
            try:
                from agent.skill_commands import build_plan_path, build_skill_invocation_message

                user_instruction = event.get_command_args().strip()
                plan_path = build_plan_path(user_instruction)
                plan_path_text = plan_path.as_posix()
                event.text = build_skill_invocation_message(
                    "/plan",
                    user_instruction,
                    task_id=_quick_key,
                    runtime_note=(
                        "Save the markdown plan with write_file to this exact relative path "
                        f"inside the active workspace/backend cwd: {plan_path_text}"
                    ),
                )
                if not event.text:
                    return "Failed to load the bundled /plan skill."
                command = None
            except Exception as e:
                logger.exception("Failed to prepare /plan command")
                return f"Failed to enter plan mode: {e}"
        
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

        if command == "insights":
            return await self._handle_insights_command(event)

        if command in ("reload-mcp", "reload_mcp"):
            return await self._handle_reload_mcp_command(event)

        if command == "update":
            return await self._handle_update_command(event)

        if command == "title":
            return await self._handle_title_command(event)

        if command == "resume":
            return await self._handle_resume_command(event)

        if command == "rollback":
            return await self._handle_rollback_command(event)

        if command == "background":
            return await self._handle_background_command(event)

        if command == "reasoning":
            return await self._handle_reasoning_command(event)

        if command == "voice":
            return await self._handle_voice_command(event)

        # User-defined quick commands (bypass agent loop, no LLM call)
        if command:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "exec":
                    exec_cmd = qcmd.get("command", "")
                    if exec_cmd:
                        proc = None
                        communicate_task = None
                        try:
                            proc = await asyncio.create_subprocess_shell(
                                exec_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            communicate_task = asyncio.create_task(proc.communicate())
                            stdout, stderr = await asyncio.wait_for(communicate_task, timeout=30)
                            output = (stdout or stderr).decode().strip()
                            return output if output else "Command returned no output."
                        except asyncio.TimeoutError:
                            if proc is not None and proc.returncode is None:
                                proc.kill()
                            if communicate_task is not None:
                                try:
                                    await communicate_task
                                except Exception:
                                    pass
                            return "Quick command timed out (30s)."
                        except Exception as e:
                            if communicate_task is not None and not communicate_task.done():
                                communicate_task.cancel()
                                try:
                                    await communicate_task
                                except Exception:
                                    pass
                            elif proc is not None and proc.returncode is None:
                                proc.kill()
                                try:
                                    await proc.communicate()
                                except Exception:
                                    pass
                            return f"Quick command error: {e}"
                    else:
                        return f"Quick command '/{command}' has no command defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (only 'exec' is supported)."

        # Plugin-registered slash commands
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                plugin_handler = get_plugin_command_handler(command)
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    import asyncio as _aio
                    result = plugin_handler(user_args)
                    if _aio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.debug("Plugin command dispatch failed (non-fatal): %s", e)

        # Skill slash commands: /skill-name loads the skill and sends to agent
        if command:
            try:
                from agent.skill_commands import get_skill_commands, build_skill_invocation_message
                skill_cmds = get_skill_commands()
                cmd_key = f"/{command}"
                if cmd_key in skill_cmds:
                    user_instruction = event.get_command_args().strip()
                    msg = build_skill_invocation_message(
                        cmd_key, user_instruction, task_id=session_key
                    )
                    if msg:
                        event.text = msg
                        # Fall through to normal message processing with skill content
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)
        
        # Pending exec approvals are handled by /approve and /deny commands above.
        # No bare text matching — "yes" in normal conversation must not trigger
        # execution of a dangerous command.

        # ── Claim this session before any await ───────────────────────
        # Between here and _run_agent registering the real AIAgent, there
        # are numerous await points (hooks, vision enrichment, STT,
        # session hygiene compression).  Without this sentinel a second
        # message arriving during any of those yields would pass the
        # "already running" guard and spin up a duplicate agent for the
        # same session — corrupting the transcript.
        self._running_agents[_quick_key] = _AGENT_PENDING_SENTINEL

        try:
            return await self._handle_message_with_agent(event, source, _quick_key)
        finally:
            # If _run_agent replaced the sentinel with a real agent and
            # then cleaned it up, this is a no-op.  If we exited early
            # (exception, command fallthrough, etc.) the sentinel must
            # not linger or the session would be permanently locked out.
            if self._running_agents.get(_quick_key) is _AGENT_PENDING_SENTINEL:
                del self._running_agents[_quick_key]

    async def _handle_message_with_agent(self, event, source, _quick_key: str):
        """Inner handler that runs under the _running_agents sentinel guard."""
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
        
        # Read privacy.redact_pii from config (re-read per message)
        _redact_pii = False
        try:
            with open(_config_path, encoding="utf-8") as _pf:
                _pcfg = yaml.safe_load(_pf) or {}
            _redact_pii = bool((_pcfg.get("privacy") or {}).get("redact_pii", False))
        except Exception:
            pass

        # Build the context prompt to inject
        context_prompt = build_session_context_prompt(context, redact_pii=_redact_pii)
        
        # If the previous session expired and was auto-reset, prepend a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if getattr(session_entry, 'was_auto_reset', False):
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == "daily":
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            context_prompt = context_note + "\n\n" + context_prompt

            # Send a user-facing notification explaining the reset, unless:
            # - notifications are disabled in config
            # - the platform is excluded (e.g. api_server, webhook)
            # - the expired session had no activity (nothing was cleared)
            try:
                policy = self.session_store.config.get_reset_policy(
                    platform=source.platform,
                    session_type=getattr(source, 'chat_type', 'dm'),
                )
                platform_name = source.platform.value if source.platform else ""
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                should_notify = (
                    policy.notify
                    and had_activity
                    and platform_name not in policy.notify_exclude_platforms
                )
                if should_notify:
                    adapter = self.adapters.get(source.platform)
                    if adapter:
                        if reset_reason == "daily":
                            reason_text = f"daily schedule at {policy.at_hour}:00"
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f"{hours}h" if not mins else f"{hours}h {mins}m" if hours else f"{mins}m"
                            reason_text = f"inactive for {duration}"
                        notice = (
                            f"◐ Session automatically reset ({reason_text}). "
                            f"Conversation history cleared.\n"
                            f"Use /resume to browse and restore a previous session.\n"
                            f"Adjust reset timing in config.yaml under session_reset."
                        )
                        try:
                            session_info = self._format_session_info()
                            if session_info:
                                notice = f"{notice}\n\n{session_info}"
                        except Exception:
                            pass
                        await adapter.send(
                            source.chat_id, notice,
                            metadata=getattr(event, 'metadata', None),
                        )
            except Exception as e:
                logger.debug("Auto-reset notification failed (non-fatal): %s", e)

            session_entry.was_auto_reset = False
            session_entry.auto_reset_reason = None

        # Auto-load skill for DM topic bindings (e.g., Telegram Private Chat Topics)
        # Only inject on NEW sessions — for ongoing conversations the skill content
        # is already in the conversation history from the first message.
        if _is_new_session and getattr(event, "auto_skill", None):
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _skill_name = event.auto_skill
                _loaded = _load_skill_payload(_skill_name, task_id=_quick_key)
                if _loaded:
                    _loaded_skill, _skill_dir, _display_name = _loaded
                    _activation_note = (
                        f'[SYSTEM: This conversation is in a topic with the "{_display_name}" skill '
                        f"auto-loaded. Follow its instructions for the duration of this session.]"
                    )
                    _skill_msg = _build_skill_message(
                        _loaded_skill, _skill_dir, _activation_note,
                        user_instruction=event.text,
                    )
                    if _skill_msg:
                        event.text = _skill_msg
                        logger.info(
                            "[Gateway] Auto-loaded skill '%s' for DM topic session %s",
                            _skill_name, session_key,
                        )
                else:
                    logger.warning(
                        "[Gateway] DM topic skill '%s' not found in available skills",
                        _skill_name,
                    )
            except Exception as e:
                logger.warning("[Gateway] Failed to auto-load topic skill '%s': %s", event.auto_skill, e)

        # Load conversation history from transcript
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # -----------------------------------------------------------------
        # Session hygiene: auto-compress pathologically large transcripts
        #
        # Long-lived gateway sessions can accumulate enough history that
        # every new message rehydrates an oversized transcript, causing
        # repeated truncation/context failures.  Detect this early and
        # compress proactively — before the agent even starts.  (#628)
        #
        # Token source priority:
        # 1. Actual API-reported prompt_tokens from the last turn
        #    (stored in session_entry.last_prompt_tokens)
        # 2. Rough char-based estimate (str(msg)//4). Overestimates
        #    by 30-50% on code/JSON-heavy sessions, but that just
        #    means hygiene fires a bit early — safe and harmless.
        # -----------------------------------------------------------------
        if history and len(history) >= 4:
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                get_model_context_length,
            )

            # Read model + compression config from config.yaml.
            # NOTE: hygiene threshold is intentionally HIGHER than the agent's
            # own compressor (0.85 vs 0.50).  Hygiene is a safety net for
            # sessions that grew too large between turns — it fires pre-agent
            # to prevent API failures.  The agent's own compressor handles
            # normal context management during its tool loop with accurate
            # real token counts.  Having hygiene at 0.50 caused premature
            # compression on every turn in long gateway sessions.
            _hyg_model = "anthropic/claude-sonnet-4.6"
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            try:
                _hyg_cfg_path = _hermes_home / "config.yaml"
                if _hyg_cfg_path.exists():
                    import yaml as _hyg_yaml
                    with open(_hyg_cfg_path, encoding="utf-8") as _hyg_f:
                        _hyg_data = _hyg_yaml.safe_load(_hyg_f) or {}

                    # Resolve model name (same logic as run_sync)
                    _model_cfg = _hyg_data.get("model", {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get("default") or _model_cfg.get("model") or _hyg_model
                        # Read explicit context_length override from model config
                        # (same as run_agent.py lines 995-1005)
                        _raw_ctx = _model_cfg.get("context_length")
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        # Read provider for accurate context detection
                        _hyg_provider = _model_cfg.get("provider") or None
                        _hyg_base_url = _model_cfg.get("base_url") or None

                    # Read compression settings — only use enabled flag.
                    # The threshold is intentionally separate from the agent's
                    # compression.threshold (hygiene runs higher).
                    _comp_cfg = _hyg_data.get("compression", {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(
                            _comp_cfg.get("enabled", True)
                        ).lower() in ("true", "1", "yes")

                # Resolve provider/base_url from runtime if not in config
                if not _hyg_provider or not _hyg_base_url:
                    try:
                        _hyg_runtime = _resolve_runtime_agent_kwargs()
                        _hyg_provider = _hyg_provider or _hyg_runtime.get("provider")
                        _hyg_base_url = _hyg_base_url or _hyg_runtime.get("base_url")
                        _hyg_api_key = _hyg_runtime.get("api_key")
                    except Exception:
                        pass
            except Exception:
                pass

            # Check env override for disabling compression entirely
            if os.getenv("CONTEXT_COMPRESSION_ENABLED", "").lower() in ("false", "0", "no"):
                _hyg_compression_enabled = False

            if _hyg_compression_enabled:
                _hyg_context_length = get_model_context_length(
                    _hyg_model,
                    base_url=_hyg_base_url or "",
                    api_key=_hyg_api_key or "",
                    config_context_length=_hyg_config_context_length,
                    provider=_hyg_provider or "",
                )
                _compress_token_threshold = int(
                    _hyg_context_length * _hyg_threshold_pct
                )
                _warn_token_threshold = int(_hyg_context_length * 0.95)

                _msg_count = len(history)

                # Prefer actual API-reported tokens from the last turn
                # (stored in session entry) over the rough char-based estimate.
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = "actual"
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = "estimated"
                    # Note: rough estimates overestimate by 30-50% for code/JSON-heavy
                    # sessions, but that just means hygiene fires a bit early — which
                    # is safe and harmless.  The 85% threshold already provides ample
                    # headroom (agent's own compressor runs at 50%).  A previous 1.4x
                    # multiplier tried to compensate by inflating the threshold, but
                    # 85% * 1.4 = 119% of context — which exceeds the model's limit
                    # and prevented hygiene from ever firing for ~200K models (GLM-5).

                _needs_compress = _approx_tokens >= _compress_token_threshold

                if _needs_compress:
                    logger.info(
                        "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                        "(threshold: %s%% of %s = %s tokens)",
                        _msg_count, f"{_approx_tokens:,}", _token_source,
                        int(_hyg_threshold_pct * 100),
                        f"{_hyg_context_length:,}",
                        f"{_compress_token_threshold:,}",
                    )

                    _hyg_adapter = self.adapters.get(source.platform)
                    _hyg_meta = {"thread_id": source.thread_id} if source.thread_id else None
                    if _hyg_adapter:
                        try:
                            await _hyg_adapter.send(
                                source.chat_id,
                                f"🗜️ Session is large ({_msg_count} messages, "
                                f"~{_approx_tokens:,} tokens). Auto-compressing...",
                                metadata=_hyg_meta,
                            )
                        except Exception:
                            pass

                    try:
                        from run_agent import AIAgent

                        _hyg_runtime = _resolve_runtime_agent_kwargs()
                        if _hyg_runtime.get("api_key"):
                            _hyg_msgs = [
                                {"role": m.get("role"), "content": m.get("content")}
                                for m in history
                                if m.get("role") in ("user", "assistant")
                                and m.get("content")
                            ]

                            if len(_hyg_msgs) >= 4:
                                _hyg_agent = AIAgent(
                                    **_hyg_runtime,
                                    model=_hyg_model,
                                    max_iterations=4,
                                    quiet_mode=True,
                                    enabled_toolsets=["memory"],
                                    session_id=session_entry.session_id,
                                )
                                _hyg_agent._print_fn = lambda *a, **kw: None

                                loop = asyncio.get_event_loop()
                                _compressed, _ = await loop.run_in_executor(
                                    None,
                                    lambda: _hyg_agent._compress_context(
                                        _hyg_msgs, "",
                                        approx_tokens=_approx_tokens,
                                    ),
                                )

                                # _compress_context ends the old session and creates
                                # a new session_id.  Write compressed messages into
                                # the NEW session so the old transcript stays intact
                                # and searchable via session_search.
                                _hyg_new_sid = _hyg_agent.session_id
                                if _hyg_new_sid != session_entry.session_id:
                                    session_entry.session_id = _hyg_new_sid
                                    self.session_store._save()

                                self.session_store.rewrite_transcript(
                                    session_entry.session_id, _compressed
                                )
                                # Reset stored token count — transcript was rewritten
                                session_entry.last_prompt_tokens = 0
                                history = _compressed
                                _new_count = len(_compressed)
                                _new_tokens = estimate_messages_tokens_rough(
                                    _compressed
                                )

                                logger.info(
                                    "Session hygiene: compressed %s → %s msgs, "
                                    "~%s → ~%s tokens",
                                    _msg_count, _new_count,
                                    f"{_approx_tokens:,}", f"{_new_tokens:,}",
                                )

                                if _hyg_adapter:
                                    try:
                                        await _hyg_adapter.send(
                                            source.chat_id,
                                            f"🗜️ Compressed: {_msg_count} → "
                                            f"{_new_count} messages, "
                                            f"~{_approx_tokens:,} → "
                                            f"~{_new_tokens:,} tokens",
                                            metadata=_hyg_meta,
                                        )
                                    except Exception:
                                        pass

                                # Still too large after compression — warn user
                                if _new_tokens >= _warn_token_threshold:
                                    logger.warning(
                                        "Session hygiene: still ~%s tokens after "
                                        "compression — suggesting /reset",
                                        f"{_new_tokens:,}",
                                    )
                                    if _hyg_adapter:
                                        try:
                                            await _hyg_adapter.send(
                                                source.chat_id,
                                                "⚠️ Session is still very large "
                                                "after compression "
                                                f"(~{_new_tokens:,} tokens). "
                                                "Consider using /reset to start "
                                                "fresh if you experience issues.",
                                                metadata=_hyg_meta,
                                            )
                                        except Exception:
                                            pass

                    except Exception as e:
                        logger.warning(
                            "Session hygiene auto-compress failed: %s", e
                        )
                        # Compression failed and session is dangerously large
                        if _approx_tokens >= _warn_token_threshold:
                            _hyg_adapter = self.adapters.get(source.platform)
                            _hyg_meta = {"thread_id": source.thread_id} if source.thread_id else None
                            if _hyg_adapter:
                                try:
                                    await _hyg_adapter.send(
                                        source.chat_id,
                                        f"⚠️ Session is very large "
                                        f"({_msg_count} messages, "
                                        f"~{_approx_tokens:,} tokens) and "
                                        "auto-compression failed. Consider "
                                        "using /compress or /reset to avoid "
                                        "issues.",
                                        metadata=_hyg_meta,
                                    )
                                except Exception:
                                    pass

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
        # Voice channel awareness — inject current voice channel state
        # into context so the agent knows who is in the channel and who
        # is speaking, without needing a separate tool call.
        # -----------------------------------------------------------------
        if source.platform == Platform.DISCORD:
            adapter = self.adapters.get(Platform.DISCORD)
            guild_id = self._get_guild_id(event)
            if guild_id and adapter and hasattr(adapter, "get_voice_channel_context"):
                vc_context = adapter.get_voice_channel_context(guild_id)
                if vc_context:
                    context_prompt += f"\n\n{vc_context}"

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

            # Expand @ context references (@file:, @folder:, @diff, etc.)
            if "@" in message_text:
                try:
                    from agent.context_references import preprocess_context_references_async
                    from agent.model_metadata import get_model_context_length
                    _msg_cwd = os.environ.get("MESSAGING_CWD", os.path.expanduser("~"))
                    _msg_ctx_len = get_model_context_length(
                        self._model, base_url=self._base_url or "")
                    _ctx_result = await preprocess_context_references_async(
                        message_text, cwd=_msg_cwd,
                        context_length=_msg_ctx_len, allowed_root=_msg_cwd)
                    if _ctx_result.blocked:
                        _adapter = self.adapters.get(source.platform)
                        if _adapter:
                            await _adapter.send(
                                source.chat_id,
                                "\n".join(_ctx_result.warnings) or "Context injection refused.",
                            )
                        return
                    if _ctx_result.expanded:
                        message_text = _ctx_result.message
                except Exception as exc:
                    logger.debug("@ context reference expansion failed: %s", exc)

            # Run the agent
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_entry.session_id,
                session_key=session_key,
                event_message_id=event.message_id,
            )

            # Stop persistent typing indicator now that the agent is done
            try:
                _typing_adapter = self.adapters.get(source.platform)
                if _typing_adapter and hasattr(_typing_adapter, "stop_typing"):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                pass

            response = agent_result.get("final_response") or ""
            agent_messages = agent_result.get("messages", [])

            # If the agent's session_id changed during compression, update
            # session_entry so transcript writes below go to the right session.
            if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
                session_entry.session_id = agent_result["session_id"]

            # Prepend reasoning/thinking if display is enabled
            if getattr(self, "_show_reasoning", False) and response:
                last_reasoning = agent_result.get("last_reasoning")
                if last_reasoning:
                    # Collapse long reasoning to keep messages readable
                    lines = last_reasoning.strip().splitlines()
                    if len(lines) > 15:
                        display_reasoning = "\n".join(lines[:15])
                        display_reasoning += f"\n_... ({len(lines) - 15} more lines)_"
                    else:
                        display_reasoning = last_reasoning.strip()
                    response = f"💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}"

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
                        "model": os.getenv("HERMES_MODEL", ""),
                        "platform": source.platform.value if source.platform else "",
                        "timestamp": ts,
                    }
                )
            
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
                # The agent already persisted these messages to SQLite via
                # _flush_messages_to_session_db(), so skip the DB write here
                # to prevent the duplicate-write bug (#860).  We still write
                # to JSONL for backward compatibility and as a backup.
                agent_persisted = self._session_db is not None
                for msg in new_messages:
                    # Skip system messages (they're rebuilt each run)
                    if msg.get("role") == "system":
                        continue
                    # Add timestamp to each message for debugging
                    entry = {**msg, "timestamp": ts}
                    self.session_store.append_to_transcript(
                        session_entry.session_id, entry,
                        skip_db=agent_persisted,
                    )
            
            # Update session with actual prompt token count and model from the agent
            self.session_store.update_session(
                session_entry.session_key,
                input_tokens=agent_result.get("input_tokens", 0),
                output_tokens=agent_result.get("output_tokens", 0),
                cache_read_tokens=agent_result.get("cache_read_tokens", 0),
                cache_write_tokens=agent_result.get("cache_write_tokens", 0),
                last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
                model=agent_result.get("model"),
                estimated_cost_usd=agent_result.get("estimated_cost_usd"),
                cost_status=agent_result.get("cost_status"),
                cost_source=agent_result.get("cost_source"),
                provider=agent_result.get("provider"),
                base_url=agent_result.get("base_url"),
            )

            # Auto voice reply: send TTS audio before the text response
            _already_sent = bool(agent_result.get("already_sent"))
            if self._should_send_voice_reply(event, response, agent_messages, already_sent=_already_sent):
                await self._send_voice_reply(event, response)

            # If streaming already delivered the response, extract and
            # deliver any MEDIA: files before returning None.  Streaming
            # sends raw text chunks that include MEDIA: tags — the normal
            # post-processing in _process_message_background is skipped
            # when already_sent is True, so media files would never be
            # delivered without this.
            if agent_result.get("already_sent"):
                if response:
                    _media_adapter = self.adapters.get(source.platform)
                    if _media_adapter:
                        await self._deliver_media_from_response(
                            response, event, _media_adapter,
                        )
                return None
            return response
            
        except asyncio.CancelledError:
            running = bool(getattr(self, "_running", False))
            shutdown_requested = bool(
                getattr(self, "_shutdown_event", None) and self._shutdown_event.is_set()
            )
            if running and not shutdown_requested:
                logger.error(
                    "Agent turn cancelled unexpectedly in active session %s; "
                    "returning interrupted-turn response.",
                    session_key,
                )
                return (
                    "Sorry, that turn was interrupted unexpectedly. "
                    "Hermes is still running; please send it again."
                )
            raise
        except Exception as e:
            # Stop typing indicator on error too
            try:
                _err_adapter = self.adapters.get(source.platform)
                if _err_adapter and hasattr(_err_adapter, "stop_typing"):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            logger.exception("Agent error in session %s", session_key)
            return (
                "Sorry, I encountered an unexpected error. "
                "The details have been logged for debugging. "
                "Try again or use /reset to start a fresh session."
            )
        except BaseException as e:
            # Keep gateway alive when underlying tools raise fatal exceptions
            # (KeyboardInterrupt/SystemExit) from worker threads.
            logger.exception("Fatal agent error in session %s", session_key)
            return (
                "Sorry, the turn hit a fatal runtime error and was aborted. "
                "Hermes is still running; please try again."
            )
        finally:
            # Clear session env
            self._clear_session_env()
    
    def _format_session_info(self) -> str:
        """Resolve current model config and return a formatted info block.

        Surfaces model, provider, context length, and endpoint so gateway
        users can immediately see if context detection went wrong (e.g.
        local models falling to the 128K default).
        """
        from agent.model_metadata import get_model_context_length, DEFAULT_FALLBACK_CONTEXT

        model = _resolve_gateway_model()
        config_context_length = None
        provider = None
        base_url = None
        api_key = None

        try:
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                import yaml as _info_yaml
                with open(cfg_path, encoding="utf-8") as f:
                    data = _info_yaml.safe_load(f) or {}
                model_cfg = data.get("model", {})
                if isinstance(model_cfg, dict):
                    raw_ctx = model_cfg.get("context_length")
                    if raw_ctx is not None:
                        try:
                            config_context_length = int(raw_ctx)
                        except (TypeError, ValueError):
                            pass
                    provider = model_cfg.get("provider") or None
                    base_url = model_cfg.get("base_url") or None
        except Exception:
            pass

        # Resolve runtime credentials for probing
        try:
            runtime = _resolve_runtime_agent_kwargs()
            provider = provider or runtime.get("provider")
            base_url = base_url or runtime.get("base_url")
            api_key = runtime.get("api_key")
        except Exception:
            pass

        context_length = get_model_context_length(
            model,
            base_url=base_url or "",
            api_key=api_key or "",
            config_context_length=config_context_length,
            provider=provider or "",
        )

        # Format context source hint
        if config_context_length is not None:
            ctx_source = "config"
        elif context_length == DEFAULT_FALLBACK_CONTEXT:
            ctx_source = "default — set model.context_length in config to override"
        else:
            ctx_source = "detected"

        # Format context length for display
        if context_length >= 1_000_000:
            ctx_display = f"{context_length / 1_000_000:.1f}M"
        elif context_length >= 1_000:
            ctx_display = f"{context_length // 1_000}K"
        else:
            ctx_display = str(context_length)

        lines = [
            f"◆ Model: `{model}`",
            f"◆ Provider: {provider or 'openrouter'}",
            f"◆ Context: {ctx_display} tokens ({ctx_source})",
        ]

        # Show endpoint for local/custom setups
        if base_url and ("localhost" in base_url or "127.0.0.1" in base_url or "0.0.0.0" in base_url):
            lines.append(f"◆ Endpoint: {base_url}")

        return "\n".join(lines)

    async def _handle_reset_command(self, event: MessageEvent) -> str:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self._session_key_for_source(source)
        
        # Flush memories in the background (fire-and-forget) so the user
        # gets the "Session reset!" response immediately.
        try:
            old_entry = self.session_store._entries.get(session_key)
            if old_entry:
                _flush_task = asyncio.create_task(
                    self._async_flush_memories(old_entry.session_id, session_key)
                )
                self._background_tasks.add(_flush_task)
                _flush_task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.debug("Gateway memory flush on reset failed: %s", e)

        self._shutdown_gateway_honcho(session_key)
        self._evict_cached_agent(session_key)
        
        # Reset the session
        new_entry = self.session_store.reset_session(session_key)
        
        # Emit session:reset hook
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })
        
        # Resolve session config info to surface to the user
        try:
            session_info = self._format_session_info()
        except Exception:
            session_info = ""

        if new_entry:
            header = "✨ Session reset! Starting fresh."
        else:
            # No existing session, just create one
            self.session_store.get_or_create_session(source, force_new=True)
            header = "✨ New session started!"

        if session_info:
            return f"{header}\n\n{session_info}"
        return header
    
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
        """Handle /stop command - interrupt a running agent.

        When an agent is truly hung (blocked thread that never checks
        _interrupt_requested), the early intercept in _handle_message()
        handles /stop before this method is reached.  This handler fires
        only through normal command dispatch (no running agent) or as a
        fallback.  Force-clean the session lock in all cases for safety.
        """
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        
        agent = self._running_agents.get(session_key)
        if agent is _AGENT_PENDING_SENTINEL:
            # Force-clean the sentinel so the session is unlocked.
            if session_key in self._running_agents:
                del self._running_agents[session_key]
            logger.info("HARD STOP (pending) for session %s — sentinel cleared", session_key[:20])
            return "⚡ Force-stopped. The agent was still starting — session unlocked."
        if agent:
            agent.interrupt("Stop requested")
            # Force-clean the session lock so a truly hung agent doesn't
            # keep it locked forever.
            if session_key in self._running_agents:
                del self._running_agents[session_key]
            return "⚡ Force-stopped. The session is unlocked — you can send a new message."
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
            "`/model [provider:model]` — Show/change model (or switch provider)",
            "`/invokeai-defaults [model] [aspect]` — Show or change default InvokeAI image settings",
            "`/wiki-host [enable|disable|status] [port]` — Toggle LAN hosting for the local wiki",
            "`/provider` — Show available providers and auth status",
            "`/personality [name]` — Set a personality",
            "`/retry` — Retry your last message",
            "`/undo` — Remove the last exchange",
            "`/sethome` — Set this chat as the home channel",
            "`/compress` — Compress conversation context",
            "`/title [name]` — Set or show the session title",
            "`/resume [name]` — Resume a previously-named session",
            "`/usage` — Show token usage for this session",
            "`/insights [days]` — Show usage insights and analytics",
            "`/reasoning [level|show|hide]` — Set reasoning effort or toggle display",
            "`/rollback [number]` — List or restore filesystem checkpoints",
            "`/background <prompt>` — Run a prompt in a separate background session",
            "`/voice [on|off|tts|status]` — Toggle voice reply mode",
            "`/cron [list|add|remove|run|pause|resume|status|tick]` — Manage cron jobs",
            "`/browser [connect|disconnect|status]` — Manage live CDP browser link for sidecar/browser tools",
            "`/reload-mcp` — Reload MCP servers from config",
            "`/update` — Update Hermes Agent to the latest version",
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

    async def _run_cron_job_interactive(self, job: dict, source: SessionSource) -> None:
        """Execute a cron job immediately and stream progress back to the source chat."""
        from cron.jobs import mark_job_run, save_job_output
        from cron.scheduler import SILENT_MARKER, _deliver_result, _resolve_delivery_target, run_job
        import queue

        adapter = self.adapters.get(source.platform)
        if not adapter:
            logger.warning("Interactive cron run requested for %s but no adapter is connected", source.platform)
            return

        progress_queue = queue.Queue()
        last_tool = [None]
        progress_state = {
            "started_at": time.monotonic(),
            "phase": "thinking",
            "last_detail": f"Queued cron job {job.get('name', job.get('id', '?'))}...",
            "tool_calls": 0,
            "updates": 0,
            "recent_events": [],
            "done": False,
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
            while len(recent_events) > 4:
                recent_events.pop(0)

        def _set_progress_state(*, phase: str | None = None, detail: str | None = None, tool_call: bool = False) -> None:
            if phase:
                progress_state["phase"] = phase
            if detail:
                progress_state["last_detail"] = detail
                _push_recent_event(detail)
            if tool_call:
                progress_state["tool_calls"] += 1
            progress_state["updates"] += 1
            progress_queue.put("__tick__")

        _set_progress_state(
            phase="starting",
            detail=f"Starting cron job {job.get('name', job.get('id', '?'))}...",
        )

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
                status = str(parsed.get("status") or "").strip()
                details = str(parsed.get("details") or "").strip()
                if status:
                    parts.append(f"status={status}")
                if details:
                    parts.append(details)
                if "exit_code" in parsed:
                    parts.append(f"exit={parsed.get('exit_code')}")
                err = parsed.get("error")
                if err:
                    parts.append(f"error={err}")
                stderr = parsed.get("stderr")
                if not err and is_error and isinstance(stderr, str) and stderr.strip():
                    parts.append(f"stderr={stderr.strip()}")
                return " | ".join(parts)
            return text if is_error else ""

        def _build_progress_message() -> str:
            elapsed = int(max(0, time.monotonic() - progress_state["started_at"]))
            phase = progress_state.get("phase", "thinking")
            phase_label = {
                "starting": "starting",
                "thinking": "thinking",
                "tool": "running tools",
                "finalizing": "finalizing",
                "completed": "finished",
                "failed": "failed",
                "interrupted": "interrupted",
                "timed_out": "timed out",
            }.get(phase, "working")
            phase_emoji = {
                "starting": "🚀",
                "thinking": "💡",
                "tool": "🛠️",
                "finalizing": "🧾",
                "completed": "✅",
                "failed": "❌",
                "interrupted": "⚡",
                "timed_out": "⏱️",
            }.get(phase, "⚙️")
            header = f"{phase_emoji} Hermes is {phase_label} on cron job `{job.get('id', '?')}`... ({elapsed}s)"
            footer = f"Tools: {progress_state['tool_calls']} | Updates: {progress_state['updates']}"
            events = list(progress_state.get("recent_events") or [])
            if not events:
                fallback = (progress_state.get("last_detail") or "Working...").strip()
                events = [f"`{datetime.now().strftime('%H:%M:%S')}` {fallback}"]
            body = "\n".join(f"• {line}" for line in events[-4:])
            return f"{header}\n{body}\n{footer}"

        def progress_callback(tool_name: str, preview: str = None, args: dict = None) -> None:
            if tool_name == "_tool_result":
                payload = args or {}
                completed_tool = str(payload.get("tool") or preview or "tool")
                duration = payload.get("duration_seconds")
                is_error = bool(payload.get("is_error"))
                status_suffix = str(payload.get("status_suffix") or "").strip()
                result_text = str(payload.get("result") or "").strip()
                status_emoji = "❌" if is_error else "✅"
                duration_text = f" in {float(duration):.2f}s" if isinstance(duration, (int, float)) else ""
                detail = f"{status_emoji} {completed_tool} finished{duration_text}"
                if status_suffix:
                    detail = f"{detail} {status_suffix}"
                summary_suffix = _summarize_result_for_status(result_text, is_error)
                if summary_suffix:
                    detail = f"{detail} | {summary_suffix}"
                _set_progress_state(phase="tool", detail=detail, tool_call=False)
                return

            if tool_name == last_tool[0] and tool_name != "_thinking":
                return
            last_tool[0] = tool_name

            from agent.display import get_tool_emoji
            emoji = get_tool_emoji(tool_name, default="⚙️")
            if tool_name == "_thinking":
                _set_progress_state(phase="thinking", detail=f"💡 thinking: {preview or 'working...'}")
                return
            if preview:
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                detail = f"{emoji} {tool_name}: \"{preview}\""
            else:
                detail = f"{emoji} {tool_name}..."
            _set_progress_state(phase="tool", detail=detail, tool_call=True)

        def progress_thinking_callback(text: str) -> None:
            cleaned = str(text or "").strip()
            if cleaned:
                progress_callback("_thinking", cleaned)

        progress_metadata = {"tool_progress": True}
        if source.thread_id:
            progress_metadata["thread_id"] = source.thread_id

        async def send_progress_messages() -> None:
            def _is_retryable_progress_edit_error(error_text: str | None) -> bool:
                """Treat transient transport/server edit failures as retryable.

                Interactive cron runs should preserve a single live progress
                message through flaky Discord API responses instead of creating
                a fresh message on every failed edit attempt.
                """
                err = (error_text or "").lower()
                permanent_markers = (
                    "unknown message",
                    "not found",
                    "forbidden",
                    "missing access",
                    "missing permissions",
                    "cannot edit",
                    "error code: 10008",
                    "error code: 50013",
                )
                if any(marker in err for marker in permanent_markers):
                    return False
                transient_markers = (
                    "503",
                    "502",
                    "504",
                    "service unavailable",
                    "gateway timeout",
                    "upstream connect error",
                    "remote connection failure",
                    "reset before headers",
                    "timeout",
                    "timed out",
                    "temporarily unavailable",
                    "connection reset",
                    "server disconnected",
                    "try again",
                    "rate limit",
                    "429",
                )
                if any(marker in err for marker in transient_markers):
                    return True
                return True

            progress_msg_id = None
            last_rendered = ""
            while True:
                try:
                    updated = False
                    stop_requested = False
                    while True:
                        try:
                            item = progress_queue.get_nowait()
                            updated = True
                            if item == "__stop__":
                                stop_requested = True
                        except queue.Empty:
                            break
                    if not updated and progress_state["updates"] <= 0 and not stop_requested:
                        await asyncio.sleep(0.25)
                        continue
                    msg = _build_progress_message()
                    if progress_msg_id is not None:
                        result = await adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=progress_msg_id,
                            content=msg,
                        )
                        if not result.success:
                            if _is_retryable_progress_edit_error(result.error):
                                logger.warning(
                                    "Transient interactive cron progress edit failure for %s message %s; preserving live progress message: %s",
                                    source.platform,
                                    progress_msg_id,
                                    result.error,
                                )
                                await asyncio.sleep(0.25)
                                continue
                            progress_msg_id = None
                    if progress_msg_id is None:
                        result = await adapter.send(
                            chat_id=source.chat_id,
                            content=msg,
                            metadata=progress_metadata,
                        )
                        if result.success and result.message_id:
                            progress_msg_id = result.message_id
                    last_rendered = msg
                    await adapter.send_typing(source.chat_id, metadata=progress_metadata)
                    if stop_requested or progress_state.get("done"):
                        with contextlib.suppress(Exception):
                            if hasattr(adapter, "stop_typing"):
                                await adapter.stop_typing(source.chat_id)
                        return
                    await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    if progress_msg_id and last_rendered:
                        with contextlib.suppress(Exception):
                            await adapter.edit_message(
                                chat_id=source.chat_id,
                                message_id=progress_msg_id,
                                content=last_rendered,
                            )
                    with contextlib.suppress(Exception):
                        if hasattr(adapter, "stop_typing"):
                            await adapter.stop_typing(source.chat_id)
                    return
                except Exception as exc:
                    logger.error("Interactive cron progress error: %s", exc)
                    await asyncio.sleep(1)

        async def _finalize_progress(phase: str, detail: str) -> None:
            progress_state["done"] = True
            _set_progress_state(phase=phase, detail=detail, tool_call=False)
            progress_queue.put("__stop__")
            try:
                await asyncio.wait_for(progress_task, timeout=2.0)
            except asyncio.TimeoutError:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task

        progress_task = asyncio.create_task(send_progress_messages())
        job_id = str(job.get("id") or "?")
        success = False
        output = ""
        final_response = ""
        error = None
        try:
            import concurrent.futures

            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="gateway-cron-run",
            ) as pool:
                success, output, final_response, error = await loop.run_in_executor(
                    pool,
                    lambda: run_job(
                        job,
                        tool_progress_callback=progress_callback,
                        thinking_callback=progress_thinking_callback,
                        platform=source.platform.value if source.platform else "cron",
                        session_id=f"cron_manual_{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    ),
                )
        except Exception as exc:
            error = str(exc)
            logger.error("Interactive cron run failed for %s: %s", job_id, exc, exc_info=True)
        finally:
            if success:
                await _finalize_progress("completed", "✅ Cron job finished.")
            else:
                failure_detail = str(error or final_response or "Cron job failed.").strip()
                if len(failure_detail) > 220:
                    failure_detail = failure_detail[:217] + "..."
                await _finalize_progress("failed", f"❌ {failure_detail or 'Cron job failed.'}")

        output_file = save_job_output(job_id, output or f"# Cron Job: {job.get('name', job_id)}\n\n(No output captured)")
        deliver_content = final_response if success else f"⚠️ Cron job '{job.get('name', job_id)}' failed:\n{error or 'unknown error'}"
        if success and deliver_content and deliver_content.strip().upper().startswith(SILENT_MARKER):
            deliver_content = f"✅ Cron job `{job_id}` finished with no new report."
        if success and not str(deliver_content or "").strip():
            deliver_content = f"✅ Cron job `{job_id}` finished, but returned no message."

        try:
            await adapter.send(
                chat_id=source.chat_id,
                content=deliver_content,
                metadata={"thread_id": source.thread_id} if source.thread_id else None,
            )
        except Exception as exc:
            logger.error("Interactive cron completion delivery failed for %s: %s", job_id, exc)

        try:
            delivery_target = _resolve_delivery_target(job)
            same_target = (
                delivery_target
                and str(delivery_target.get("platform")) == source.platform.value
                and str(delivery_target.get("chat_id")) == str(source.chat_id)
                and str(delivery_target.get("thread_id") or "") == str(source.thread_id or "")
            )
            if delivery_target and not same_target and deliver_content:
                import concurrent.futures

                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="gateway-cron-delivery",
                ) as pool:
                    await loop.run_in_executor(pool, lambda: _deliver_result(job, deliver_content))
        except Exception as exc:
            logger.error("Interactive cron secondary delivery failed for %s: %s", job_id, exc)

        mark_job_run(job_id, bool(success), error if not success else None)

    async def _handle_cron_command(self, event: MessageEvent) -> str:
        """Handle /cron command in gateway chats."""
        from tools.cronjob_tools import cronjob as cronjob_tool

        args = event.get_command_args().strip()
        if not args:
            action = "list"
            tokens: list[str] = []
        else:
            try:
                tokens = shlex.split(args)
            except ValueError as e:
                return f"Invalid /cron arguments: {e}"
            action = (tokens[0].strip().lower() if tokens else "list")

        def _cron_api(**kwargs):
            try:
                payload = cronjob_tool(**kwargs)
                return json.loads(payload)
            except Exception as e:
                return {"success": False, "error": str(e)}

        def _format_job_line(job: dict) -> str:
            job_id = str(job.get("job_id", "?"))
            name = str(job.get("name", "(unnamed)"))
            schedule = str(job.get("schedule", "?"))
            state = str(job.get("state", "scheduled"))
            deliver = str(job.get("deliver", "local"))
            next_run = str(job.get("next_run_at") or "n/a")
            return (
                f"- `{job_id}` **{name}**\n"
                f"  schedule: `{schedule}` | state: `{state}` | deliver: `{deliver}` | next: `{next_run}`"
            )

        if action in {"list", "ls"}:
            include_disabled = any(t in {"--all", "-a"} for t in tokens[1:])
            result = _cron_api(action="list", include_disabled=include_disabled)
            if not result.get("success"):
                return f"Failed to list cron jobs: {result.get('error', 'unknown error')}"
            jobs = result.get("jobs", [])
            if not jobs:
                return (
                    "No scheduled jobs.\n"
                    "Create one with `/cron add <schedule> <prompt>`."
                )
            lines = [f"⏰ **Scheduled Jobs ({len(jobs)})**"]
            lines.extend(_format_job_line(job) for job in jobs)
            return "\n".join(lines)

        if action in {"add", "create"}:
            if len(tokens) < 3:
                return (
                    "Usage: `/cron add <schedule> <prompt>`\n"
                    "If your schedule has spaces, quote it. Example: "
                    "`/cron add \"0 9 * * *\" Daily standup summary`"
                )
            schedule = tokens[1]
            prompt = " ".join(tokens[2:]).strip()
            result = _cron_api(action="create", schedule=schedule, prompt=prompt)
            if not result.get("success"):
                return f"Failed to create cron job: {result.get('error', 'unknown error')}"
            return (
                f"✅ Created cron job `{result.get('job_id', '?')}`\n"
                f"name: **{result.get('name', '(unnamed)')}**\n"
                f"schedule: `{result.get('schedule', schedule)}`\n"
                f"next: `{result.get('next_run_at', 'n/a')}`"
            )

        if action in {"remove", "rm", "delete"}:
            if len(tokens) < 2:
                return "Usage: `/cron remove <job_id>`"
            job_id = tokens[1]
            result = _cron_api(action="remove", job_id=job_id)
            if not result.get("success"):
                return f"Failed to remove cron job: {result.get('error', 'unknown error')}"
            removed = result.get("removed_job", {})
            return f"🗑️ Removed cron job `{removed.get('id', job_id)}` ({removed.get('name', 'unnamed')})."

        if action in {"run", "run_now", "trigger"}:
            from cron.jobs import get_job

            if len(tokens) < 2:
                return "Usage: `/cron run <job_id>`"
            job_id = tokens[1]
            job = get_job(job_id)
            if not job:
                return "Failed to trigger cron job: Job not found."
            asyncio.create_task(self._run_cron_job_interactive(job, event.source))
            return (
                f"▶️ Running cron job `{job.get('id', job_id)}` ({job.get('name', 'unnamed')}) now.\n"
                "I’ll stream progress here and post the result when it finishes."
            )

        if action == "pause":
            if len(tokens) < 2:
                return "Usage: `/cron pause <job_id>`"
            job_id = tokens[1]
            reason = " ".join(tokens[2:]).strip() or None
            result = _cron_api(action="pause", job_id=job_id, reason=reason)
            if not result.get("success"):
                return f"Failed to pause cron job: {result.get('error', 'unknown error')}"
            job = result.get("job", {})
            return f"⏸️ Paused cron job `{job.get('job_id', job_id)}` ({job.get('name', 'unnamed')})."

        if action == "resume":
            if len(tokens) < 2:
                return "Usage: `/cron resume <job_id>`"
            job_id = tokens[1]
            result = _cron_api(action="resume", job_id=job_id)
            if not result.get("success"):
                return f"Failed to resume cron job: {result.get('error', 'unknown error')}"
            job = result.get("job", {})
            return f"✅ Resumed cron job `{job.get('job_id', job_id)}` ({job.get('name', 'unnamed')})."

        if action == "status":
            try:
                from hermes_cli.gateway import find_gateway_pids
                pids = find_gateway_pids()
            except Exception:
                pids = []
            result = _cron_api(action="list", include_disabled=False)
            if not result.get("success"):
                return f"Failed to load cron status: {result.get('error', 'unknown error')}"
            jobs = result.get("jobs", [])
            if pids:
                run_state = f"running (PID: {', '.join(map(str, pids))})"
            else:
                run_state = "not running"
            next_runs = [j.get("next_run_at") for j in jobs if j.get("next_run_at")]
            next_run = min(next_runs) if next_runs else "n/a"
            return (
                f"⏱️ **Cron Status**\n"
                f"gateway: `{run_state}`\n"
                f"active jobs: `{len(jobs)}`\n"
                f"next run: `{next_run}`"
            )

        if action == "tick":
            try:
                from cron.scheduler import tick as cron_tick
                await asyncio.to_thread(cron_tick, False)
                return "🧪 Ran one cron scheduler tick."
            except Exception as e:
                return f"Failed to run cron tick: {e}"

        return (
            "Unknown `/cron` subcommand.\n"
            "Use: `/cron list`, `/cron add <schedule> <prompt>`, "
            "`/cron remove <job_id>`, `/cron run <job_id>`, `/cron pause <job_id>`, "
            "`/cron resume <job_id>`, `/cron status`, or `/cron tick`."
        )
    
    async def _handle_model_command(self, event: MessageEvent) -> str:
        """Handle /model command - show or change the current model."""
        import yaml
        from hermes_cli.models import (
            curated_models_for_provider,
            normalize_provider,
            parse_model_input,
            _PROVIDER_LABELS,
        )

        args = event.get_command_args().strip()
        config_path = _hermes_home / 'config.yaml'

        # Resolve current model and provider from config
        current = os.getenv("HERMES_MODEL") or "anthropic/claude-opus-4.6"
        current_provider = "openrouter"
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, str):
                    current = model_cfg
                elif isinstance(model_cfg, dict):
                    current = model_cfg.get("default", current)
                    current_provider = model_cfg.get("provider", current_provider)
        except Exception:
            pass

        # Resolve "auto" to the actual provider using credential detection
        current_provider = normalize_provider(current_provider)
        if current_provider == "auto":
            try:
                from hermes_cli.auth import resolve_provider as _resolve_provider
                current_provider = _resolve_provider(current_provider)
            except Exception:
                current_provider = "openrouter"

        current = _normalize_gateway_model_for_provider(current, current_provider)

        # Detect custom endpoint: provider resolved to openrouter but a custom
        # base URL is configured — the user set up a custom endpoint.
        if current_provider == "openrouter" and os.getenv("OPENAI_BASE_URL", "").strip():
            current_provider = "custom"

        if not args:
            provider_label = _PROVIDER_LABELS.get(current_provider, current_provider)
            lines = [
                f"🤖 **Current model:** `{current}`",
                f"**Provider:** {provider_label}",
                "",
            ]
            curated = curated_models_for_provider(current_provider)
            if curated:
                lines.append(f"**Available models ({provider_label}):**")
                for mid, desc in curated:
                    marker = " ←" if mid == current else ""
                    label = f"  _{desc}_" if desc else ""
                    lines.append(f"• `{mid}`{label}{marker}")
                lines.append("")
            lines.append("To change: `/model model-name`")
            lines.append("Switch provider: `/model provider:model-name`")
            return "\n".join(lines)

        # Handle bare "/model custom" — switch to custom provider
        # and auto-detect the model from the endpoint.
        if args.strip().lower() == "custom":
            from hermes_cli.model_switch import switch_to_custom_provider
            cust_result = switch_to_custom_provider()
            if not cust_result.success:
                return f"⚠️ {cust_result.error_message}"
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                if "model" not in user_config or not isinstance(user_config["model"], dict):
                    user_config["model"] = {}
                user_config["model"]["default"] = cust_result.model
                user_config["model"]["provider"] = "custom"
                user_config["model"]["base_url"] = cust_result.base_url
                with open(config_path, 'w', encoding="utf-8") as f:
                    yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save model change: {e}"
            os.environ["HERMES_MODEL"] = cust_result.model
            os.environ["HERMES_INFERENCE_PROVIDER"] = "custom"
            self._effective_model = None
            self._effective_provider = None
            return (
                f"🤖 Model changed to `{cust_result.model}` (saved to config)\n"
                f"**Provider:** Custom\n"
                f"**Endpoint:** `{cust_result.base_url}`\n"
                f"_Model auto-detected from endpoint. Takes effect on next message._"
            )

        # Core model-switching pipeline (shared with CLI)
        from hermes_cli.model_switch import switch_model

        # Resolve current base_url for is_custom detection
        _resolved_base = ""
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider as _rtp
            _resolved_base = _rtp(requested=current_provider).get("base_url", "")
        except Exception:
            pass

        target_provider, requested_model = parse_model_input(args, current_provider)
        requested_model = _normalize_gateway_model_for_provider(requested_model, target_provider)
        switch_input = requested_model
        if target_provider != current_provider:
            switch_input = f"{target_provider}:{requested_model}"

        result = switch_model(
            switch_input,
            current_provider,
            current_base_url=_resolved_base,
            current_api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
        )
        result.new_model = _normalize_gateway_model_for_provider(
            result.new_model,
            result.target_provider,
        )

        if not result.success:
            msg = result.error_message
            tip = "\n\nUse `/model` to see available models, `/provider` to see providers" if "Did you mean" not in msg else ""
            return f"⚠️ {msg}{tip}"

        # Persist to config only if validation approves
        if result.persist:
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                if "model" not in user_config or not isinstance(user_config["model"], dict):
                    user_config["model"] = {}
                user_config["model"]["default"] = result.new_model
                if result.provider_changed:
                    user_config["model"]["provider"] = result.target_provider
                    # Persist base_url for custom endpoints; clear when
                    # switching away from custom (#2562 Phase 2).
                    if result.base_url and "openrouter.ai" not in (result.base_url or ""):
                        user_config["model"]["base_url"] = result.base_url
                    else:
                        user_config["model"].pop("base_url", None)
                with open(config_path, 'w', encoding="utf-8") as f:
                    yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save model change: {e}"

        # Set env vars so the next agent run picks up the change
        os.environ["HERMES_MODEL"] = result.new_model
        os.environ["HERMES_INFERENCE_PROVIDER"] = result.target_provider

        provider_note = f"\n**Provider:** {result.provider_label}" if result.provider_changed else ""

        warning = ""
        if result.warning_message:
            warning = f"\n⚠️ {result.warning_message}"

        persist_note = "saved to config" if result.persist else "this session only — will revert on restart"

        # Clear fallback state since user explicitly chose a model
        self._effective_model = None
        self._effective_provider = None

        # Show endpoint info for custom providers
        custom_hint = ""
        if result.is_custom_target:
            endpoint = result.base_url or _resolved_base or "custom endpoint"
            custom_hint = f"\n**Endpoint:** `{endpoint}`"
            if not result.provider_changed:
                custom_hint += (
                    "\n_To switch providers, use_ `/model provider:model`"
                    "\n_e.g._ `/model openrouter:anthropic/claude-sonnet-4`"
                )

        return f"🤖 Model changed to `{result.new_model}` ({persist_note}){provider_note}{warning}{custom_hint}\n_(takes effect on next message)_"

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
                "No image defaults saved yet.\n\n"
                "Use `/invokeai-defaults <model> <aspect_ratio>`.\n"
                "Supported aspect ratios: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`."
            )

        try:
            tokens = shlex.split(args)
        except ValueError as e:
            return f"Could not parse image defaults: {e}"

        if len(tokens) > 2:
            return (
                "Too many arguments for `/invokeai-defaults`.\n\n"
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
                "Please pick a model.\n\n"
                "The command can update just the aspect ratio if a model is already saved."
            )

        if not aspect_ratio:
            aspect_ratio = "1:1"

        if aspect_ratio not in aspect_presets:
            valid = ", ".join(f"`{key}`" for key in aspect_presets)
            return (
                f"Unknown aspect ratio: `{aspect_ratio}`\n\n"
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
            return f"Failed to save image defaults: {e}"

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
            "Saved image defaults.\n\n"
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
            return f"Could not parse wiki-host command: {e}"

        action = (tokens[0] or "").strip().lower()
        port_token = tokens[1].strip() if len(tokens) > 1 else ""

        if action in {"status", "show"}:
            return self._wiki_host_status_message()

        if action in {"disable", "off", "stop"}:
            self._stop_wiki_host()
            _save_wiki_host_config(enabled=False, port=None)
            return "Wiki hosting disabled."

        if action in {"enable", "on", "start"}:
            port = self._wiki_host_port or int((_get_wiki_host_config().get("port") or 8008))
            if port_token:
                try:
                    port = int(port_token)
                except ValueError:
                    return f"Invalid port: `{port_token}`"
            if not (1 <= int(port) <= 65535):
                return f"Invalid port: `{port}`"

            try:
                return self._start_wiki_host(int(port))
            except OSError as e:
                return f"Could not start wiki host on port `{port}`: {e}"
            except Exception as e:
                return f"Failed to start wiki host: {e}"

        return (
            f"Unknown wiki-host action: `{action}`\n\n"
            "Usage:\n"
            "- `/wiki-host status`\n"
            "- `/wiki-host enable 8008`\n"
            "- `/wiki-host disable`"
        )
    async def _handle_provider_command(self, event: MessageEvent) -> str:
        """Handle /provider command - show available providers."""
        import yaml
        from hermes_cli.models import (
            list_available_providers,
            normalize_provider,
            _PROVIDER_LABELS,
        )

        # Resolve current provider from config
        current_provider = "openrouter"
        config_path = _hermes_home / 'config.yaml'
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, dict):
                    current_provider = model_cfg.get("provider", current_provider)
        except Exception:
            pass

        current_provider = normalize_provider(current_provider)
        if current_provider == "auto":
            try:
                from hermes_cli.auth import resolve_provider as _resolve_provider
                current_provider = _resolve_provider(current_provider)
            except Exception:
                current_provider = "openrouter"

        # Detect custom endpoint
        if current_provider == "openrouter" and os.getenv("OPENAI_BASE_URL", "").strip():
            current_provider = "custom"

        current_label = _PROVIDER_LABELS.get(current_provider, current_provider)

        lines = [
            f"🔌 **Current provider:** {current_label} (`{current_provider}`)",
            "",
            "**Available providers:**",
        ]

        providers = list_available_providers()
        for p in providers:
            marker = " ← active" if p["id"] == current_provider else ""
            auth = "✅" if p["authenticated"] else "❌"
            aliases = f"  _(also: {', '.join(p['aliases'])})_" if p["aliases"] else ""
            lines.append(f"{auth} `{p['id']}` — {p['label']}{aliases}{marker}")

        lines.append("")
        lines.append("Switch: `/model provider:model-name`")
        lines.append("Setup: `hermes setup`")
        return "\n".join(lines)
    
    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """Handle /personality command - list or set a personality."""
        import yaml

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / 'config.yaml'

        try:
            if config_path.exists():
                with open(config_path, 'r', encoding="utf-8") as f:
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
            lines.append("• `none` — (no personality overlay)")
            for name, prompt in personalities.items():
                if isinstance(prompt, dict):
                    preview = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                else:
                    preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                lines.append(f"• `{name}` — {preview}")
            lines.append("\nUsage: `/personality <name>`")
            return "\n".join(lines)

        def _resolve_prompt(value):
            if isinstance(value, dict):
                parts = [value.get("system_prompt", "")]
                if value.get("tone"):
                    parts.append(f'Tone: {value["tone"]}')
                if value.get("style"):
                    parts.append(f'Style: {value["style"]}')
                return "\n".join(p for p in parts if p)
            return str(value)

        if args in ("none", "default", "neutral"):
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = ""
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save personality change: {e}"
            self._ephemeral_system_prompt = ""
            return "🎭 Personality cleared — using base agent behavior.\n_(takes effect on next message)_"
        elif args in personalities:
            new_prompt = _resolve_prompt(personalities[args])

            # Write to config.yaml, same pattern as CLI save_config_value.
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = new_prompt
                with open(config_path, 'w', encoding="utf-8") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save personality change: {e}"

            # Update in-memory so it takes effect on the very next message.
            self._ephemeral_system_prompt = new_prompt

            return f"🎭 Personality set to **{args}**\n_(takes effect on next message)_"

        available = "`none`, " + ", ".join(f"`{n}`" for n in personalities.keys())
        return f"Unknown personality: `{args}`\n\nAvailable: {available}"

    async def _handle_terminal_command(self, event: MessageEvent) -> str:
        """Handle /terminal command - show or change current Windows shell mode."""
        args = event.get_command_args().strip().lower()

        if os.name != "nt":
            return (
                "Terminal mode only applies when the **gateway** runs on Windows. "
                "Right now the gateway is running on Linux/WSL, so local commands "
                "always use your default POSIX shell."
            )

        current = os.getenv("HERMES_WINDOWS_SHELL", "auto")
        if not args:
            return (
                "🖥️ **Current terminal mode:** "
                f"`{current}`\n\n"
                "Usage: `/terminal cmd`, `/terminal powershell`, `/terminal wsl`, `/terminal auto`"
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
                "Valid options: `cmd`, `powershell`, `wsl`, `auto` "
                "(aliases: `windows`, `pwsh`)."
            )

        new_value = mode_map[args]
        os.environ["HERMES_WINDOWS_SHELL"] = new_value
        logger.info(
            "Terminal mode switched to %s (HERMES_WINDOWS_SHELL=%s) by user",
            new_value,
            new_value,
        )

        # Persist for gateway restarts.
        try:
            import yaml

            config_path = _hermes_home / "config.yaml"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    persisted = yaml.safe_load(f) or {}
            else:
                persisted = {}

            persisted["HERMES_WINDOWS_SHELL"] = new_value
            with open(config_path, "w", encoding="utf-8", newline="") as f:
                yaml.dump(persisted, f, default_flow_style=False)
        except Exception as e:
            logger.warning("Failed to persist terminal mode to config.yaml: %s", e)

        human_mode = {
            "powershell": "PowerShell",
            "wsl": "WSL (Linux shell)",
            "cmd": "cmd.exe",
            "auto": "auto (cmd.exe > PowerShell > WSL)",
        }.get(new_value, new_value)

        return (
            f"🖥️ Terminal mode set to **{human_mode}** "
            f"(`HERMES_WINDOWS_SHELL={new_value}`).\n"
            "Future local commands will use this shell."
        )

    @staticmethod
    def _normalize_cdp_browser_name(raw: str) -> str:
        normalized = (raw or "").strip().lower()
        alias_map = {
            "": "auto",
            "auto": "auto",
            "chrome": "chrome",
            "google-chrome": "chrome",
            "edge": "edge",
            "msedge": "edge",
            "microsoft-edge": "edge",
            "brave": "brave",
            "brave-browser": "brave",
            "chromium": "chromium",
            "comet": "comet",
        }
        return alias_map.get(normalized, normalized)

    @staticmethod
    def _default_cdp_port_for_browser(browser: str) -> int:
        mapping = {
            "auto": 9222,
            "chrome": 9222,
            "edge": 9223,
            "brave": 9224,
            "chromium": 9225,
            "comet": 9226,
        }
        return mapping.get(GatewayRunner._normalize_cdp_browser_name(browser), 9222)

    @staticmethod
    def _is_likely_cdp_endpoint(raw: str) -> bool:
        token = (raw or "").strip()
        if not token:
            return False
        if token.startswith(("ws://", "wss://", "http://", "https://")):
            return True
        if token.isdigit():
            return True
        if " " in token:
            return False
        host, sep, port = token.rpartition(":")
        return bool(sep and host and port.isdigit())

    @staticmethod
    def _resolve_browser_connect_target(raw_arg: str, default_port: int, default_browser: str, default_cdp_url: str):
        arg = (raw_arg or "").strip()
        browser_choice = GatewayRunner._normalize_cdp_browser_name(default_browser)
        if not arg:
            cdp_url = (default_cdp_url or "").strip() or f"ws://localhost:{default_port}"
            return cdp_url, browser_choice, ""

        if GatewayRunner._is_likely_cdp_endpoint(arg):
            endpoint = arg
            if endpoint.isdigit():
                endpoint = f"ws://localhost:{endpoint}"
            elif "://" not in endpoint:
                endpoint = f"ws://{endpoint}"
            return endpoint, browser_choice, ""

        candidate = GatewayRunner._normalize_cdp_browser_name(arg)
        supported = {"auto", "chrome", "edge", "brave", "chromium", "comet"}
        if candidate in supported:
            cdp_port = GatewayRunner._default_cdp_port_for_browser(candidate)
            return f"ws://localhost:{cdp_port}", candidate, ""

        err = (
            f"Unknown browser target `{arg}`. "
            "Use one of: auto, chrome, edge, brave, chromium, comet, or pass a CDP endpoint "
            "(example: ws://localhost:9222)."
        )
        return "", "", err

    @staticmethod
    def _extract_cdp_port(cdp_url: str, fallback_port: int) -> int:
        try:
            parsed = urlparse((cdp_url or "").strip())
            if parsed.port:
                return int(parsed.port)
        except Exception:
            pass
        try:
            return int(str(cdp_url).rsplit(":", 1)[-1].split("/")[0])
        except (ValueError, IndexError):
            return fallback_port

    @staticmethod
    def _is_local_cdp_url(cdp_url: str) -> bool:
        try:
            parsed = urlparse((cdp_url or "").strip())
        except Exception:
            return False
        host = (parsed.hostname or "").strip().lower()
        return host in {"localhost", "127.0.0.1"}

    async def _handle_browser_command(self, event: MessageEvent) -> str:
        """Handle /browser connect|disconnect|status in gateway/sidecar sessions."""
        try:
            from tools.browser_tool import (
                cleanup_all_browsers,
                read_shared_cdp_state,
                persist_shared_cdp_state,
                clear_shared_cdp_state,
            )
        except Exception as e:
            return f"Browser command unavailable: {e}"

        args = event.get_command_args().strip()
        if not args:
            sub = "status"
            target_arg = ""
        else:
            parts = args.split(None, 1)
            sub = (parts[0] or "status").strip().lower()
            target_arg = (parts[1] if len(parts) > 1 else "").strip()

        default_browser = self._normalize_cdp_browser_name(os.environ.get("BROWSER_CDP_BROWSER", "auto"))
        try:
            default_port = int(str(os.environ.get("BROWSER_CDP_PORT", "9222")).strip())
        except ValueError:
            default_port = 9222

        shared_state = read_shared_cdp_state() or {}
        shared_cdp = str(shared_state.get("cdp_url") or "").strip()
        current_env = os.environ.get("BROWSER_CDP_URL", "").strip()
        current = current_env or shared_cdp
        default_cdp = current or f"ws://localhost:{default_port}"

        if sub == "status":
            if current:
                lines = [
                    "🌐 Browser is connected to live CDP.",
                    f"- Endpoint: `{current}`",
                ]
                if not current_env and shared_cdp:
                    lines.append("- Source: shared runtime state")
                port = self._extract_cdp_port(current, default_port)
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(("127.0.0.1", port))
                    s.close()
                    lines.append("- Reachability: ✓ local endpoint reachable")
                except Exception:
                    lines.append("- Reachability: ⚠ local endpoint not reachable")
                lines.append(f"- Preferred target: `{default_browser}`")
                lines.append(f"- Default port: `{default_port}`")
                return "\n".join(lines)
            if os.environ.get("BROWSERBASE_API_KEY"):
                return (
                    "🌐 Browser mode: Browserbase (cloud).\n"
                    "Use `/browser connect` to attach a live local CDP browser."
                )
            return (
                "🌐 Browser mode: local headless Chromium.\n"
                "Use `/browser connect [target]` to attach live CDP.\n"
                "Targets: `auto`, `chrome`, `edge`, `brave`, `chromium`, `comet`, `ws://host:port`, `port`."
            )

        if sub == "disconnect":
            if not current:
                return "Browser is already disconnected from live CDP."
            os.environ.pop("BROWSER_CDP_URL", None)
            clear_shared_cdp_state()
            try:
                cleanup_all_browsers()
            except Exception:
                pass
            return (
                "🌐 Browser disconnected from live CDP.\n"
                "Browser tools reverted to default mode (local headless or Browserbase)."
            )

        if sub == "connect":
            cdp_url, browser_choice, resolve_err = self._resolve_browser_connect_target(
                raw_arg=target_arg,
                default_port=default_port,
                default_browser=default_browser,
                default_cdp_url=default_cdp,
            )
            if resolve_err:
                return resolve_err

            port = self._extract_cdp_port(cdp_url, default_port)
            is_local = self._is_local_cdp_url(cdp_url)

            reachable = True
            if is_local:
                reachable = False
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(("127.0.0.1", port))
                    s.close()
                    reachable = True
                except Exception:
                    reachable = False

            if is_local and not reachable:
                return (
                    "⚠ Browser not connected yet (CDP endpoint is unreachable).\n"
                    f"- Expected endpoint: `{cdp_url}`\n"
                    "- Start your browser with remote debugging enabled, then run `/browser connect` again."
                )

            os.environ["BROWSER_CDP_URL"] = cdp_url
            os.environ["BROWSER_CDP_BROWSER"] = browser_choice
            persisted = persist_shared_cdp_state(cdp_url, browser_choice)
            try:
                cleanup_all_browsers()
            except Exception:
                pass

            lines = [
                "🌐 Browser connected to live CDP.",
                f"- Endpoint: `{cdp_url}`",
                f"- Target: `{browser_choice}`",
            ]
            if not persisted:
                lines.append("- ⚠ Could not persist shared runtime state.")
            return "\n".join(lines)

        return (
            "Usage: `/browser connect [target]`, `/browser disconnect`, `/browser status`\n"
            "Targets: `auto`, `chrome`, `edge`, `brave`, `chromium`, `comet`, `ws://host:port`, `port`."
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
        # Reset stored token count — transcript was truncated
        session_entry.last_prompt_tokens = 0
        
        # Re-send by creating a fake text event with the old message
        retry_event = MessageEvent(
            text=last_user_msg,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=event.raw_message,
        )
        
        # Let the normal message handler process it
        return await self._handle_message(retry_event)
    
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
        # Reset stored token count — transcript was truncated
        session_entry.last_prompt_tokens = 0
        
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
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            user_config[env_key] = chat_id
            with open(config_path, 'w', encoding="utf-8") as f:
                yaml.dump(user_config, f, default_flow_style=False)
            # Also set in the current environment so it takes effect immediately
            os.environ[env_key] = str(chat_id)
        except Exception as e:
            return f"Failed to save home channel: {e}"
        
        return (
            f"✅ Home channel set to **{chat_name}** (ID: {chat_id}).\n"
            f"Cron jobs and cross-platform messages will be delivered here."
        )
    
    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        """Extract Discord guild_id from the raw message object."""
        raw = getattr(event, "raw_message", None)
        if raw is None:
            return None
        # Slash command interaction
        if hasattr(raw, "guild_id") and raw.guild_id:
            return int(raw.guild_id)
        # Regular message
        if hasattr(raw, "guild") and raw.guild:
            return raw.guild.id
        return None

    async def _handle_voice_command(self, event: MessageEvent) -> str:
        """Handle /voice [on|off|tts|channel|leave|status] command."""
        args = event.get_command_args().strip().lower()
        chat_id = event.source.chat_id

        adapter = self.adapters.get(event.source.platform)

        if args in ("on", "enable"):
            self._voice_mode[chat_id] = "voice_only"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
            return (
                "Voice mode enabled.\n"
                "I'll reply with voice when you send voice messages.\n"
                "Use /voice tts to get voice replies for all messages."
            )
        elif args in ("off", "disable"):
            self._voice_mode[chat_id] = "off"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            return "Voice mode disabled. Text-only replies."
        elif args == "tts":
            self._voice_mode[chat_id] = "all"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
            return (
                "Auto-TTS enabled.\n"
                "All replies will include a voice message."
            )
        elif args in ("channel", "join"):
            return await self._handle_voice_channel_join(event)
        elif args == "leave":
            return await self._handle_voice_channel_leave(event)
        elif args == "status":
            mode = self._voice_mode.get(chat_id, "off")
            labels = {
                "off": "Off (text only)",
                "voice_only": "On (voice reply to voice messages)",
                "all": "TTS (voice reply to all messages)",
            }
            # Append voice channel info if connected
            adapter = self.adapters.get(event.source.platform)
            guild_id = self._get_guild_id(event)
            if guild_id and hasattr(adapter, "get_voice_channel_info"):
                info = adapter.get_voice_channel_info(guild_id)
                if info:
                    lines = [
                        f"Voice mode: {labels.get(mode, mode)}",
                        f"Voice channel: #{info['channel_name']}",
                        f"Participants: {info['member_count']}",
                    ]
                    for m in info["members"]:
                        status = " (speaking)" if m.get("is_speaking") else ""
                        lines.append(f"  - {m['display_name']}{status}")
                    return "\n".join(lines)
            return f"Voice mode: {labels.get(mode, mode)}"
        else:
            # Toggle: off → on, on/all → off
            current = self._voice_mode.get(chat_id, "off")
            if current == "off":
                self._voice_mode[chat_id] = "voice_only"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
                return "Voice mode enabled."
            else:
                self._voice_mode[chat_id] = "off"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
                return "Voice mode disabled."

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        if not hasattr(adapter, "join_voice_channel"):
            return "Voice channels are not supported on this platform."

        guild_id = self._get_guild_id(event)
        if not guild_id:
            return "This command only works in a Discord server."

        voice_channel = await adapter.get_user_voice_channel(
            guild_id, event.source.user_id
        )
        if not voice_channel:
            return "You need to be in a voice channel first."

        # Wire callbacks BEFORE join so voice input arriving immediately
        # after connection is not lost.
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = self._handle_voice_channel_input
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = self._handle_voice_timeout_cleanup

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to join voice channel: %s", e)
            adapter._voice_input_callback = None
            err_lower = str(e).lower()
            if "pynacl" in err_lower or "nacl" in err_lower or "davey" in err_lower:
                return (
                    "Voice dependencies are missing (PyNaCl / davey). "
                    "Install or reinstall Hermes with the messaging extra, e.g. "
                    "`pip install hermes-agent[messaging]`."
                )
            return f"Failed to join voice channel: {e}"

        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            self._voice_mode[event.source.chat_id] = "all"
            self._save_voice_modes()
            self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=False)
            return (
                f"Joined voice channel **{voice_channel.name}**.\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
            )
        # Join failed — clear callback
        adapter._voice_input_callback = None
        return "Failed to join voice channel. Check bot permissions (Connect + Speak)."

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        guild_id = self._get_guild_id(event)

        if not guild_id or not hasattr(adapter, "leave_voice_channel"):
            return "Not in a voice channel."

        if not hasattr(adapter, "is_in_voice_channel") or not adapter.is_in_voice_channel(guild_id):
            return "Not in a voice channel."

        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning("Error leaving voice channel: %s", e)
        # Always clean up state even if leave raised an exception
        self._voice_mode[event.source.chat_id] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=True)
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str) -> None:
        """Called by the adapter when a voice channel times out.

        Cleans up runner-side voice_mode state that the adapter cannot reach.
        """
        self._voice_mode[chat_id] = "off"
        self._save_voice_modes()
        adapter = self.adapters.get(Platform.DISCORD)
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    async def _handle_voice_channel_input(
        self, guild_id: int, user_id: int, transcript: str
    ):
        """Handle transcribed voice from a user in a voice channel.

        Creates a synthetic MessageEvent and processes it through the
        adapter's full message pipeline (session, typing, agent, TTS reply).
        """
        adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return

        text_ch_id = adapter._voice_text_channels.get(guild_id)
        if not text_ch_id:
            return

        # Check authorization before processing voice input
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=str(text_ch_id),
            user_id=str(user_id),
            user_name=str(user_id),
            chat_type="channel",
        )
        if not self._is_user_authorized(source):
            logger.debug("Unauthorized voice input from user %d, ignoring", user_id)
            return

        # Show transcript in text channel (after auth, with mention sanitization)
        # via adapter.send so Discord embed chunking/limits match normal replies.
        try:
            safe_text = transcript[:4000].replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
            await adapter.send(
                chat_id=str(text_ch_id),
                content=f"**[Voice]** <@{user_id}>: {safe_text}",
                include_listen_button=False,
            )
        except Exception as e:
            logger.debug("Failed to send voice transcript mirror message: %s", e)

        # Build a synthetic MessageEvent and feed through the normal pipeline
        # Use SimpleNamespace as raw_message so _get_guild_id() can extract
        # guild_id and _send_voice_reply() plays audio in the voice channel.
        from types import SimpleNamespace
        event = MessageEvent(
            source=source,
            text=transcript,
            message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
        )

        await adapter.handle_message(event)

    def _should_send_voice_reply(
        self,
        event: MessageEvent,
        response: str,
        agent_messages: list,
        already_sent: bool = False,
    ) -> bool:
        """Decide whether the runner should send a TTS voice reply.

        Returns False when:
        - voice_mode is off for this chat
        - response is empty or an error
        - agent already called text_to_speech tool (dedup)
        - voice input and base adapter auto-TTS already handled it (skip_double)
          UNLESS streaming already consumed the response (already_sent=True),
          in which case the base adapter won't have text for auto-TTS so the
          runner must handle it.
        """
        if not response or response.startswith("Error:"):
            return False

        chat_id = event.source.chat_id
        voice_mode = self._voice_mode.get(chat_id, "off")
        is_voice_input = (event.message_type == MessageType.VOICE)

        should = (
            (voice_mode == "all")
            or (voice_mode == "voice_only" and is_voice_input)
        )
        if not should:
            return False

        # Dedup: agent already called TTS tool
        has_agent_tts = any(
            msg.get("role") == "assistant"
            and any(
                tc.get("function", {}).get("name") == "text_to_speech"
                for tc in (msg.get("tool_calls") or [])
            )
            for msg in agent_messages
        )
        if has_agent_tts:
            return False

        # Dedup: base adapter auto-TTS already handles voice input
        # (play_tts plays in VC when connected, so runner can skip).
        # When streaming already delivered the text (already_sent=True),
        # the base adapter will receive None and can't run auto-TTS,
        # so the runner must take over.
        if is_voice_input and not already_sent:
            return False

        return True

    async def _send_voice_reply(self, event: MessageEvent, text: str) -> None:
        """Generate TTS audio and send as a voice message before the text reply."""
        import uuid as _uuid
        audio_path = None
        actual_path = None
        try:
            from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts

            tts_text = _strip_markdown_for_tts(text[:4000])
            if not tts_text:
                return

            # Use .mp3 extension so edge-tts conversion to opus works correctly.
            # The TTS tool may convert to .ogg — use file_path from result.
            audio_path = os.path.join(
                tempfile.gettempdir(), "hermes_voice",
                f"tts_reply_{_uuid.uuid4().hex[:12]}.mp3",
            )
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)

            result_json = await asyncio.to_thread(
                text_to_speech_tool, text=tts_text, output_path=audio_path
            )
            result = json.loads(result_json)

            # Use the actual file path from result (may differ after opus conversion)
            actual_path = result.get("file_path", audio_path)
            if not result.get("success") or not os.path.isfile(actual_path):
                logger.warning("Auto voice reply TTS failed: %s", result.get("error"))
                return

            adapter = self.adapters.get(event.source.platform)

            # If connected to a voice channel, play there instead of sending a file
            guild_id = self._get_guild_id(event)
            if (guild_id
                    and hasattr(adapter, "play_in_voice_channel")
                    and hasattr(adapter, "is_in_voice_channel")
                    and adapter.is_in_voice_channel(guild_id)):
                await adapter.play_in_voice_channel(guild_id, actual_path)
            elif adapter and hasattr(adapter, "send_voice"):
                send_kwargs: Dict[str, Any] = {
                    "chat_id": event.source.chat_id,
                    "audio_path": actual_path,
                    "reply_to": event.message_id,
                }
                if event.source.thread_id:
                    send_kwargs["metadata"] = {"thread_id": event.source.thread_id}
                await adapter.send_voice(**send_kwargs)
        except Exception as e:
            logger.warning("Auto voice reply failed: %s", e, exc_info=True)
        finally:
            for p in {audio_path, actual_path} - {None}:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def _deliver_media_from_response(
        self,
        response: str,
        event: MessageEvent,
        adapter,
    ) -> None:
        """Extract MEDIA: tags and local file paths from a response and deliver them.

        Called after streaming has already sent the text to the user, so the
        text itself is already delivered — this only handles file attachments
        that the normal _process_message_background path would have caught.
        """
        from pathlib import Path

        try:
            media_files, _ = adapter.extract_media(response)
            _, cleaned = adapter.extract_images(response)
            local_files, _ = adapter.extract_local_files(cleaned)

            _thread_meta = {"thread_id": event.source.thread_id} if event.source.thread_id else None

            _AUDIO_EXTS = {'.ogg', '.opus', '.mp3', '.wav', '.m4a'}
            _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
            _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

            for media_path, is_voice in media_files:
                try:
                    ext = Path(media_path).suffix.lower()
                    if ext in _AUDIO_EXTS:
                        await adapter.send_voice(
                            chat_id=event.source.chat_id,
                            audio_path=media_path,
                            metadata=_thread_meta,
                        )
                    elif ext in _VIDEO_EXTS:
                        await adapter.send_video(
                            chat_id=event.source.chat_id,
                            video_path=media_path,
                            metadata=_thread_meta,
                        )
                    elif ext in _IMAGE_EXTS:
                        await adapter.send_image_file(
                            chat_id=event.source.chat_id,
                            image_path=media_path,
                            metadata=_thread_meta,
                        )
                    else:
                        await adapter.send_document(
                            chat_id=event.source.chat_id,
                            file_path=media_path,
                            metadata=_thread_meta,
                        )
                except Exception as e:
                    logger.warning("[%s] Post-stream media delivery failed: %s", adapter.name, e)

            for file_path in local_files:
                try:
                    ext = Path(file_path).suffix.lower()
                    if ext in _IMAGE_EXTS:
                        await adapter.send_image_file(
                            chat_id=event.source.chat_id,
                            image_path=file_path,
                            metadata=_thread_meta,
                        )
                    else:
                        await adapter.send_document(
                            chat_id=event.source.chat_id,
                            file_path=file_path,
                            metadata=_thread_meta,
                        )
                except Exception as e:
                    logger.warning("[%s] Post-stream file delivery failed: %s", adapter.name, e)

        except Exception as e:
            logger.warning("Post-stream media extraction failed: %s", e)

    async def _handle_rollback_command(self, event: MessageEvent) -> str:
        """Handle /rollback command — list or restore filesystem checkpoints."""
        from tools.checkpoint_manager import CheckpointManager, format_checkpoint_list

        # Read checkpoint config from config.yaml
        cp_cfg = {}
        try:
            import yaml as _y
            _cfg_path = _hermes_home / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _data = _y.safe_load(_f) or {}
                cp_cfg = _data.get("checkpoints", {})
                if isinstance(cp_cfg, bool):
                    cp_cfg = {"enabled": cp_cfg}
        except Exception:
            pass

        if not cp_cfg.get("enabled", False):
            return (
                "Checkpoints are not enabled.\n"
                "Enable in config.yaml:\n```\ncheckpoints:\n  enabled: true\n```"
            )

        mgr = CheckpointManager(
            enabled=True,
            max_snapshots=cp_cfg.get("max_snapshots", 50),
        )

        cwd = os.getenv("MESSAGING_CWD", str(Path.home()))
        arg = event.get_command_args().strip()

        if not arg:
            checkpoints = mgr.list_checkpoints(cwd)
            return format_checkpoint_list(checkpoints, cwd)

        # Restore by number or hash
        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            return f"No checkpoints found for {cwd}"

        target_hash = None
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(checkpoints):
                target_hash = checkpoints[idx]["hash"]
            else:
                return f"Invalid checkpoint number. Use 1-{len(checkpoints)}."
        except ValueError:
            target_hash = arg

        result = mgr.restore(cwd, target_hash)
        if result["success"]:
            return (
                f"✅ Restored to checkpoint {result['restored_to']}: {result['reason']}\n"
                f"A pre-rollback snapshot was saved automatically."
            )
        return f"❌ {result['error']}"

    async def _handle_background_command(self, event: MessageEvent) -> str:
        """Handle /background <prompt> — run a prompt in a separate background session.

        Spawns a new AIAgent in a background thread with its own session.
        When it completes, sends the result back to the same chat without
        modifying the active session's conversation history.
        """
        prompt = event.get_command_args().strip()
        if not prompt:
            return (
                "Usage: /background <prompt>\n"
                "Example: /background Summarize the top HN stories today\n\n"
                "Runs the prompt in a separate session. "
                "You can keep chatting — the result will appear here when done."
            )

        source = event.source
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{os.urandom(3).hex()}"

        # Fire-and-forget the background task
        _task = asyncio.create_task(
            self._run_background_task(prompt, source, task_id)
        )
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)

        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return f'🔄 Background task started: "{preview}"\nTask ID: {task_id}\nYou can keep chatting — results will appear when done.'

    async def _run_background_task(
        self, prompt: str, source: "SessionSource", task_id: str
    ) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from run_agent import AIAgent

        adapter = self.adapters.get(source.platform)
        if not adapter:
            logger.warning("No adapter for platform %s in background task %s", source.platform, task_id)
            return

        _thread_metadata = {"thread_id": source.thread_id} if source.thread_id else None

        try:
            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                await adapter.send(
                    source.chat_id,
                    f"❌ Background task {task_id} failed: no provider credentials configured.",
                    metadata=_thread_metadata,
                )
                return

            user_config = _load_gateway_config()
            model = _resolve_gateway_model(user_config)
            platform_key = _platform_config_key(source.platform)

            from hermes_cli.tools_config import _get_platform_tools
            enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))

            pr = self._provider_routing
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            reasoning_config = self._load_reasoning_config()
            self._reasoning_config = reasoning_config
            progress_mode = (
                os.getenv("HERMES_TOOL_PROGRESS_MODE")
                or "all"
            )

            def run_sync():
                agent = AIAgent(
                    model=model,
                    **runtime_kwargs,
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=(progress_mode == "verbose"),
                    enabled_toolsets=enabled_toolsets,
                    reasoning_config=reasoning_config,
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=task_id,
                    platform=platform_key,
                    session_db=self._session_db,
                    fallback_model=self._fallback_model,
                )

                return agent.run_conversation(
                    user_message=prompt,
                    task_id=task_id,
                )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, run_sync)

            response = result.get("final_response", "") if result else ""
            if not response and result and result.get("error"):
                response = f"Error: {result['error']}"

            # Extract media files from the response
            if response:
                media_files, response = adapter.extract_media(response)
                images, text_content = adapter.extract_images(response)

                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'

                if text_content:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + text_content,
                        metadata=_thread_metadata,
                    )
                elif not images and not media_files:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + "(No response generated)",
                        metadata=_thread_metadata,
                    )

                # Send extracted images
                for image_url, alt_text in (images or []):
                    try:
                        await adapter.send_image(
                            chat_id=source.chat_id,
                            image_url=image_url,
                            caption=alt_text,
                        )
                    except Exception:
                        pass

                # Send media files
                for media_path in (media_files or []):
                    try:
                        await adapter.send_file(
                            chat_id=source.chat_id,
                            file_path=media_path,
                        )
                    except Exception:
                        pass
            else:
                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)',
                    metadata=_thread_metadata,
                )

        except Exception as e:
            logger.exception("Background task %s failed", task_id)
            try:
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f"❌ Background task {task_id} failed: {e}",
                    metadata=_thread_metadata,
                )
            except Exception:
                pass

    async def _handle_reasoning_command(self, event: MessageEvent) -> str:
        """Handle /reasoning command — manage reasoning effort and display toggle.

        Usage:
            /reasoning              Show current effort level and display state
            /reasoning <level>      Set reasoning effort (none, low, medium, high, xhigh)
            /reasoning show|on      Show model reasoning in responses
            /reasoning hide|off     Hide model reasoning from responses
        """
        import yaml

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / "config.yaml"
        self._reasoning_config = self._load_reasoning_config()
        self._show_reasoning = self._load_show_reasoning()

        def _save_config_key(key_path: str, value):
            """Save a dot-separated key to config.yaml."""
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                keys = key_path.split(".")
                current = user_config
                for k in keys[:-1]:
                    if k not in current or not isinstance(current[k], dict):
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
                return True
            except Exception as e:
                logger.error("Failed to save config key %s: %s", key_path, e)
                return False

        if not args:
            # Show current state
            rc = self._reasoning_config
            if rc is None:
                level = "medium (default)"
            elif rc.get("enabled") is False:
                level = "none (disabled)"
            else:
                level = rc.get("effort", "medium")
            display_state = "on ✓" if self._show_reasoning else "off"
            return (
                "🧠 **Reasoning Settings**\n\n"
                f"**Effort:** `{level}`\n"
                f"**Display:** {display_state}\n\n"
                "_Usage:_ `/reasoning <none|low|medium|high|xhigh|show|hide>`"
            )

        # Display toggle
        if args in ("show", "on"):
            self._show_reasoning = True
            _save_config_key("display.show_reasoning", True)
            return "🧠 ✓ Reasoning display: **ON**\nModel thinking will be shown before each response."

        if args in ("hide", "off"):
            self._show_reasoning = False
            _save_config_key("display.show_reasoning", False)
            return "🧠 ✓ Reasoning display: **OFF**"

        # Effort level change
        effort = args.strip()
        if effort == "none":
            parsed = {"enabled": False}
        elif effort in ("xhigh", "high", "medium", "low", "minimal"):
            parsed = {"enabled": True, "effort": effort}
        else:
            return (
                f"⚠️ Unknown argument: `{effort}`\n\n"
                "**Valid levels:** none, low, minimal, medium, high, xhigh\n"
                "**Display:** show, hide"
            )

        self._reasoning_config = parsed
        if _save_config_key("agent.reasoning_effort", effort):
            return f"🧠 ✓ Reasoning effort set to `{effort}` (saved to config)\n_(takes effect on next message)_"
        else:
            return f"🧠 ✓ Reasoning effort set to `{effort}` (this session only)"

    async def _handle_verbose_command(self, event: MessageEvent) -> str:
        """Handle /verbose command — cycle tool progress display mode.

        Gated by ``display.tool_progress_command`` in config.yaml (default off).
        When enabled, cycles the tool progress mode through off → new → all →
        verbose → off, same as the CLI.
        """
        import yaml

        config_path = _hermes_home / "config.yaml"

        # --- check config gate ------------------------------------------------
        try:
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            gate_enabled = user_config.get("display", {}).get("tool_progress_command", False)
        except Exception:
            gate_enabled = False

        if not gate_enabled:
            return (
                "The `/verbose` command is not enabled for messaging platforms.\n\n"
                "Enable it in `config.yaml`:\n```yaml\n"
                "display:\n  tool_progress_command: true\n```"
            )

        # --- cycle mode -------------------------------------------------------
        cycle = ["off", "new", "all", "verbose"]
        descriptions = {
            "off": "⚙️ Tool progress: **OFF** — no tool activity shown.",
            "new": "⚙️ Tool progress: **NEW** — shown when tool changes.",
            "all": "⚙️ Tool progress: **ALL** — every tool call shown.",
            "verbose": "⚙️ Tool progress: **VERBOSE** — full args and results.",
        }

        raw_progress = user_config.get("display", {}).get("tool_progress", "all")
        # YAML 1.1 parses bare "off" as boolean False — normalise back
        if raw_progress is False:
            current = "off"
        elif raw_progress is True:
            current = "all"
        else:
            current = str(raw_progress).lower()
        if current not in cycle:
            current = "all"
        idx = (cycle.index(current) + 1) % len(cycle)
        new_mode = cycle[idx]

        # Save to config.yaml
        try:
            if "display" not in user_config or not isinstance(user_config.get("display"), dict):
                user_config["display"] = {}
            user_config["display"]["tool_progress"] = new_mode
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
            return f"{descriptions[new_mode]}\n_(saved to config — takes effect on next message)_"
        except Exception as e:
            logger.warning("Failed to save tool_progress mode: %s", e)
            return f"{descriptions[new_mode]}\n_(could not save to config: {e})_"

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

            # Resolve model from config (same reason as memory flush above).
            model = _resolve_gateway_model()

            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            original_count = len(msgs)
            approx_tokens = estimate_messages_tokens_rough(msgs)

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=4,
                quiet_mode=True,
                enabled_toolsets=["memory"],
                session_id=session_entry.session_id,
            )
            tmp_agent._print_fn = lambda *a, **kw: None

            loop = asyncio.get_event_loop()
            compressed, _ = await loop.run_in_executor(
                None,
                lambda: tmp_agent._compress_context(msgs, "", approx_tokens=approx_tokens)
            )

            # _compress_context already calls end_session() on the old session
            # (preserving its full transcript in SQLite) and creates a new
            # session_id for the continuation.  Write the compressed messages
            # into the NEW session so the original history stays searchable.
            new_session_id = tmp_agent.session_id
            if new_session_id != session_entry.session_id:
                session_entry.session_id = new_session_id
                self.session_store._save()

            self.session_store.rewrite_transcript(new_session_id, compressed)
            # Reset stored token count — transcript changed, old value is stale
            self.session_store.update_session(
                session_entry.session_key, last_prompt_tokens=0
            )
            new_count = len(compressed)
            new_tokens = estimate_messages_tokens_rough(compressed)

            return (
                f"🗜️ Compressed: {original_count} → {new_count} messages\n"
                f"~{approx_tokens:,} → ~{new_tokens:,} tokens"
            )
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return f"Compression failed: {e}"

    async def _handle_title_command(self, event: MessageEvent) -> str:
        """Handle /title command — set or show the current session's title."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_id = session_entry.session_id

        if not self._session_db:
            return "Session database not available."

        # Ensure session exists in SQLite DB (it may only exist in session_store
        # if this is the first command in a new session)
        existing_title = self._session_db.get_session_title(session_id)
        if existing_title is None:
            # Session doesn't exist in DB yet — create it
            try:
                self._session_db.create_session(
                    session_id=session_id,
                    source=source.platform.value if source.platform else "unknown",
                    user_id=source.user_id,
                )
            except Exception:
                pass  # Session might already exist, ignore errors

        title_arg = event.get_command_args().strip()
        if title_arg:
            # Sanitize the title before setting
            try:
                sanitized = self._session_db.sanitize_title(title_arg)
            except ValueError as e:
                return f"⚠️ {e}"
            if not sanitized:
                return "⚠️ Title is empty after cleanup. Please use printable characters."
            # Set the title
            try:
                if self._session_db.set_session_title(session_id, sanitized):
                    return f"✏️ Session title set: **{sanitized}**"
                else:
                    return "Session not found in database."
            except ValueError as e:
                return f"⚠️ {e}"
        else:
            # Show the current title
            title = self._session_db.get_session_title(session_id)
            if title:
                return f"📌 Session title: **{title}**"
            else:
                return "No title set. Usage: `/title My Session Name`"

    async def _handle_resume_command(self, event: MessageEvent) -> str:
        """Handle /resume command — switch to a previously-named session."""
        if not self._session_db:
            return "Session database not available."

        source = event.source
        session_key = self._session_key_for_source(source)
        name = event.get_command_args().strip()

        if not name:
            # List recent titled sessions for this user/platform
            try:
                user_source = source.platform.value if source.platform else None
                sessions = self._session_db.list_sessions_rich(
                    source=user_source, limit=10
                )
                titled = [s for s in sessions if s.get("title")]
                if not titled:
                    return (
                        "No named sessions found.\n"
                        "Use `/title My Session` to name your current session, "
                        "then `/resume My Session` to return to it later."
                    )
                lines = ["📋 **Named Sessions**\n"]
                for s in titled[:10]:
                    title = s["title"]
                    preview = s.get("preview", "")[:40]
                    preview_part = f" — _{preview}_" if preview else ""
                    lines.append(f"• **{title}**{preview_part}")
                lines.append("\nUsage: `/resume <session name>`")
                return "\n".join(lines)
            except Exception as e:
                logger.debug("Failed to list titled sessions: %s", e)
                return f"Could not list sessions: {e}"

        # Resolve the name to a session ID
        target_id = self._session_db.resolve_session_by_title(name)
        if not target_id:
            return (
                f"No session found matching '**{name}**'.\n"
                "Use `/resume` with no arguments to see available sessions."
            )

        # Check if already on that session
        current_entry = self.session_store.get_or_create_session(source)
        if current_entry.session_id == target_id:
            return f"📌 Already on session **{name}**."

        # Flush memories for current session before switching
        try:
            _flush_task = asyncio.create_task(
                self._async_flush_memories(current_entry.session_id, session_key)
            )
            self._background_tasks.add(_flush_task)
            _flush_task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.debug("Memory flush on resume failed: %s", e)

        self._shutdown_gateway_honcho(session_key)

        # Clear any running agent for this session key
        if session_key in self._running_agents:
            del self._running_agents[session_key]

        # Switch the session entry to point at the old session
        new_entry = self.session_store.switch_session(session_key, target_id)
        if not new_entry:
            return "Failed to switch session."

        # Get the title for confirmation
        title = self._session_db.get_session_title(target_id) or name

        # Count messages for context
        history = self.session_store.load_transcript(target_id)
        msg_count = len([m for m in history if m.get("role") == "user"]) if history else 0
        msg_part = f" ({msg_count} message{'s' if msg_count != 1 else ''})" if msg_count else ""

        return f"↻ Resumed session **{title}**{msg_part}. Conversation restored."

    async def _handle_usage_command(self, event: MessageEvent) -> str:
        """Handle /usage command -- show token usage for the session's last agent run."""
        source = event.source
        session_key = self._session_key_for_source(source)

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
                pct = min(100, ctx.last_prompt_tokens / ctx.context_length * 100) if ctx.context_length else 0
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

    async def _handle_insights_command(self, event: MessageEvent) -> str:
        """Handle /insights command -- show usage insights and analytics."""
        import asyncio as _asyncio

        args = event.get_command_args().strip()
        days = 30
        source = None

        # Parse simple args: /insights 7  or  /insights --days 7
        if args:
            parts = args.split()
            i = 0
            while i < len(parts):
                if parts[i] == "--days" and i + 1 < len(parts):
                    try:
                        days = int(parts[i + 1])
                    except ValueError:
                        return f"Invalid --days value: {parts[i + 1]}"
                    i += 2
                elif parts[i] == "--source" and i + 1 < len(parts):
                    source = parts[i + 1]
                    i += 2
                elif parts[i].isdigit():
                    days = int(parts[i])
                    i += 1
                else:
                    i += 1

        try:
            from hermes_state import SessionDB
            from agent.insights import InsightsEngine

            loop = _asyncio.get_event_loop()

            def _run_insights():
                db = SessionDB()
                engine = InsightsEngine(db)
                report = engine.generate(days=days, source=source)
                result = engine.format_gateway(report)
                db.close()
                return result

            return await loop.run_in_executor(None, _run_insights)
        except Exception as e:
            logger.error("Insights command error: %s", e, exc_info=True)
            return f"Error generating insights: {e}"

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

    # ------------------------------------------------------------------
    # /approve & /deny — explicit dangerous-command approval
    # ------------------------------------------------------------------

    _APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes

    async def _handle_approve_command(self, event: MessageEvent) -> str:
        """Handle /approve command — execute a pending dangerous command."""
        source = event.source
        session_key = self._session_key_for_source(source)

        if session_key not in self._pending_approvals:
            return "No pending command to approve."

        import time as _time

        approval = self._pending_approvals[session_key]
        ts = approval.get("timestamp", 0)
        if _time.time() - ts > self._APPROVAL_TIMEOUT_SECONDS:
            self._pending_approvals.pop(session_key, None)
            return "⚠️ Approval expired (timed out after 5 minutes). Ask the agent to try again."

        self._pending_approvals.pop(session_key)
        cmd = approval["command"]
        pattern_keys = approval.get("pattern_keys", [])
        if not pattern_keys:
            pk = approval.get("pattern_key", "")
            pattern_keys = [pk] if pk else []

        args = event.get_command_args().strip().lower()
        from tools.approval import approve_permanent, approve_session

        if args in ("always", "permanent", "permanently"):
            for pk in pattern_keys:
                approve_permanent(pk)
            scope_msg = " (pattern approved permanently)"
        elif args in ("session", "ses"):
            for pk in pattern_keys:
                approve_session(session_key, pk)
            scope_msg = " (pattern approved for this session)"
        else:
            for pk in pattern_keys:
                approve_session(session_key, pk)
            scope_msg = ""

        logger.info("User approved dangerous command via /approve: %s...%s", cmd[:60], scope_msg)
        from tools.terminal_tool import terminal_tool

        result = terminal_tool(command=cmd, force=True)
        return f"✅ Command approved and executed{scope_msg}.\n\n```\n{result[:3500]}\n```"

    async def _handle_deny_command(self, event: MessageEvent) -> str:
        """Handle /deny command — reject a pending dangerous command."""
        source = event.source
        session_key = self._session_key_for_source(source)

        if session_key not in self._pending_approvals:
            return "No pending command to deny."

        self._pending_approvals.pop(session_key)
        logger.info("User denied dangerous command via /deny")
        return "❌ Command denied."

    async def _handle_update_command(self, event: MessageEvent) -> str:
        """Handle /update command — update Hermes Agent to the latest version.

        Spawns ``hermes update`` in a separate systemd scope so it survives the
        gateway restart that ``hermes update`` may trigger at the end. Marker
        files are written so either the current gateway process or the next one
        can notify the user when the update finishes.
        """
        import json
        import shutil
        import subprocess
        from datetime import datetime

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        git_dir = os.path.join(project_root, ".git")

        if not os.path.exists(git_dir):
            return "✗ Not a git repository — cannot update."

        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            return (
                "✗ Could not locate the `hermes` command. "
                "Hermes is running, but the update command could not find the "
                "executable on PATH or via the current Python interpreter. "
                "Try running `hermes update` manually in your terminal."
            )

        pending_path = _hermes_home / ".update_pending.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"
        pending = {
            "platform": event.source.platform.value,
            "chat_id": event.source.chat_id,
            "user_id": event.source.user_id,
            "timestamp": datetime.now().isoformat(),
        }
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        exit_code_path.unlink(missing_ok=True)

        # Spawn `hermes update` in a separate cgroup so it survives gateway
        # restart. systemd-run --user --scope creates a transient scope unit.
        hermes_cmd_str = " ".join(shlex.quote(part) for part in hermes_cmd)
        update_cmd = (
            f"{hermes_cmd_str} update > {shlex.quote(str(output_path))} 2>&1; "
            f"status=$?; printf '%s' \"$status\" > {shlex.quote(str(exit_code_path))}"
        )
        try:
            systemd_run = shutil.which("systemd-run")
            if systemd_run:
                subprocess.Popen(
                    [systemd_run, "--user", "--scope",
                     "--unit=hermes-update", "--",
                     "bash", "-c", update_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            elif os.name == "nt":
                python_runner = (
                    "import pathlib, subprocess, sys; "
                    "hermes = sys.argv[1:-2]; "
                    "output_path = pathlib.Path(sys.argv[-2]); "
                    "exit_code_path = pathlib.Path(sys.argv[-1]); "
                    "output_path.parent.mkdir(parents=True, exist_ok=True); "
                    "with open(output_path, 'w', encoding='utf-8', errors='replace') as out: "
                    "    rc = subprocess.run(hermes + ['update'], stdout=out, stderr=subprocess.STDOUT).returncode; "
                    "exit_code_path.write_text(str(rc), encoding='utf-8')"
                )
                creationflags = 0
                windows_flag_defaults = {
                    "DETACHED_PROCESS": 0x00000008,
                    "CREATE_NEW_PROCESS_GROUP": 0x00000200,
                    "CREATE_NO_WINDOW": 0x08000000,
                }
                for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                    creationflags |= getattr(subprocess, flag_name, windows_flag_defaults[flag_name])
                subprocess.Popen(
                    [sys.executable, "-c", python_runner, *hermes_cmd, str(output_path), str(exit_code_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    creationflags=creationflags,
                )
            else:
                # Fallback: best-effort detach with start_new_session
                subprocess.Popen(
                    ["bash", "-c", f"nohup {update_cmd} &"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except Exception as e:
            pending_path.unlink(missing_ok=True)
            exit_code_path.unlink(missing_ok=True)
            return f"✗ Failed to start update: {e}"

        self._schedule_update_notification_watch()
        return "⚕ Starting Hermes update… I'll notify you when it's done."

    def _schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, "_update_notification_task", None)
        if existing_task and not existing_task.done():
            return

        try:
            self._update_notification_task = asyncio.create_task(
                self._watch_for_update_completion()
            )
        except RuntimeError:
            logger.debug("Skipping update notification watcher: no running event loop")

    async def _watch_for_update_completion(
        self,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> None:
        """Wait for ``hermes update`` to finish, then send its notification."""
        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        exit_code_path = _hermes_home / ".update_exit_code"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while (pending_path.exists() or claimed_path.exists()) and loop.time() < deadline:
            if exit_code_path.exists():
                await self._send_update_notification()
                return
            await asyncio.sleep(poll_interval)

        if (pending_path.exists() or claimed_path.exists()) and not exit_code_path.exists():
            logger.warning("Update watcher timed out waiting for completion marker")
            exit_code_path.write_text("124", encoding="utf-8")
            await self._send_update_notification()

    async def _send_update_notification(self) -> bool:
        """If an update finished, notify the user.

        Returns False when the update is still running so a caller can retry
        later. Returns True after a definitive send/skip decision.
        """
        import json
        import re as _re

        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"

        if not pending_path.exists() and not claimed_path.exists():
            return False

        cleanup = True
        active_pending_path = claimed_path
        try:
            if pending_path.exists():
                try:
                    pending_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return True
            elif not claimed_path.exists():
                return True

            pending = json.loads(claimed_path.read_text(encoding="utf-8"))
            platform_str = pending.get("platform")
            chat_id = pending.get("chat_id")

            if not exit_code_path.exists():
                logger.info("Update notification deferred: update still running")
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False

            exit_code_raw = exit_code_path.read_text(encoding="utf-8").strip() or "1"
            exit_code = int(exit_code_raw)

            # Read the captured update output
            output = ""
            if output_path.exists():
                output = output_path.read_text(encoding="utf-8", errors="replace")

            # Resolve adapter
            platform = Platform(platform_str)
            adapter = self.adapters.get(platform)

            if adapter and chat_id:
                # Strip ANSI escape codes for clean display
                output = _re.sub(r'\x1b\[[0-9;]*m', '', output).strip()
                if output:
                    if len(output) > 3500:
                        output = "…" + output[-3500:]
                    if exit_code == 0:
                        msg = f"✅ Hermes update finished.\n\n```\n{output}\n```"
                    else:
                        msg = f"❌ Hermes update failed.\n\n```\n{output}\n```"
                else:
                    if exit_code == 0:
                        msg = "✅ Hermes update finished successfully."
                    else:
                        msg = "❌ Hermes update failed. Check the gateway logs or run `hermes update` manually for details."
                await adapter.send(chat_id, msg)
                logger.info(
                    "Sent post-update notification to %s:%s (exit=%s)",
                    platform_str,
                    chat_id,
                    exit_code,
                )
        except Exception as e:
            logger.warning("Post-update notification failed: %s", e)
        finally:
            if cleanup:
                active_pending_path.unlink(missing_ok=True)
                claimed_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                exit_code_path.unlink(missing_ok=True)

        return True

    def _set_session_env(self, context: SessionContext) -> None:
        """Set environment variables for the current session."""
        os.environ["HERMES_SESSION_PLATFORM"] = context.source.platform.value
        os.environ["HERMES_SESSION_CHAT_ID"] = context.source.chat_id
        if context.source.chat_name:
            os.environ["HERMES_SESSION_CHAT_NAME"] = context.source.chat_name
        if context.source.thread_id:
            os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)
    
    def _clear_session_env(self) -> None:
        """Clear session environment variables."""
        for var in ["HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_NAME", "HERMES_SESSION_THREAD_ID"]:
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
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            The enriched message string with transcriptions prepended.
        """
        if not getattr(self.config, "stt_enabled", True):
            disabled_note = "[The user sent voice message(s), but transcription is disabled in config.]"
            if user_text:
                return f"{disabled_note}\n\n{user_text}"
            return disabled_note

        from tools.transcription_tools import transcribe_audio, get_stt_model_from_config
        import asyncio

        stt_model = get_stt_model_from_config()

        enriched_parts = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path, model=stt_model)
                if result["success"]:
                    transcript = result["transcript"]
                    enriched_parts.append(
                        f'[The user sent a voice message~ '
                        f'Here\'s what they said: "{transcript}"]'
                    )
                else:
                    error = result.get("error", "unknown error")
                    if (
                        "No STT provider" in error
                        or error.startswith("Neither VOICE_TOOLS_OPENAI_KEY nor OPENAI_API_KEY is set")
                    ):
                        enriched_parts.append(
                            "[The user sent a voice message but I can't listen "
                            "to it right now~ No STT provider is configured "
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

        Notification mode (from ``display.background_process_notifications``):
          - ``all``    — running-output updates + final message
          - ``result`` — final completion message only
          - ``error``  — final message only when exit code != 0
          - ``off``    — no messages at all
        """
        from tools.process_registry import process_registry

        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        session_key = watcher.get("session_key", "")
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        notify_mode = self._load_background_notifications_mode()

        logger.debug("Process watcher started: %s (every %ss, notify=%s)",
                      session_id, interval, notify_mode)

        if notify_mode == "off":
            # Still wait for the process to exit so we can log it, but don't
            # push any messages to the user.
            while True:
                await asyncio.sleep(interval)
                session = process_registry.get(session_id)
                if session is None or session.exited:
                    break
            logger.debug("Process watcher ended (silent): %s", session_id)
            return

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
                # Decide whether to notify based on mode
                should_notify = (
                    notify_mode in ("all", "result")
                    or (notify_mode == "error" and session.exit_code not in (0, None))
                )
                if should_notify:
                    new_output = session.output_buffer[-1000:] if session.output_buffer else ""
                    message_text = (
                        f"[Background process {session_id} finished with exit code {session.exit_code}~ "
                        f"Here's the final output:\n{new_output}]"
                    )
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter and chat_id:
                        try:
                            send_meta = {"thread_id": thread_id} if thread_id else None
                            await adapter.send(chat_id, message_text, metadata=send_meta)
                        except Exception as e:
                            logger.error("Watcher delivery error: %s", e)
                break

            elif has_new_output and notify_mode == "all":
                # New output available -- deliver status update (only in "all" mode)
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
                        send_meta = {"thread_id": thread_id} if thread_id else None
                        await adapter.send(chat_id, message_text, metadata=send_meta)
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)

        logger.debug("Process watcher ended: %s", session_id)

    _MAX_INTERRUPT_DEPTH = 3  # Cap recursive interrupt handling (#816)

    @staticmethod
    def _agent_config_signature(
        model: str,
        runtime: dict,
        enabled_toolsets: list,
        ephemeral_prompt: str,
    ) -> str:
        """Compute a stable string key from agent config values.

        When this signature changes between messages, the cached AIAgent is
        discarded and rebuilt.  When it stays the same, the cached agent is
        reused — preserving the frozen system prompt and tool schemas for
        prompt cache hits.
        """
        import hashlib, json as _j

        # Fingerprint the FULL credential string instead of using a short
        # prefix. OAuth/JWT-style tokens frequently share a common prefix
        # (e.g. "eyJhbGci"), which can cause false cache hits across auth
        # switches if only the first few characters are considered.
        _api_key = str(runtime.get("api_key", "") or "")
        _api_key_fingerprint = hashlib.sha256(_api_key.encode()).hexdigest() if _api_key else ""

        blob = _j.dumps(
            [
                model,
                _api_key_fingerprint,
                runtime.get("base_url", ""),
                runtime.get("provider", ""),
                runtime.get("api_mode", ""),
                sorted(enabled_toolsets) if enabled_toolsets else [],
                # reasoning_config excluded — it's set per-message on the
                # cached agent and doesn't affect system prompt or tools.
                ephemeral_prompt or "",
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session (called on /new, /model, etc)."""
        _lock = getattr(self, "_agent_cache_lock", None)
        if _lock:
            with _lock:
                self._agent_cache.pop(session_key, None)
    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
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
        
        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)

        from hermes_cli.tools_config import _get_platform_tools
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        browser_sidecar_cfg = {}
        if isinstance(user_config, dict):
            browser_sidecar_cfg = user_config.get("browser_sidecar", {}) or {}

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

        browser_sidecar_max_iterations = None
        browser_sidecar_max_tool_calls = None

        # Browser sidecar sessions default to dedicated sidecar toolsets.
        # By default this includes browser automation and excludes delegation.
        if self._is_browser_bridge_source(source):
            configured_sidecar_toolsets = browser_sidecar_cfg.get("toolsets")
            normalized_sidecar_toolsets = []
            if isinstance(configured_sidecar_toolsets, list):
                normalized_sidecar_toolsets = [
                    str(toolset).strip()
                    for toolset in configured_sidecar_toolsets
                    if str(toolset).strip()
                ]

            if normalized_sidecar_toolsets:
                enabled_toolsets = normalized_sidecar_toolsets
            else:
                allow_sidecar_delegation = _as_bool(
                    browser_sidecar_cfg.get(
                        "allow_delegation",
                        os.getenv("HERMES_BROWSER_SIDECAR_ALLOW_DELEGATION", ""),
                    ),
                    default=False,
                )
                enabled_toolsets = [
                    "hermes-sidecar-delegating" if allow_sidecar_delegation else "hermes-sidecar"
                ]

            raw_sidecar_max_turns = browser_sidecar_cfg.get("max_turns")
            try:
                parsed_sidecar_max_turns = int(raw_sidecar_max_turns)
            except (TypeError, ValueError):
                parsed_sidecar_max_turns = 0
            if parsed_sidecar_max_turns > 0:
                browser_sidecar_max_iterations = parsed_sidecar_max_turns

            raw_sidecar_max_tool_calls = browser_sidecar_cfg.get("max_tool_calls")
            try:
                parsed_sidecar_max_tool_calls = int(raw_sidecar_max_tool_calls)
            except (TypeError, ValueError):
                parsed_sidecar_max_tool_calls = 0
            if parsed_sidecar_max_tool_calls > 0:
                browser_sidecar_max_tool_calls = parsed_sidecar_max_tool_calls

            logger.info(
                "Browser sidecar policy active: toolsets=%s max_turns=%s max_tool_calls=%s",
                enabled_toolsets,
                browser_sidecar_max_iterations if browser_sidecar_max_iterations is not None else "default",
                browser_sidecar_max_tool_calls if browser_sidecar_max_tool_calls is not None else "default",
            )
        _progress_cfg = user_config.get("display", {}) if isinstance(user_config.get("display"), dict) else {}
        # Tool progress mode from config.yaml: "all", "new", "verbose", "off"
        # Falls back to env vars for backward compatibility.
        # YAML 1.1 parses bare `off` as boolean False — normalise before
        # the `or` chain so it doesn't silently fall through to "all".
        _raw_tp = user_config.get("display", {}).get("tool_progress")
        if _raw_tp is False:
            _raw_tp = "off"
        _progress_mode_env = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        if _raw_tp is None and not _progress_mode_env and source.platform == Platform.DISCORD:
            # Discord is much easier to follow when progress is action-oriented
            # rather than every repeated tool preview / thinking update.
            progress_mode = "new"
        else:
            progress_mode = (
                _raw_tp
                or _progress_mode_env
                or "all"
            )
        progress_style = (
            _progress_cfg.get("tool_progress_style")
            or os.getenv("HERMES_TOOL_PROGRESS_STYLE")
            or "single"
        ).strip().lower()
        if progress_style not in {"feed", "single"}:
            progress_style = "single"

        try:
            progress_rolling_entries = int(
                _progress_cfg.get("tool_progress_rolling_entries")
                or os.getenv("HERMES_TOOL_PROGRESS_ROLLING_ENTRIES", "4")
            )
        except Exception:
            progress_rolling_entries = 4
        progress_rolling_entries = max(1, min(progress_rolling_entries, 8))

        try:
            progress_embed_max_chars = int(
                os.getenv("HERMES_TOOL_PROGRESS_EMBED_MAX_CHARS", "3800")
            )
        except Exception:
            progress_embed_max_chars = 3800
        progress_embed_max_chars = max(1200, min(progress_embed_max_chars, 3900))

        tool_progress_enabled = progress_mode != "off"

        _has_messaging_adapter = bool(self.adapters.get(source.platform))
        _bridge_sidecar = self._is_browser_bridge_source(source)
        # Queue only when a messaging adapter consumes progress (Discord, etc.). Browser sidecar
        # uses synchronous fan-out from _push_recent_event into _browser_bridge_progress instead.
        progress_queue = queue.Queue() if (tool_progress_enabled and _has_messaging_adapter) else None
        last_tool = [None]  # Mutable container for tracking in closure
        last_terminal_progress = [None]
        progress_state = {
            "started_at": time.monotonic(),
            "phase": "thinking",
            "last_detail": "Queued request...",
            "tool_calls": 0,
            "updates": 0,
            "recent_events": [],
            "all_events": [],
            "done": False,
        }

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
                status = str(parsed.get("status") or "").strip()
                details = str(parsed.get("details") or "").strip()
                if status:
                    parts.append(f"status={status}")
                if details:
                    parts.append(details)
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

        def _push_recent_event(detail: str) -> None:
            text = (detail or "").strip()
            if not text:
                return
            ts = datetime.now().strftime("%H:%M:%S")
            event_line = f"`{ts}` {text}"
            all_events = progress_state.setdefault("all_events", [])
            if all_events and all_events[-1] == event_line:
                return
            all_events.append(event_line)
            recent_events = progress_state.setdefault("recent_events", [])
            if recent_events and recent_events[-1] == event_line:
                return
            recent_events.append(event_line)
            while len(recent_events) > progress_rolling_entries:
                recent_events.pop(0)
            # Sidecar extension polls progress.recent_events — mirror Discord-style tool lines here.
            if session_key and _bridge_sidecar:
                bridge_line = f"[{ts}] {text}"
                bridge_events = self._append_browser_bridge_progress_event(
                    session_key,
                    bridge_line,
                    limit=_BROWSER_BRIDGE_PROGRESS_EVENT_CAP,
                )
                self._set_browser_bridge_progress(
                    session_key,
                    running=True,
                    detail=text[:500] if text else "",
                    recent_events=bridge_events,
                )

        def _emit_gateway_terminal_progress(detail: str, phase: str | None = None) -> None:
            text = (detail or "").strip()
            if not text:
                return
            rendered = text if not phase else f"[{phase}] {text}"
            if rendered == last_terminal_progress[0]:
                return
            last_terminal_progress[0] = rendered
            _write_gateway_console_progress_line(
                f"[gateway-progress][session {session_id}] {rendered}"
            )
            logger.info(
                "[gateway-progress][session %s] %s",
                session_id,
                rendered,
            )

        def _set_progress_state(*, phase: str | None = None, detail: str | None = None, tool_call: bool = False):
            if phase:
                progress_state["phase"] = phase
            if detail:
                progress_state["last_detail"] = detail
                _push_recent_event(detail)
                _emit_gateway_terminal_progress(detail, progress_state.get("phase"))
            if tool_call:
                progress_state["tool_calls"] += 1
            progress_state["updates"] += 1
            if progress_queue:
                progress_queue.put("__tick__")

        def _build_progress_message() -> str:
            elapsed = int(max(0, time.monotonic() - progress_state["started_at"]))
            phase = progress_state.get("phase", "thinking")
            phase_label = {
                "starting": "starting",
                "thinking": "thinking",
                "tool": "running commands",
                "finalizing": "finalizing",
                "completed": "finished",
                "failed": "failed",
                "interrupted": "interrupted",
                "timed_out": "timed out",
            }.get(phase, "working")
            phase_emoji = {
                "starting": "🚀",
                "thinking": "💡",
                "tool": "🛠️",
                "finalizing": "🧾",
                "completed": "✅",
                "failed": "❌",
                "interrupted": "⚡",
                "timed_out": "⏱️",
            }.get(phase, "⚙️")
            header = f"{phase_emoji} Hermes is {phase_label}... ({elapsed}s)"
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

        def _build_progress_feed_page(lines: list[str]) -> str:
            elapsed = int(max(0, time.monotonic() - progress_state["started_at"]))
            phase = progress_state.get("phase", "thinking")
            phase_label = {
                "starting": "starting",
                "thinking": "thinking",
                "tool": "running commands",
                "finalizing": "finalizing",
                "completed": "finished",
                "failed": "failed",
                "interrupted": "interrupted",
                "timed_out": "timed out",
            }.get(phase, "working")
            phase_emoji = {
                "starting": "🚀",
                "thinking": "💡",
                "tool": "🛠️",
                "finalizing": "🧾",
                "completed": "✅",
                "failed": "❌",
                "interrupted": "⚡",
                "timed_out": "⏱️",
            }.get(phase, "⚙️")
            header = f"{phase_emoji} Hermes is {phase_label}... ({elapsed}s)"
            footer = f"Tools: {progress_state['tool_calls']} | Updates: {progress_state['updates']}"
            body = "\n".join(f"• {line}" for line in lines)
            return f"{header}\n{body}\n{footer}"

        def _trim_progress_event_for_feed(line: str) -> str:
            header = "⚙️ Hermes is working... (9999s)"
            footer = "Tools: 9999 | Updates: 9999"
            overhead = len(f"{header}\n• \n{footer}")
            available = max(80, progress_embed_max_chars - overhead)
            if len(line) <= available:
                return line
            return line[: max(0, available - 24)] + "... [trimmed for embed]"

        def progress_callback(tool_name: str, preview: str = None, args: dict = None):
            """Callback invoked by agent when tool progress changes."""
            if not progress_queue and not _bridge_sidecar:
                return

            suppress_thinking_progress = (
                source.platform == Platform.DISCORD and progress_mode != "verbose"
            )

            # Optional completion payloads from newer run_agent variants.
            if tool_name == "_tool_result":
                payload = args or {}
                completed_tool = str(payload.get("tool") or preview or "tool")
                duration = payload.get("duration_seconds")
                is_error = bool(payload.get("is_error"))
                status_suffix = str(payload.get("status_suffix") or "").strip()
                result_text = str(payload.get("result") or "").strip()
                status_emoji = "❌" if is_error else "✅"
                duration_text = ""
                if isinstance(duration, (int, float)):
                    duration_text = f" in {float(duration):.2f}s"
                detail = f"{status_emoji} {completed_tool} finished{duration_text}"
                summary_suffix = _summarize_result_for_status(result_text, is_error)
                if status_suffix:
                    detail = f"{detail} {status_suffix}"
                if summary_suffix:
                    detail = f"{detail} | {summary_suffix}"
                _set_progress_state(phase="tool", detail=detail, tool_call=False)
                return

            if tool_name == "_tool_waiting":
                payload = args or {}
                waiting_tool = str(payload.get("tool") or preview or "tool")
                elapsed = payload.get("elapsed_seconds")
                waiting_preview = str(payload.get("preview") or waiting_tool).strip()
                elapsed_text = ""
                if isinstance(elapsed, (int, float)):
                    elapsed_text = f" ({int(max(1, round(float(elapsed))))}s)"
                detail = f"⏳ {waiting_tool} still running{elapsed_text}"
                if waiting_preview and waiting_preview != waiting_tool:
                    detail = f"{detail} | {waiting_preview}"
                _set_progress_state(phase="tool", detail=detail, tool_call=False)
                return

            # "new" mode: only report when tool changes.
            if progress_mode == "new" and tool_name == last_tool[0]:
                return
            last_tool[0] = tool_name

            from agent.display import get_tool_emoji
            emoji = get_tool_emoji(tool_name, default="⚙️")

            if tool_name == "_thinking":
                if suppress_thinking_progress:
                    return
                _set_progress_state(phase="thinking", detail=f"💡 thinking: {preview or 'working...'}")
                return

            # Verbose mode: include argument keys/preview.
            if progress_mode == "verbose" and args:
                try:
                    if tool_name == "terminal":
                        command = str(args.get("command") or "").strip()
                        workdir = str(args.get("workdir") or "").strip()
                        timeout = args.get("timeout")
                        parts = [f"{emoji} terminal"]
                        if workdir:
                            parts.append(f"cwd={workdir}")
                        if timeout not in (None, ""):
                            parts.append(f"timeout={timeout}")
                        if command:
                            max_command_chars = max(240, min(progress_embed_max_chars - 250, 1800))
                            if len(command) > max_command_chars:
                                command = command[: max_command_chars - 3] + "..."
                            parts.append(f"cmd={command}")
                        detail = " | ".join(parts)
                        _set_progress_state(phase="tool", detail=detail, tool_call=True)
                        return
                    args_str = json.dumps(args, ensure_ascii=False, default=str)
                except Exception:
                    args_str = str(args)
                max_args_chars = max(240, min(progress_embed_max_chars - 250, 1200))
                if len(args_str) > max_args_chars:
                    args_str = args_str[: max_args_chars - 3] + "..."
                detail = f"{emoji} {tool_name}({list(args.keys())}) {args_str}"
                _set_progress_state(phase="tool", detail=detail, tool_call=True)
                return

            if preview:
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                detail = f"{emoji} {tool_name}: \"{preview}\""
            else:
                detail = f"{emoji} {tool_name}..."
            _set_progress_state(phase="tool", detail=detail, tool_call=True)

        def progress_thinking_callback(text: str) -> None:
            cleaned = str(text or "").strip()
            if not cleaned:
                return
            progress_callback("_thinking", cleaned)

        def progress_tool_gen_callback(tool_name: str) -> None:
            cleaned = str(tool_name or "").strip()
            if not cleaned:
                return
            progress_callback(cleaned)

        # Background task to send progress messages.
        # Threading metadata is platform-specific:
        # - Slack DM threading needs event_message_id fallback (reply thread)
        # - Telegram uses message_thread_id only for forum topics; passing a
        #   normal DM/group message id as thread_id causes send failures
        # - Discord benefits from tool_progress metadata to suppress listen controls
        if source.platform == Platform.SLACK:
            _progress_thread_id = source.thread_id or event_message_id
            _progress_metadata = {"thread_id": _progress_thread_id} if _progress_thread_id else None
        elif source.platform == Platform.DISCORD:
            _progress_thread_id = source.thread_id
            _progress_metadata = {"tool_progress": True}
            if _progress_thread_id:
                _progress_metadata["thread_id"] = _progress_thread_id
        else:
            _progress_thread_id = source.thread_id
            _progress_metadata = (
                {"tool_progress": True, "thread_id": _progress_thread_id}
                if _progress_thread_id else None
            )

        async def send_progress_messages():
            if not progress_queue:
                return

            adapter = self.adapters.get(source.platform)
            if not adapter:
                return

            def _is_retryable_progress_edit_error(error_text: str | None) -> bool:
                """Treat transport/server issues as transient so we keep one live message."""
                err = (error_text or "").lower()
                permanent_markers = (
                    "unknown message",
                    "not found",
                    "forbidden",
                    "missing access",
                    "missing permissions",
                    "cannot edit",
                    "error code: 10008",
                    "error code: 50013",
                )
                if any(marker in err for marker in permanent_markers):
                    return False
                transient_markers = (
                    "503",
                    "502",
                    "504",
                    "service unavailable",
                    "gateway timeout",
                    "upstream connect error",
                    "remote connection failure",
                    "reset before headers",
                    "timeout",
                    "timed out",
                    "temporarily unavailable",
                    "connection reset",
                    "try again",
                    "rate limit",
                    "429",
                )
                if any(marker in err for marker in transient_markers):
                    return True
                # Default to retryable so one flaky edit does not fan out into message spam.
                return True

            progress_msg_id = None
            feed_page_ids: list[str | None] = []
            feed_page_lines: list[list[str]] = []
            feed_page_rendered: list[str] = []
            feed_event_index = 0
            last_rendered = ""
            last_emit_at = 0.0

            while True:
                try:
                    updated = False
                    stop_requested = False
                    while True:
                        try:
                            item = progress_queue.get_nowait()
                            updated = True
                            if item == "__stop__":
                                stop_requested = True
                        except queue.Empty:
                            break

                    now = time.monotonic()
                    heartbeat_due = (now - last_emit_at) >= 1.0
                    should_emit = updated or stop_requested or (progress_style == "single" and heartbeat_due)
                    if progress_state["updates"] <= 0 and not updated and not stop_requested:
                        await asyncio.sleep(0.25)
                        continue
                    if not should_emit:
                        await asyncio.sleep(0.25)
                        continue

                    msg = _build_progress_message()
                    if progress_style == "single":
                        result = None
                        if progress_msg_id is not None:
                            result = await adapter.edit_message(
                                chat_id=source.chat_id,
                                message_id=progress_msg_id,
                                content=msg,
                            )
                            if not result.success:
                                if _is_retryable_progress_edit_error(result.error):
                                    logger.warning(
                                        "Transient progress edit failure for %s message %s; preserving single-message mode: %s",
                                        source.platform,
                                        progress_msg_id,
                                        result.error,
                                    )
                                    last_emit_at = now
                                    await asyncio.sleep(0.25)
                                    continue
                                progress_msg_id = None
                        else:
                            result = await adapter.send(
                                chat_id=source.chat_id,
                                content=msg,
                                metadata=_progress_metadata,
                            )
                            if result.success and result.message_id:
                                progress_msg_id = result.message_id
                        if progress_msg_id is None and (result is None or not result.success):
                            result = await adapter.send(
                                chat_id=source.chat_id,
                                content=msg,
                                metadata=_progress_metadata,
                            )
                            if result.success and result.message_id:
                                progress_msg_id = result.message_id
                    elif source.platform == Platform.DISCORD:
                        result = None
                        all_events = list(progress_state.get("all_events") or [])

                        while feed_event_index < len(all_events):
                            line = _trim_progress_event_for_feed(all_events[feed_event_index])
                            if not feed_page_lines:
                                feed_page_lines.append([])
                                feed_page_ids.append(None)
                                feed_page_rendered.append("")
                            candidate_lines = [*feed_page_lines[-1], line]
                            candidate_msg = _build_progress_feed_page(candidate_lines)
                            if feed_page_lines[-1] and len(candidate_msg) > progress_embed_max_chars:
                                feed_page_rendered[-1] = _build_progress_feed_page(feed_page_lines[-1])
                                feed_page_lines.append([line])
                                feed_page_ids.append(None)
                                feed_page_rendered.append("")
                            else:
                                feed_page_lines[-1] = candidate_lines
                            feed_event_index += 1

                        if not feed_page_lines:
                            fallback = (progress_state.get("last_detail") or "Working...").strip()
                            feed_page_lines.append([f"`{datetime.now().strftime('%H:%M:%S')}` {fallback}"])
                            feed_page_ids.append(None)
                            feed_page_rendered.append("")

                        for idx, page_lines in enumerate(feed_page_lines):
                            is_live_page = idx == (len(feed_page_lines) - 1)
                            page_msg = (
                                _build_progress_feed_page(page_lines)
                                if is_live_page
                                else (feed_page_rendered[idx] or _build_progress_feed_page(page_lines))
                            )
                            page_id = feed_page_ids[idx]
                            if page_id is None:
                                result = await adapter.send(
                                    chat_id=source.chat_id,
                                    content=page_msg,
                                    metadata=_progress_metadata,
                                )
                                if result.success and result.message_id:
                                    feed_page_ids[idx] = result.message_id
                                    feed_page_rendered[idx] = page_msg
                                continue

                            if not is_live_page or feed_page_rendered[idx] == page_msg:
                                result = SendResult(success=True, message_id=page_id)
                                continue

                            result = await adapter.edit_message(
                                chat_id=source.chat_id,
                                message_id=page_id,
                                content=page_msg,
                            )
                            if not result.success:
                                if _is_retryable_progress_edit_error(result.error):
                                    logger.warning(
                                        "Transient paged-feed edit failure for %s message %s; preserving live page: %s",
                                        source.platform,
                                        page_id,
                                        result.error,
                                    )
                                    last_emit_at = now
                                    await asyncio.sleep(0.25)
                                    continue
                                feed_page_ids[idx] = None
                                result = await adapter.send(
                                    chat_id=source.chat_id,
                                    content=page_msg,
                                    metadata=_progress_metadata,
                                )
                                if result.success and result.message_id:
                                    feed_page_ids[idx] = result.message_id
                                    feed_page_rendered[idx] = page_msg
                                    continue
                            if result.success:
                                feed_page_rendered[idx] = page_msg
                    else:
                        result = await adapter.send(
                            chat_id=source.chat_id,
                            content=msg,
                            metadata=_progress_metadata,
                        )

                    if result is None or result.success:
                        last_rendered = msg
                    last_emit_at = now
                    await adapter.send_typing(source.chat_id, metadata=_progress_metadata)
                    if stop_requested or progress_state.get("done"):
                        with contextlib.suppress(Exception):
                            if hasattr(adapter, "stop_typing"):
                                await adapter.stop_typing(source.chat_id)
                        return
                    await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    if progress_msg_id and last_rendered:
                        try:
                            await adapter.edit_message(
                                chat_id=source.chat_id,
                                message_id=progress_msg_id,
                                content=last_rendered,
                            )
                        except Exception:
                            pass
                    with contextlib.suppress(Exception):
                        if hasattr(adapter, "stop_typing"):
                            await adapter.stop_typing(source.chat_id)
                    return
                except Exception as e:
                    logger.error("Progress message error: %s", e)
                    await asyncio.sleep(1)

        async def _finalize_progress(phase: str, detail: str) -> None:
            if not progress_task:
                return
            progress_state["done"] = True
            _set_progress_state(phase=phase, detail=detail, tool_call=False)
            if progress_queue:
                progress_queue.put("__stop__")
            try:
                await asyncio.wait_for(progress_task, timeout=2.0)
            except asyncio.TimeoutError:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task

        def _resolve_terminal_progress(result: dict | None, pending_message: str | None = None) -> tuple[str, str]:
            if not isinstance(result, dict):
                return "failed", "❌ Request failed."

            interrupted = bool(result.get("interrupted"))
            interrupt_reason = str(result.get("interrupt_reason") or "").strip().lower()
            final_text = str(result.get("final_response") or "").strip()
            error_text = str(result.get("error") or "").strip()

            if interrupted:
                if interrupt_reason == "timeout":
                    detail = final_text or error_text or "Request timed out."
                    if len(detail) > 220:
                        detail = detail[:217] + "..."
                    if not detail.startswith("⏱️"):
                        detail = f"⏱️ {detail}"
                    return "timed_out", detail
                if pending_message:
                    return "interrupted", "⚡ Interrupted by a newer message."
                detail = final_text or "Operation interrupted."
                if len(detail) > 220:
                    detail = detail[:217] + "..."
                if not detail.startswith("⚡"):
                    detail = f"⚡ {detail}"
                return "interrupted", detail

            if result.get("completed"):
                detail = final_text or "Reply ready."
                if len(detail) > 220:
                    detail = detail[:217] + "..."
                if not detail.startswith("✅"):
                    detail = f"✅ {detail}"
                return "completed", detail

            detail = error_text or final_text or "Request failed."
            if len(detail) > 220:
                detail = detail[:217] + "..."
            if not detail.startswith("❌"):
                detail = f"❌ {detail}"
            return "failed", detail
        
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

        # Bridge sync status_callback → async adapter.send for context pressure
        _status_adapter = self.adapters.get(source.platform)
        _status_chat_id = source.chat_id
        _status_thread_metadata = {"thread_id": _progress_thread_id} if _progress_thread_id else None

        def _status_callback_sync(event_type: str, message: str) -> None:
            if not _status_adapter:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    _status_adapter.send(
                        _status_chat_id,
                        message,
                        metadata=_status_thread_metadata,
                    ),
                    _loop_for_step,
                )
            except Exception as _e:
                logger.debug("status_callback error (%s): %s", event_type, _e)

        def run_sync():
            # Pass session_key to process registry via env var so background
            # processes can be mapped back to this gateway session
            os.environ["HERMES_SESSION_KEY"] = session_key or ""

            # Read from env var or use default (same as CLI)
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            if browser_sidecar_max_iterations is not None:
                max_iterations = browser_sidecar_max_iterations
            
            # Map platform enum to the platform hint key the agent understands.
            # Platform.LOCAL ("local") maps to "cli"; others pass through as-is.
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            
            # Combine platform context with user-configured ephemeral system prompt
            combined_ephemeral = context_prompt or ""
            if self._ephemeral_system_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + self._ephemeral_system_prompt).strip()

            # Re-read .env and config for fresh credentials (gateway is long-lived,
            # keys may change without restart).
            try:
                load_dotenv(_env_path, override=True, encoding="utf-8")
            except UnicodeDecodeError:
                load_dotenv(_env_path, override=True, encoding="latin-1")
            except Exception:
                pass

            model = _resolve_gateway_model(user_config)

            try:
                runtime_kwargs = _resolve_runtime_agent_kwargs()
            except Exception as exc:
                return {
                    "final_response": f"⚠️ Provider authentication failed: {exc}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }

            pr = self._provider_routing
            honcho_manager, honcho_config = self._get_or_create_gateway_honcho(session_key)
            reasoning_config = self._load_reasoning_config()
            self._reasoning_config = reasoning_config
            # Set up streaming consumer if enabled
            _stream_consumer = None
            _stream_delta_cb = None
            _scfg = getattr(getattr(self, 'config', None), 'streaming', None)
            if _scfg is None:
                from gateway.config import StreamingConfig
                _scfg = StreamingConfig()

            if _scfg.enabled and _scfg.transport != "off":
                try:
                    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
                    _adapter = self.adapters.get(source.platform)
                    if _adapter:
                        _consumer_cfg = StreamConsumerConfig(
                            edit_interval=_scfg.edit_interval,
                            buffer_threshold=_scfg.buffer_threshold,
                            cursor=_scfg.cursor,
                        )
                        _stream_consumer = GatewayStreamConsumer(
                            adapter=_adapter,
                            chat_id=source.chat_id,
                            config=_consumer_cfg,
                            metadata={"thread_id": _progress_thread_id} if _progress_thread_id else None,
                        )
                        _stream_delta_cb = _stream_consumer.on_delta
                        stream_consumer_holder[0] = _stream_consumer
                except Exception as _sc_err:
                    logger.debug("Could not set up stream consumer: %s", _sc_err)

            turn_route = self._resolve_turn_agent_config(message, model, runtime_kwargs)

            # Check agent cache — reuse the AIAgent from the previous message
            # in this session to preserve the frozen system prompt and tool
            # schemas for prompt cache hits.
            _sig = self._agent_config_signature(
                turn_route["model"],
                turn_route["runtime"],
                enabled_toolsets,
                combined_ephemeral,
            )
            agent = None
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached = _cache.get(session_key)
                    if cached and cached[1] == _sig:
                        agent = cached[0]
                        logger.debug("Reusing cached agent for session %s", session_key)

            if agent is None:
                # Config changed or first message — create fresh agent
                initial_tool_progress_callback = progress_callback if tool_progress_enabled else None
                initial_thinking_callback = progress_thinking_callback if tool_progress_enabled else None
                initial_step_callback = _step_callback_sync if _hooks_ref.loaded_hooks else None
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    ephemeral_system_prompt=combined_ephemeral or None,
                    prefill_messages=self._prefill_messages or None,
                    reasoning_config=reasoning_config,
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=session_id,
                    platform=platform_key,
                    honcho_session_key=session_key,
                    honcho_manager=honcho_manager,
                    honcho_config=honcho_config,
                    session_db=self._session_db,
                    fallback_model=self._fallback_model,
                    max_tool_calls_per_run=browser_sidecar_max_tool_calls,
                    tool_progress_callback=initial_tool_progress_callback,
                    thinking_callback=initial_thinking_callback,
                    step_callback=initial_step_callback,
                    status_callback=_status_callback_sync,
                    stream_delta_callback=_stream_delta_cb,
                    tool_gen_callback=progress_tool_gen_callback if tool_progress_enabled else None,
                )
                if _cache_lock and _cache is not None:
                    with _cache_lock:
                        _cache[session_key] = (agent, _sig)
                logger.debug("Created new agent for session %s (sig=%s)", session_key, _sig)

            # Per-message state — refresh callbacks and reasoning config every
            # turn so cached agents follow the current session context.
            agent.tool_progress_callback = progress_callback if tool_progress_enabled else None
            agent.thinking_callback = progress_thinking_callback if tool_progress_enabled else None
            agent.step_callback = _step_callback_sync if _hooks_ref.loaded_hooks else None
            agent.stream_delta_callback = _stream_delta_cb
            agent.status_callback = _status_callback_sync
            agent.reasoning_config = reasoning_config
            # Background review delivery — send "💾 Memory updated" etc. to user
            def _bg_review_send(message: str) -> None:
                if not _status_adapter:
                    return
                try:
                    asyncio.run_coroutine_threadsafe(
                        _status_adapter.send(
                            _status_chat_id,
                            message,
                            metadata=_status_thread_metadata,
                        ),
                        _loop_for_step,
                    )
                except Exception as _e:
                    logger.debug("background_review_callback error: %s", _e)

            agent.background_review_callback = _bg_review_send
            agent.tool_gen_callback = progress_tool_gen_callback if tool_progress_enabled else None
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
                        entry = {"role": role, "content": content}
                        # Preserve reasoning fields on assistant messages so
                        # multi-turn reasoning context survives session reload.
                        # The agent's _build_api_kwargs converts these to the
                        # provider-specific format (reasoning_content, etc.).
                        if role == "assistant":
                            for _rkey in ("reasoning", "reasoning_details",
                                          "codex_reasoning_items"):
                                _rval = msg.get(_rkey)
                                if _rval:
                                    entry[_rkey] = _rval
                        agent_history.append(entry)
            
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
            
            tool_task_id = session_id
            if self._is_browser_bridge_source(source):
                # Sidecar turns use a stable prefix so tools can apply sidecar-only
                # execution policy (for example forcing local/CDP browser mode).
                tool_task_id = f"sidecar_{session_id}"

            result = agent.run_conversation(
                message,
                conversation_history=agent_history,
                task_id=tool_task_id,
            )
            result_holder[0] = result
            
            # Return final response, or a message if something went wrong
            final_response = result.get("final_response")

            # Extract actual token counts from the agent instance used for this run
            _last_prompt_toks = 0
            _input_toks = 0
            _output_toks = 0
            _agent = agent_holder[0]
            if _agent and hasattr(_agent, "context_compressor"):
                _last_prompt_toks = getattr(_agent.context_compressor, "last_prompt_tokens", 0)
                _input_toks = getattr(_agent, "session_prompt_tokens", 0)
                _output_toks = getattr(_agent, "session_completion_tokens", 0)
            _resolved_model = getattr(_agent, "model", None) if _agent else None

            if not final_response:
                error_msg = f"⚠️ {result['error']}" if result.get("error") else "(No response generated)"
                return {
                    "final_response": error_msg,
                    "messages": result.get("messages", []),
                    "api_calls": result.get("api_calls", 0),
                    "tools": tools_holder[0] or [],
                    "history_offset": len(agent_history),
                    "last_prompt_tokens": _last_prompt_toks,
                    "input_tokens": _input_toks,
                    "output_tokens": _output_toks,
                    "model": _resolved_model,
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
                    if has_voice_directive:
                        unique_tags.insert(0, "[[audio_as_voice]]")
                    final_response = final_response + "\n" + "\n".join(unique_tags)
            
            # Sync session_id: the agent may have created a new session during
            # mid-run context compression (_compress_context splits sessions).
            # If so, update the session store entry so the NEXT message loads
            # the compressed transcript, not the stale pre-compression one.
            agent = agent_holder[0]
            if agent and session_key and hasattr(agent, 'session_id') and agent.session_id != session_id:
                logger.info(
                    "Session split detected: %s → %s (compression)",
                    session_id, agent.session_id,
                )
                entry = self.session_store._entries.get(session_key)
                if entry:
                    entry.session_id = agent.session_id
                    self.session_store._save()

            effective_session_id = getattr(agent, 'session_id', session_id) if agent else session_id

            return {
                "final_response": final_response,
                "last_reasoning": result.get("last_reasoning"),
                "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                "api_calls": result_holder[0].get("api_calls", 0) if result_holder[0] else 0,
                "tools": tools_holder[0] or [],
                "history_offset": len(agent_history),
                "last_prompt_tokens": _last_prompt_toks,
                "input_tokens": _input_toks,
                "output_tokens": _output_toks,
                "model": _resolved_model,
                "session_id": effective_session_id,
            }
        
        # Start progress message sender if enabled
        progress_task = None
        if tool_progress_enabled:
            _set_progress_state(
                phase="starting",
                detail="🚀 starting request: handing message to Hermes...",
            )
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
            if not adapter or not session_key:
                return
            
            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                # Check if adapter has a pending interrupt for this session.
                # Must use session_key (build_session_key output) — NOT
                # source.chat_id — because the adapter stores interrupt events
                # under the full session key.
                if hasattr(adapter, 'has_pending_interrupt') and adapter.has_pending_interrupt(session_key):
                    agent = agent_holder[0]
                    if agent:
                        pending_event = adapter.get_pending_message(session_key)
                        pending_text = pending_event.text if pending_event else None
                        logger.debug("Interrupt detected from adapter, signaling agent...")
                        agent.interrupt(pending_text)
                        break
        
        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())
        response = None
        pending = None
        
        try:
            # Run in thread pool to not block
            loop = asyncio.get_event_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-agent") as pool:
                response = await loop.run_in_executor(pool, run_sync)

            # Track fallback model state: if the agent switched to a
            # fallback model during this run, persist it so /model shows
            # the actually-active model instead of the config default.
            _agent = agent_holder[0]
            if _agent is not None and hasattr(_agent, 'model'):
                _cfg_model = _resolve_gateway_model()
                if _agent.model != _cfg_model:
                    self._effective_model = _agent.model
                    self._effective_provider = getattr(_agent, 'provider', None)
                    # Fallback activated — evict cached agent so the next
                    # message starts fresh and retries the primary model.
                    self._evict_cached_agent(session_key)
                else:
                    # Primary model worked — clear any stale fallback state
                    self._effective_model = None
                    self._effective_provider = None

            # Check if we were interrupted OR have a queued message (/queue).
            result = result_holder[0]
            adapter = self.adapters.get(source.platform)
            
            # Get pending message from adapter.
            # Use session_key (not source.chat_id) to match adapter's storage keys.
            if result and adapter and session_key:
                if result.get("interrupted"):
                    # Interrupted — consume the interrupt message
                    pending_event = adapter.get_pending_message(session_key)
                    if pending_event:
                        pending = pending_event.text
                    elif result.get("interrupt_message"):
                        pending = result.get("interrupt_message")
                else:
                    # Normal completion — check for /queue'd messages that were
                    # stored without triggering an interrupt.
                    pending_event = adapter.get_pending_message(session_key)
                    if pending_event:
                        pending = pending_event.text
                        logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])
            
            if pending:
                logger.debug("Processing pending message: '%s...'", pending[:40])
                
                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and session_key and session_key in adapter._active_sessions:
                    adapter._active_sessions[session_key].clear()
                
                # Cap recursion depth to prevent resource exhaustion when the
                # user sends multiple messages while the agent keeps failing. (#816)
                if interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
                    logger.warning(
                        "Interrupt recursion depth %d reached for session %s — "
                        "queueing message instead of recursing.",
                        interrupt_depth, session_key,
                    )
                    # Queue the pending message for normal processing on next turn
                    adapter = self.adapters.get(source.platform)
                    if adapter and hasattr(adapter, 'queue_message'):
                        adapter.queue_message(session_key, pending)
                    return result_holder[0] or {"final_response": response, "messages": history}

                was_interrupted = result.get("interrupted")
                if not was_interrupted:
                    # Queued message after normal completion — deliver the first
                    # response before processing the queued follow-up.
                    # Skip if streaming already delivered it.
                    _sc = stream_consumer_holder[0]
                    _already_streamed = _sc and getattr(_sc, "already_sent", False)
                    first_response = result.get("final_response", "")
                    if first_response and not _already_streamed:
                        try:
                            await adapter.send(source.chat_id, first_response,
                                               metadata=getattr(event, "metadata", None))
                        except Exception as e:
                            logger.warning("Failed to send first response before queued message: %s", e)
                # else: interrupted — discard the interrupted response ("Operation
                # interrupted." is just noise; the user already knows they sent a
                # new message).

                # Process the pending message with updated history
                updated_history = result.get("messages", history)
                return await self._run_agent(
                    message=pending,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=source,
                    session_id=session_id,
                    session_key=session_key,
                    interrupt_depth=interrupt_depth + 1,
                )
        finally:
            if progress_task:
                active_exc = sys.exc_info()[1]
                if active_exc is not None:
                    error_text = str(active_exc).strip() or type(active_exc).__name__
                    if len(error_text) > 220:
                        error_text = error_text[:217] + "..."
                    await _finalize_progress("failed", f"❌ {error_text}")
                else:
                    phase, detail = _resolve_terminal_progress(result_holder[0], pending)
                    await _finalize_progress(phase, detail)

            interrupt_monitor.cancel()
            
            # Clean up tracking
            tracking_task.cancel()
            if session_key and session_key in self._running_agents:
                del self._running_agents[session_key]
            
            # Wait for cancelled tasks
            for task in [interrupt_monitor, tracking_task]:
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


async def start_gateway(config: Optional[GatewayConfig] = None, replace: bool = False) -> bool:
    """
    Start the gateway and run until interrupted.
    
    This is the main entry point for running the gateway.
    Returns True if the gateway ran successfully, False if it failed to start.
    A False return causes a non-zero exit code so systemd can auto-restart.
    
    Args:
        config: Optional gateway configuration override.
        replace: If True, kill any existing gateway instance before starting.
                 Useful for systemd services to avoid restart-loop deadlocks
                 when the previous process hasn't fully exited yet.
    """
    _harden_windows_console_logging()

    # ── Duplicate-instance guard ──────────────────────────────────────
    # Prevent two gateways from running under the same HERMES_HOME.
    # The PID file is scoped to HERMES_HOME, so future multi-profile
    # setups (each profile using a distinct HERMES_HOME) will naturally
    # allow concurrent instances without tripping this guard.
    import time as _time
    from gateway.status import get_running_pid, remove_pid_file
    existing_pid = get_running_pid()
    if existing_pid is not None and existing_pid != os.getpid():
        if replace:
            logger.info(
                "Replacing existing gateway instance (PID %d) with --replace.",
                existing_pid,
            )
            try:
                os.kill(existing_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Already gone
            except PermissionError:
                logger.error(
                    "Permission denied killing PID %d. Cannot replace.",
                    existing_pid,
                )
                return False
            # Wait up to 10 seconds for the old process to exit
            for _ in range(20):
                try:
                    os.kill(existing_pid, 0)
                    _time.sleep(0.5)
                except (ProcessLookupError, PermissionError):
                    break  # Process is gone
            else:
                # Still alive after 10s — force kill
                logger.warning(
                    "Old gateway (PID %d) did not exit after SIGTERM, sending SIGKILL.",
                    existing_pid,
                )
                try:
                    os.kill(existing_pid, signal.SIGKILL)
                    _time.sleep(0.5)
                except (ProcessLookupError, PermissionError):
                    pass
            remove_pid_file()
            # Also release all scoped locks left by the old process.
            # Stopped (Ctrl+Z) processes don't release locks on exit,
            # leaving stale lock files that block the new gateway from starting.
            try:
                from gateway.status import release_all_scoped_locks
                _released = release_all_scoped_locks()
                if _released:
                    logger.info("Released %d stale scoped lock(s) from old gateway.", _released)
            except Exception:
                pass
        else:
            hermes_home = str(get_hermes_home())
            logger.error(
                "Another gateway instance is already running (PID %d, HERMES_HOME=%s). "
                "Use 'hermes gateway restart' to replace it, or 'hermes gateway stop' first.",
                existing_pid, hermes_home,
            )
            print(
                f"\nGateway already running (PID {existing_pid}).\n"
                f"   Use 'hermes gateway restart' to replace it,\n"
                f"   or 'hermes gateway stop' to kill it first.\n"
                f"   Or use 'hermes gateway run --replace' to auto-replace.\n"
            )
            return False

    # Sync bundled skills on gateway start (fast -- skips unchanged)
    try:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)
    except Exception:
        pass

    # Configure rotating file log so gateway output is persisted for debugging
    log_dir = _hermes_home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / 'gateway.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        errors="replace",
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
        encoding="utf-8",
        errors="replace",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(error_handler)

    runner = GatewayRunner(config)
    detached_mode = os.getenv("HERMES_GATEWAY_DETACHED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    previous_sigint_handler = None

    # Detached gateway windows should not be torn down by stray SIGINT delivery.
    # This process is meant to be controlled via `hermes gateway stop/restart`.
    if detached_mode:
        try:
            previous_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            logger.info("Detached gateway mode: ignoring SIGINT to prevent accidental shutdown.")
        except Exception as e:
            logger.debug("Could not install detached SIGINT ignore handler: %s", e)
    
    # Set up signal handlers
    def signal_handler():
        asyncio.create_task(runner.stop())
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass
    
    # Start the gateway
    success = await runner.start()
    if not success:
        return False
    if runner.should_exit_cleanly:
        if runner.exit_reason:
            logger.error("Gateway exiting cleanly: %s", runner.exit_reason)
        return True
    
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
    try:
        while True:
            try:
                await runner.wait_for_shutdown()
                break
            except asyncio.CancelledError:
                running = bool(getattr(runner, "_running", False))
                shutdown_requested = bool(getattr(runner, "_shutdown_event", None) and runner._shutdown_event.is_set())
                active_sidecar_turns = any(
                    not task.done()
                    for task in getattr(runner, "_browser_bridge_tasks", {}).values()
                )
                recoverable_cancel = running and not shutdown_requested and (
                    detached_mode or active_sidecar_turns
                )
                if recoverable_cancel:
                    detail = "detached mode" if detached_mode else "active browser sidecar turn"
                    logger.error(
                        "Unexpected cancellation while gateway is running (%s); "
                        "keeping gateway alive and resuming wait.",
                        detail,
                    )
                    current = asyncio.current_task()
                    if current and hasattr(current, "uncancel"):
                        try:
                            current.uncancel()
                        except Exception:
                            pass
                    await asyncio.sleep(0)
                    continue

                logger.info("Gateway shutdown wait was cancelled; stopping gracefully.")
                if running:
                    try:
                        await runner.stop()
                    except Exception as e:
                        logger.debug("Graceful stop after cancellation failed: %s", e)
                break
    finally:
        if previous_sigint_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint_handler)
            except Exception:
                pass

        # Stop cron ticker cleanly
        cron_stop.set()
        cron_thread.join(timeout=5)

        # Close MCP server connections
        try:
            from tools.mcp_tool import shutdown_mcp_servers
            shutdown_mcp_servers()
        except Exception:
            pass

        # Ensure runtime status and PID are not left stale after abnormal exits.
        try:
            from gateway.status import remove_pid_file, write_runtime_status
            remove_pid_file()
            write_runtime_status(gateway_state="stopped", exit_reason=getattr(runner, "_exit_reason", None))
        except Exception:
            pass

    if getattr(runner, "should_exit_with_failure", False):
        if runner.exit_reason:
            logger.error("Gateway exiting with failure: %s", runner.exit_reason)
        return False

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
