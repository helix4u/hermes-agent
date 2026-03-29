"""CLI commands for Honcho integration management.

Handles: hermes honcho setup | status | sessions | map | peer
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from honcho_integration.client import resolve_config_path, GLOBAL_CONFIG_PATH

HOST = "hermes"
HONCHO_SDK_SPEC = "honcho-ai>=2.0.1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_HONCHO_DIR = PROJECT_ROOT / "docker" / "honcho"
LOCAL_HONCHO_COMPOSE = LOCAL_HONCHO_DIR / "compose.yaml"
LOCAL_HONCHO_ENV_EXAMPLE = LOCAL_HONCHO_DIR / ".env.example"
LOCAL_HONCHO_ENV = LOCAL_HONCHO_DIR / ".env"
LOCAL_HONCHO_CONFIG_TEMPLATE = LOCAL_HONCHO_DIR / "config.toml.local.example"
DEFAULT_LOCAL_HONCHO_GIT_URL = "https://github.com/plastic-labs/honcho.git"
DEFAULT_LOCAL_HONCHO_PORT = 8420
DEFAULT_LOCAL_HONCHO_WAIT_SECS = 60
WINDOWS_DOCKER_EXE = Path("/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe")
WINDOWS_STAGE_DIR_NAME = "hermes-honcho-local"


def _config_path() -> Path:
    """Return the active Honcho config path (instance-local or global)."""
    return resolve_config_path()


def _read_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_config(cfg: dict, path: Path | None = None) -> None:
    path = path or _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _resolve_api_key(cfg: dict) -> str:
    """Resolve API key with host -> root -> env fallback."""
    host_key = ((cfg.get("hosts") or {}).get(HOST) or {}).get("apiKey")
    return host_key or cfg.get("apiKey", "") or os.environ.get("HONCHO_API_KEY", "")


def _local_honcho_base_url(port: int) -> str:
    return f"http://localhost:{port}"


def _resolve_local_source_dir(source_dir: str | None = None) -> Path:
    if not source_dir:
        return LOCAL_HONCHO_DIR / "honcho-src"
    candidate = Path(source_dir).expanduser()
    if not candidate.is_absolute():
        candidate = (LOCAL_HONCHO_DIR / candidate).resolve()
    return candidate


def _format_source_dir_for_env(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(LOCAL_HONCHO_DIR.resolve())
    except ValueError:
        return resolved.as_posix()
    rel_text = relative.as_posix()
    return f"./{rel_text}" if not rel_text.startswith(".") else rel_text


def _upsert_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            new_lines.append(f"{prefix}{value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{prefix}{value}")
    path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def _remove_env_value(path: Path, key: str) -> None:
    if not path.exists():
        return
    prefix = f"{key}="
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(prefix)
    ]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _ensure_local_compose_env(
    port: int | None = None,
    source_dir: str | None = None,
) -> tuple[Path, Path, str]:
    """Ensure docker/honcho/.env exists and points at the requested source + port."""
    if not LOCAL_HONCHO_COMPOSE.exists():
        raise FileNotFoundError(f"Missing local Honcho compose file: {LOCAL_HONCHO_COMPOSE}")
    if not LOCAL_HONCHO_ENV.exists():
        if not LOCAL_HONCHO_ENV_EXAMPLE.exists():
            raise FileNotFoundError(f"Missing local Honcho env example: {LOCAL_HONCHO_ENV_EXAMPLE}")
        shutil.copyfile(LOCAL_HONCHO_ENV_EXAMPLE, LOCAL_HONCHO_ENV)
    env_values = _read_env_values(LOCAL_HONCHO_ENV)
    resolved_source_dir = source_dir or env_values.get("HONCHO_SOURCE_DIR")
    resolved_port = port
    if resolved_port is None:
        try:
            resolved_port = int(env_values.get("HONCHO_HTTP_PORT", DEFAULT_LOCAL_HONCHO_PORT))
        except (TypeError, ValueError):
            resolved_port = DEFAULT_LOCAL_HONCHO_PORT
    source_path = _resolve_local_source_dir(resolved_source_dir)
    _upsert_env_value(LOCAL_HONCHO_ENV, "HONCHO_SOURCE_DIR", _format_source_dir_for_env(source_path))
    _upsert_env_value(LOCAL_HONCHO_ENV, "HONCHO_HTTP_PORT", str(resolved_port))
    return source_path, LOCAL_HONCHO_ENV, _local_honcho_base_url(resolved_port)


def _clone_local_honcho_source(destination: Path, git_url: str = DEFAULT_LOCAL_HONCHO_GIT_URL) -> None:
    if destination.exists():
        return
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("git is required to clone the local Honcho server source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [git_bin, "clone", "--depth", "1", git_url, str(destination)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Unknown git clone error").strip()
        raise RuntimeError(f"Failed to clone Honcho source: {details}")


def _ensure_local_honcho_server_config(source_path: Path) -> Path | None:
    target = source_path / "config.toml"
    if target.exists() or not LOCAL_HONCHO_CONFIG_TEMPLATE.exists():
        return target if target.exists() else None
    shutil.copyfile(LOCAL_HONCHO_CONFIG_TEMPLATE, target)
    return target


def _first_nonempty(*values: object) -> str:
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        elif value:
            return str(value)
    return ""


def _normalize_provider_name(provider: str | None) -> str:
    normalized = (provider or "").strip().lower()
    aliases = {
        "codex": "openai-codex",
    }
    return aliases.get(normalized, normalized)


def _resolve_local_honcho_model_defaults() -> tuple[str, str]:
    from hermes_cli.config import load_config

    config = load_config()
    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    compression_cfg = (
        config.get("compression") if isinstance(config.get("compression"), dict) else {}
    )
    delegation_cfg = (
        config.get("delegation") if isinstance(config.get("delegation"), dict) else {}
    )
    auxiliary_cfg = config.get("auxiliary") if isinstance(config.get("auxiliary"), dict) else {}
    vision_cfg = auxiliary_cfg.get("vision") if isinstance(auxiliary_cfg.get("vision"), dict) else {}

    provider = _normalize_provider_name(
        _first_nonempty(
            vision_cfg.get("provider"),
            compression_cfg.get("summary_provider"),
            delegation_cfg.get("provider"),
            model_cfg.get("provider"),
        )
    )
    model = _first_nonempty(
        vision_cfg.get("model"),
        compression_cfg.get("summary_model"),
        delegation_cfg.get("model"),
        model_cfg.get("default"),
    )
    return provider or "nous", model or "google/gemini-3-flash-preview"


def _resolve_local_honcho_model_context() -> dict[str, str]:
    from hermes_cli.config import load_config

    config = load_config()
    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    compression_cfg = (
        config.get("compression") if isinstance(config.get("compression"), dict) else {}
    )
    delegation_cfg = (
        config.get("delegation") if isinstance(config.get("delegation"), dict) else {}
    )
    auxiliary_cfg = config.get("auxiliary") if isinstance(config.get("auxiliary"), dict) else {}
    vision_cfg = auxiliary_cfg.get("vision") if isinstance(auxiliary_cfg.get("vision"), dict) else {}

    main_provider = _normalize_provider_name(_first_nonempty(model_cfg.get("provider")))
    main_model = _first_nonempty(model_cfg.get("default"))

    source_candidates = [
        (
            "auxiliary.vision",
            _normalize_provider_name(_first_nonempty(vision_cfg.get("provider"))),
            _first_nonempty(vision_cfg.get("model")),
        ),
        (
            "compression.summary",
            _normalize_provider_name(_first_nonempty(compression_cfg.get("summary_provider"))),
            _first_nonempty(compression_cfg.get("summary_model")),
        ),
        (
            "delegation",
            _normalize_provider_name(_first_nonempty(delegation_cfg.get("provider"))),
            _first_nonempty(delegation_cfg.get("model")),
        ),
        ("model.default", main_provider, main_model),
    ]

    sync_label = "model.default"
    sync_provider = ""
    sync_model = ""
    for label, provider, model in source_candidates:
        if provider or model:
            sync_label = label
            sync_provider = provider
            sync_model = model
            break

    sync_provider = _normalize_provider_name(_first_nonempty(sync_provider, main_provider, "nous"))
    sync_model = _first_nonempty(
        sync_model,
        _first_nonempty(compression_cfg.get("summary_model")),
        main_model,
        "google/gemini-3-flash-preview",
    )

    return {
        "main_provider": main_provider or "nous",
        "main_model": main_model or "openai/gpt-5.4-mini",
        "sync_label": sync_label,
        "sync_provider": sync_provider,
        "sync_model": sync_model,
    }


def _resolve_local_honcho_runtime_profile() -> dict[str, object]:
    model_context = _resolve_local_honcho_model_context()
    source_provider = model_context["sync_provider"]
    source_model = model_context["sync_model"]

    remove_env = [
        "LLM_ANTHROPIC_API_KEY",
        "LLM_GEMINI_API_KEY",
        "LLM_GROQ_API_KEY",
        "LLM_OPENAI_API_KEY",
        "LLM_OPENAI_COMPATIBLE_API_KEY",
        "LLM_OPENAI_COMPATIBLE_BASE_URL",
        "LLM_VLLM_API_KEY",
        "LLM_VLLM_BASE_URL",
    ]

    if source_provider == "nous":
        from hermes_cli.auth import resolve_nous_runtime_credentials

        creds = resolve_nous_runtime_credentials(min_key_ttl_seconds=5 * 60)
        api_key = (creds.get("api_key") or "").strip()
        base_url = (creds.get("base_url") or "").strip()
        if not api_key or not base_url:
            raise RuntimeError("Nous credentials are not available for local Honcho sync")
        return {
            "main_provider": model_context["main_provider"],
            "main_model": model_context["main_model"],
            "sync_label": model_context["sync_label"],
            "source_provider": source_provider,
            "source_model": source_model,
            "honcho_provider": "custom",
            "honcho_model": source_model,
            "embedding_provider": "openai",
            "env_updates": {
                "LLM_OPENAI_COMPATIBLE_API_KEY": api_key,
                "LLM_OPENAI_COMPATIBLE_BASE_URL": base_url,
            },
            "remove_env": [key for key in remove_env if key not in {
                "LLM_OPENAI_COMPATIBLE_API_KEY",
                "LLM_OPENAI_COMPATIBLE_BASE_URL",
            }],
        }

    if source_provider == "google":
        api_key = (
            os.environ.get("LLM_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or ""
        ).strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is required to use a Google model in local Honcho"
            )
        return {
            "main_provider": model_context["main_provider"],
            "main_model": model_context["main_model"],
            "sync_label": model_context["sync_label"],
            "source_provider": source_provider,
            "source_model": source_model,
            "honcho_provider": "google",
            "honcho_model": source_model,
            "embedding_provider": "gemini",
            "env_updates": {
                "LLM_GEMINI_API_KEY": api_key,
            },
            "remove_env": [key for key in remove_env if key != "LLM_GEMINI_API_KEY"],
        }

    raise RuntimeError(
        "Local Honcho model sync currently supports Hermes providers "
        "'nous' and 'google'. Update Hermes auxiliary.vision or compression "
        "settings, or edit docker/honcho/honcho-src/config.toml manually."
    )


def _render_local_honcho_server_config(profile: dict[str, object]) -> str:
    provider = str(profile["honcho_provider"])
    model = json.dumps(str(profile["honcho_model"]))
    embedding_provider = str(profile["embedding_provider"])
    source_provider = str(profile["source_provider"])
    source_model = str(profile["source_model"])
    return f"""# Managed by Hermes local Honcho setup.
#
# This stack keeps Honcho local (Docker API + DB + Redis) while syncing its LLM
# routing to Hermes' configured provider/model defaults. For Hermes' `nous`
# provider, Honcho uses its OpenAI-compatible `custom` client under the hood.
#
# Hermes sync source: {profile.get("sync_label", "unknown")}
# Hermes source provider: {source_provider}
# Hermes source model: {source_model}

[app]
EMBED_MESSAGES = false

[llm]
# Embeddings stay disabled in the default local stack. This setting is kept
# explicit so Honcho never drifts back to an OpenRouter default.
EMBEDDING_PROVIDER = "{embedding_provider}"

[deriver]
ENABLED = false
PROVIDER = "{provider}"
MODEL = {model}

[summary]
ENABLED = false
PROVIDER = "{provider}"
MODEL = {model}

[dream]
ENABLED = false
PROVIDER = "{provider}"
MODEL = {model}

[dialectic.levels.minimal]
PROVIDER = "{provider}"
MODEL = {model}
THINKING_BUDGET_TOKENS = 0
MAX_TOOL_ITERATIONS = 1

[dialectic.levels.low]
PROVIDER = "{provider}"
MODEL = {model}
THINKING_BUDGET_TOKENS = 0
MAX_TOOL_ITERATIONS = 5

[dialectic.levels.medium]
PROVIDER = "{provider}"
MODEL = {model}
THINKING_BUDGET_TOKENS = 1024
MAX_TOOL_ITERATIONS = 2

[dialectic.levels.high]
PROVIDER = "{provider}"
MODEL = {model}
THINKING_BUDGET_TOKENS = 1024
MAX_TOOL_ITERATIONS = 4

[dialectic.levels.max]
PROVIDER = "{provider}"
MODEL = {model}
THINKING_BUDGET_TOKENS = 2048
MAX_TOOL_ITERATIONS = 10
"""


def _write_local_honcho_server_config(source_path: Path, env_path: Path) -> tuple[Path, dict[str, object]]:
    profile = _resolve_local_honcho_runtime_profile()
    env_updates = profile.get("env_updates") or {}
    for key in profile.get("remove_env") or []:
        _remove_env_value(env_path, str(key))
    for key, value in env_updates.items():
        _upsert_env_value(env_path, str(key), str(value))

    target = source_path / "config.toml"
    target.write_text(
        _render_local_honcho_server_config(profile),
        encoding="utf-8",
    )
    return target, profile


def _nested_get(mapping: dict[str, object], *keys: str) -> object:
    current: object = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_local_honcho_server_snapshot(source_path: Path, env_path: Path) -> dict[str, object]:
    target = source_path / "config.toml"
    raw: dict[str, object] = {}
    if target.exists():
        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except Exception:
            raw = {}

    env_values = _read_env_values(env_path)
    sections = {
        "deriver": ("deriver",),
        "summary": ("summary",),
        "dream": ("dream",),
        "dialectic.minimal": ("dialectic", "levels", "minimal"),
        "dialectic.low": ("dialectic", "levels", "low"),
        "dialectic.medium": ("dialectic", "levels", "medium"),
        "dialectic.high": ("dialectic", "levels", "high"),
        "dialectic.max": ("dialectic", "levels", "max"),
    }
    routes: dict[str, dict[str, str]] = {}
    for name, path in sections.items():
        provider = _first_nonempty(_nested_get(raw, *path, "PROVIDER"))
        model = _first_nonempty(_nested_get(raw, *path, "MODEL"))
        routes[name] = {"provider": provider, "model": model}

    return {
        "config_path": str(target),
        "exists": target.exists(),
        "embedding_provider": _first_nonempty(_nested_get(raw, "llm", "EMBEDDING_PROVIDER")),
        "routes": routes,
        "env_bridge": {
            "LLM_OPENAI_COMPATIBLE_BASE_URL": _first_nonempty(
                env_values.get("LLM_OPENAI_COMPATIBLE_BASE_URL")
            ),
            "LLM_OPENAI_COMPATIBLE_API_KEY": bool(
                _first_nonempty(env_values.get("LLM_OPENAI_COMPATIBLE_API_KEY"))
            ),
            "LLM_GEMINI_API_KEY": bool(_first_nonempty(env_values.get("LLM_GEMINI_API_KEY"))),
        },
    }


def _collect_local_honcho_model_audit(
    *,
    port: int | None = None,
    source_dir: str | None = None,
) -> dict[str, object]:
    source_path, env_path, base_url = _ensure_local_compose_env(port=port, source_dir=source_dir)
    desired_profile = _resolve_local_honcho_runtime_profile()
    snapshot = _read_local_honcho_server_snapshot(source_path, env_path)

    desired_provider = str(desired_profile["honcho_provider"])
    desired_model = str(desired_profile["honcho_model"])
    desired_embedding = str(desired_profile["embedding_provider"])
    routes = snapshot["routes"] if isinstance(snapshot.get("routes"), dict) else {}

    mismatches: list[str] = []
    for name, route in routes.items():
        if not isinstance(route, dict):
            mismatches.append(f"{name}: missing")
            continue
        current_provider = _first_nonempty(route.get("provider"))
        current_model = _first_nonempty(route.get("model"))
        if current_provider != desired_provider or current_model != desired_model:
            mismatches.append(
                f"{name}: expected {desired_provider}:{desired_model}, "
                f"found {current_provider or '(unset)'}:{current_model or '(unset)'}"
            )
    current_embedding = _first_nonempty(snapshot.get("embedding_provider"))
    if current_embedding != desired_embedding:
        mismatches.append(
            f"llm.EMBEDDING_PROVIDER: expected {desired_embedding}, "
            f"found {current_embedding or '(unset)'}"
        )

    return {
        "base_url": base_url,
        "source_path": str(source_path),
        "env_path": str(env_path),
        "desired": {
            "main_provider": desired_profile.get("main_provider", ""),
            "main_model": desired_profile.get("main_model", ""),
            "sync_label": desired_profile.get("sync_label", "unknown"),
            "source_provider": desired_profile["source_provider"],
            "source_model": desired_profile["source_model"],
            "honcho_provider": desired_provider,
            "honcho_model": desired_model,
            "embedding_provider": desired_embedding,
        },
        "current": snapshot,
        "in_sync": not mismatches,
        "mismatches": mismatches,
    }


def cmd_local_model_audit(args) -> None:
    """Show the expected and current Honcho model/provider routing."""
    port = getattr(args, "port", None)
    source_dir_arg = getattr(args, "source_dir", None)
    as_json = bool(getattr(args, "json", False))

    try:
        audit = _collect_local_honcho_model_audit(port=port, source_dir=source_dir_arg)
    except Exception as exc:
        print(f"  Local Honcho model audit failed: {exc}\n")
        return

    if as_json:
        print(json.dumps(audit, indent=2, ensure_ascii=False))
        print()
        return

    desired = audit["desired"]
    current = audit["current"]
    env_bridge = current.get("env_bridge") if isinstance(current, dict) else {}
    print("\nLocal Honcho model audit\n" + "─" * 40)
    print(f"  Base URL:      {audit['base_url']}")
    print(f"  Source dir:    {audit['source_path']}")
    print(f"  Env file:      {audit['env_path']}")
    print(
        "  Main Hermes:   "
        f"{desired['main_provider']}:{desired['main_model']}"
    )
    print(
        "  Honcho sync:   "
        f"{desired['sync_label']} -> {desired['source_provider']}:{desired['source_model']}"
    )
    print(
        "  Honcho route:  "
        f"{desired['honcho_provider']}:{desired['honcho_model']}"
    )
    print(f"  Embeddings:    {desired['embedding_provider']}")
    if isinstance(env_bridge, dict):
        bridge_base = _first_nonempty(env_bridge.get("LLM_OPENAI_COMPATIBLE_BASE_URL"))
        if bridge_base:
            print(f"  Transport:     OpenAI-compatible via {bridge_base}")
        print(
            "  Credentials:   "
            f"openai-compatible={'yes' if env_bridge.get('LLM_OPENAI_COMPATIBLE_API_KEY') else 'no'}, "
            f"gemini={'yes' if env_bridge.get('LLM_GEMINI_API_KEY') else 'no'}"
        )

    routes = current.get("routes") if isinstance(current, dict) else {}
    if isinstance(routes, dict):
        print("\n  Current config:")
        for name, route in routes.items():
            if not isinstance(route, dict):
                continue
            print(
                f"    {name:<18}"
                f"{_first_nonempty(route.get('provider')) or '(unset)'}:"
                f"{_first_nonempty(route.get('model')) or '(unset)'}"
            )

    if audit["in_sync"]:
        print("\n  Status: in sync\n")
    else:
        print("\n  Status: drift detected")
        for item in audit["mismatches"]:
            print(f"    - {item}")
        print("\n  Run: hermes honcho local model-apply [--restart]\n")


def cmd_local_model_apply(args) -> None:
    """Rewrite the local Honcho model/provider config from Hermes defaults."""
    port = getattr(args, "port", None)
    source_dir_arg = getattr(args, "source_dir", None)
    restart = bool(getattr(args, "restart", False))

    try:
        source_path, env_path, base_url = _ensure_local_compose_env(port=port, source_dir=source_dir_arg)
        if not source_path.exists():
            print(f"  Honcho source not found at {source_path}")
            print("  Run 'hermes honcho local setup' first or point --source-dir at an existing checkout.\n")
            return
        config_path, profile = _write_local_honcho_server_config(source_path, env_path)
    except Exception as exc:
        print(f"  Failed to apply local Honcho model config: {exc}\n")
        return

    print("\nLocal Honcho model apply\n" + "─" * 40)
    print(f"  Config path:   {config_path}")
    print(f"  Base URL:      {base_url}")
    print(
        "  Main Hermes:   "
        f"{profile.get('main_provider', '')}:{profile.get('main_model', '')}"
    )
    print(
        "  Honcho sync:   "
        f"{profile.get('sync_label', 'unknown')} -> "
        f"{profile['source_provider']}:{profile['source_model']}"
    )
    print(
        "  Honcho route:  "
        f"{profile['honcho_provider']}:{profile['honcho_model']}"
    )

    if restart:
        print("\n  Restarting local Honcho stack...", flush=True)
        stop_args = type("Args", (), {"volumes": False})()
        cmd_local_stop(stop_args)
        start_args = type(
            "Args",
            (),
            {
                "port": port,
                "source_dir": source_dir_arg,
                "no_clone": True,
                "git_url": DEFAULT_LOCAL_HONCHO_GIT_URL,
                "wait": DEFAULT_LOCAL_HONCHO_WAIT_SECS,
            },
        )()
        cmd_local_start(start_args)
        return

    print("\n  Applied. Restart the stack if it is already running:\n")
    print("    hermes honcho local model-apply --restart\n")


def _docker_cli_candidates() -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for name in ("docker", "docker.exe"):
        candidate = shutil.which(name)
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    if WINDOWS_DOCKER_EXE.exists():
        explicit = str(WINDOWS_DOCKER_EXE)
        if explicit not in seen:
            candidates.append(explicit)
    return candidates


def _resolve_docker_cli(require_engine: bool = False) -> str | None:
    probe_args = ["version"] if require_engine else ["compose", "version"]
    for docker_bin in _docker_cli_candidates():
        result = subprocess.run(
            [docker_bin, *probe_args],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return docker_bin
    return None


def _windows_temp_dir() -> Path:
    result = subprocess.run(
        ["cmd.exe", "/c", "echo %TEMP%"],
        capture_output=True,
        text=True,
    )
    for raw_line in reversed(result.stdout.splitlines()):
        candidate = raw_line.strip().strip("'")
        if re.match(r"^[A-Za-z]:\\", candidate):
            converted = subprocess.run(
                ["wslpath", "-u", candidate],
                capture_output=True,
                text=True,
            )
            if converted.returncode == 0 and converted.stdout.strip():
                return Path(converted.stdout.strip())
    return Path("/mnt/c/Windows/Temp")


def _needs_windows_stage(docker_bin: str) -> bool:
    return docker_bin.lower().endswith("docker.exe") and not str(LOCAL_HONCHO_DIR.resolve()).startswith("/mnt/")


def _windows_stage_dir() -> Path:
    return _windows_temp_dir() / WINDOWS_STAGE_DIR_NAME


def _stage_local_honcho_project_for_windows(force: bool = False) -> Path:
    stage_dir = _windows_stage_dir()
    if stage_dir.exists() and force:
        shutil.rmtree(stage_dir)
    if not stage_dir.exists():
        shutil.copytree(
            LOCAL_HONCHO_DIR,
            stage_dir,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        env_source = LOCAL_HONCHO_ENV if LOCAL_HONCHO_ENV.exists() else LOCAL_HONCHO_ENV_EXAMPLE
        if env_source.exists():
            shutil.copyfile(env_source, stage_dir / ".env")
    return stage_dir


def _compose_command(docker_bin: str, *args: str) -> list[str]:
    return [docker_bin, "compose", "--env-file", ".env", "-f", "compose.yaml", *args]


def _run_local_compose(*args: str) -> subprocess.CompletedProcess[str]:
    docker_bin = _resolve_docker_cli(require_engine=True)
    if not docker_bin:
        raise RuntimeError(
            "No reachable Docker engine found. Start Docker Desktop or Docker Engine "
            "and enable WSL integration if you are running inside WSL."
        )
    if _needs_windows_stage(docker_bin):
        force_stage = bool(args) and args[0] == "up"
        compose_dir = _stage_local_honcho_project_for_windows(force=force_stage)
    else:
        compose_dir = LOCAL_HONCHO_DIR
    return subprocess.run(
        _compose_command(docker_bin, *args),
        cwd=str(compose_dir),
        capture_output=True,
        text=True,
    )


def _wait_for_local_honcho(base_url: str, timeout_secs: int = DEFAULT_LOCAL_HONCHO_WAIT_SECS) -> bool:
    deadline = time.time() + max(timeout_secs, 1)
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(base_url, timeout=2):
                return True
        except urllib_error.HTTPError:
            return True
        except Exception:
            time.sleep(1)
    return False


def _write_local_honcho_config(
    base_url: str,
    *,
    workspace: str = "hermes",
    peer_name: str | None = None,
    ai_peer: str = HOST,
    memory_mode: str = "hybrid",
    recall_mode: str = "tools",
    session_strategy: str = "per-directory",
    honcho_config_path: Path | None = None,
) -> Path:
    from hermes_cli.config import load_config, save_config

    config = load_config()
    honcho_cfg = config.get("honcho")
    if not isinstance(honcho_cfg, dict):
        honcho_cfg = {}
        config["honcho"] = honcho_cfg
    honcho_cfg["enabled"] = True
    honcho_cfg["local_only"] = True
    honcho_cfg["base_url"] = base_url
    save_config(config)

    cfg_path = honcho_config_path or _config_path()
    cfg = _read_config() if honcho_config_path is None else (
        json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    )
    cfg.pop("apiKey", None)
    cfg.pop("baseUrl", None)
    cfg.pop("base_url", None)
    hosts = cfg.setdefault("hosts", {})
    hermes_host = hosts.setdefault(HOST, {})
    hermes_host["baseUrl"] = base_url
    hermes_host["enabled"] = True
    hermes_host["workspace"] = workspace
    hermes_host["aiPeer"] = ai_peer
    hermes_host["memoryMode"] = memory_mode
    hermes_host["writeFrequency"] = "async"
    hermes_host["recallMode"] = recall_mode
    hermes_host["sessionStrategy"] = session_strategy
    hermes_host["saveMessages"] = True
    if peer_name:
        hermes_host["peerName"] = peer_name
    _write_config(cfg, path=cfg_path)
    return cfg_path


def _prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    sys.stdout.write(f"  {label}{suffix}: ")
    sys.stdout.flush()
    if secret:
        if sys.stdin.isatty():
            import getpass
            val = getpass.getpass(prompt="")
        else:
            # Non-TTY (piped input, test runners) — read plaintext
            val = sys.stdin.readline().strip()
    else:
        val = sys.stdin.readline().strip()
    return val or (default or "")


def _honcho_sdk_available() -> bool:
    try:
        import honcho  # noqa: F401
        return True
    except ImportError:
        return False


def _run_install(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )


def _install_honcho_sdk() -> tuple[bool, str]:
    """Install honcho-ai into the interpreter backing this Hermes CLI."""
    attempts: list[tuple[str, list[str]]] = []

    uv_path = shutil.which("uv")
    if uv_path:
        attempts.append(
            (
                "uv pip install",
                [uv_path, "pip", "install", "--python", sys.executable, HONCHO_SDK_SPEC],
            )
        )

    pip_install_cmd = [sys.executable, "-m", "pip", "install", HONCHO_SDK_SPEC]
    attempts.append(("python -m pip install", pip_install_cmd))

    errors: list[str] = []
    for label, cmd in attempts:
        result = _run_install(cmd)
        if result.returncode == 0:
            return True, ""

        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        details = stderr or stdout or "Unknown error"
        errors.append(f"{label} failed: {details}")

        # Some managed interpreters (notably uv tool envs) can be missing pip.__main__.
        if cmd == pip_install_cmd and ("pip.__main__" in details or "No module named pip" in details):
            ensurepip = _run_install([sys.executable, "-m", "ensurepip", "--upgrade"])
            if ensurepip.returncode == 0:
                retry = _run_install(pip_install_cmd)
                if retry.returncode == 0:
                    return True, ""
                retry_err = (retry.stderr or "").strip() or (retry.stdout or "").strip() or "Unknown error"
                errors.append(f"python -m pip install (after ensurepip) failed: {retry_err}")
            else:
                ep_err = (ensurepip.stderr or "").strip() or (ensurepip.stdout or "").strip() or "Unknown error"
                errors.append(f"python -m ensurepip --upgrade failed: {ep_err}")

    return False, "\n".join(errors)


def _ensure_sdk_installed() -> bool:
    """Check honcho-ai is importable; offer to install if not. Returns True if ready."""
    if _honcho_sdk_available():
        return True

    print("  honcho-ai is not installed.")
    answer = _prompt(f"Install it now? ({HONCHO_SDK_SPEC})", default="y")
    if answer.lower() not in ("y", "yes"):
        print(
            f"  Skipping install. Run: uv pip install --python \"{sys.executable}\" "
            f"'{HONCHO_SDK_SPEC}'"
        )
        print(f"  Or: python -m pip install '{HONCHO_SDK_SPEC}'\n")
        return False

    print("  Installing honcho-ai...", flush=True)
    ok, error_text = _install_honcho_sdk()
    if ok:
        print("  Installed.\n")
        return True

    print(f"  Install failed:\n{error_text}")
    print(
        f"  Run manually: uv pip install --python \"{sys.executable}\" '{HONCHO_SDK_SPEC}'"
    )
    print(f"  Or: python -m pip install '{HONCHO_SDK_SPEC}'\n")
    return False


def cmd_local_setup(args) -> None:
    """Bootstrap a local Docker Honcho stack and point Hermes at it."""
    port = getattr(args, "port", None)
    source_dir_arg = getattr(args, "source_dir", None)
    clone_missing = not getattr(args, "no_clone", False)
    git_url = getattr(args, "git_url", DEFAULT_LOCAL_HONCHO_GIT_URL)
    start_stack = bool(getattr(args, "start", False))
    workspace = getattr(args, "workspace", None) or "hermes"
    user_name = getattr(args, "user_name", None) or os.environ.get("USER", "user")
    memory_mode = getattr(args, "memory_mode", "hybrid")
    recall_mode = getattr(args, "recall_mode", "tools")
    session_strategy = getattr(args, "session_strategy", "per-directory")

    docker_cli = _resolve_docker_cli(require_engine=False)
    if not docker_cli:
        print("  Docker is required for local Honcho. Install Docker Desktop or Docker Engine first.\n")
        return

    if not _ensure_sdk_installed():
        return

    try:
        source_path, env_path, base_url = _ensure_local_compose_env(port=port, source_dir=source_dir_arg)
        if clone_missing and not source_path.exists():
            print(f"  Cloning Honcho server source into {source_path}...", flush=True)
            _clone_local_honcho_source(source_path, git_url=git_url)
        elif not source_path.exists():
            print(f"  Honcho source not found at {source_path}")
            print("  Re-run without --no-clone to clone it automatically, or point --source-dir at an existing checkout.\n")
            return
        _ensure_local_honcho_server_config(source_path)
        local_server_config, runtime_profile = _write_local_honcho_server_config(source_path, env_path)
        cfg_path = _write_local_honcho_config(
            base_url,
            workspace=workspace,
            peer_name=user_name,
            memory_mode=memory_mode,
            recall_mode=recall_mode,
            session_strategy=session_strategy,
        )
    except Exception as exc:
        print(f"  Local Honcho setup failed: {exc}\n")
        return

    print("\nLocal Honcho setup\n" + "─" * 40)
    print(f"  Compose dir:  {LOCAL_HONCHO_DIR}")
    print(f"  Source dir:   {source_path}")
    print(f"  Env file:     {env_path}")
    print(f"  Base URL:     {base_url}")
    print(f"  Hermes path:  {cfg_path}")
    print(f"  Docker CLI:   {docker_cli}")
    if local_server_config:
        print(f"  Server cfg:   {local_server_config}")
    if runtime_profile:
        print(
            "  Main Hermes:  "
            f"{runtime_profile.get('main_provider', '')}:{runtime_profile.get('main_model', '')}"
        )
        print(
            "  Honcho sync:  "
            f"{runtime_profile.get('sync_label', 'unknown')} -> "
            f"{runtime_profile['source_provider']}:{runtime_profile['source_model']}"
        )
        print(
            "  Honcho route: "
            f"{runtime_profile['honcho_provider']}:{runtime_profile['honcho_model']}"
        )
    print("  Remote Honcho remains disabled; Hermes is pinned to this local URL only.")

    if start_stack:
        print("\n  Starting Docker stack...", flush=True)
        cmd_local_start(args)
        return

    print("\n  Next steps:")
    print("    hermes honcho local start    — build and start the stack")
    print("    hermes honcho local status   — inspect Docker services")
    print("    hermes honcho status         — inspect Hermes Honcho config\n")


def cmd_local_start(args) -> None:
    """Start the local Docker Honcho stack."""
    port = getattr(args, "port", None)
    source_dir_arg = getattr(args, "source_dir", None)
    clone_missing = not getattr(args, "no_clone", False)
    git_url = getattr(args, "git_url", DEFAULT_LOCAL_HONCHO_GIT_URL)
    wait_secs = getattr(args, "wait", DEFAULT_LOCAL_HONCHO_WAIT_SECS)

    try:
        source_path, env_path, base_url = _ensure_local_compose_env(port=port, source_dir=source_dir_arg)
        if clone_missing and not source_path.exists():
            print(f"  Cloning Honcho server source into {source_path}...", flush=True)
            _clone_local_honcho_source(source_path, git_url=git_url)
        elif not source_path.exists():
            print(f"  Honcho source not found at {source_path}")
            print("  Run 'hermes honcho local setup' first or omit --no-clone.\n")
            return
        _ensure_local_honcho_server_config(source_path)
        _, runtime_profile = _write_local_honcho_server_config(source_path, env_path)
        result = _run_local_compose("up", "-d", "--build")
    except Exception as exc:
        print(f"  Failed to start local Honcho: {exc}\n")
        return

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "docker compose up failed").strip()
        print(f"  Failed to start local Honcho.\n  Error: {details}\n")
        return

    print("  Local Honcho stack started.")
    if runtime_profile:
        print(
            "  Main Hermes: "
            f"{runtime_profile.get('main_provider', '')}:{runtime_profile.get('main_model', '')}"
        )
        print(
            "  Honcho sync: "
            f"{runtime_profile.get('sync_label', 'unknown')} -> "
            f"{runtime_profile['source_provider']}:{runtime_profile['source_model']}"
        )
        print(
            "  Honcho route: "
            f"{runtime_profile['honcho_provider']}:{runtime_profile['honcho_model']}"
        )
    if wait_secs and _wait_for_local_honcho(base_url, timeout_secs=wait_secs):
        print(f"  HTTP endpoint is reachable at {base_url}\n")
    elif wait_secs:
        print(f"  Stack started, but {base_url} did not respond within {wait_secs}s.\n")
    else:
        print()


def cmd_local_stop(args) -> None:
    """Stop the local Docker Honcho stack."""
    remove_volumes = bool(getattr(args, "volumes", False))
    compose_args = ["down"]
    if remove_volumes:
        compose_args.append("-v")
    result = _run_local_compose(*compose_args)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "docker compose down failed").strip()
        print(f"  Failed to stop local Honcho.\n  Error: {details}\n")
        return
    print("  Local Honcho stack stopped.\n")


def cmd_local_status(args) -> None:
    """Show docker compose status for the local Honcho stack."""
    try:
        _, _, base_url = _ensure_local_compose_env()
    except Exception as exc:
        print(f"  Local Honcho is not initialized: {exc}\n")
        return

    result = _run_local_compose("ps")
    print("\nLocal Honcho status\n" + "─" * 40)
    print(f"  Compose dir: {LOCAL_HONCHO_DIR}")
    print(f"  Base URL:    {base_url}")
    if result.stdout.strip():
        print()
        print(result.stdout.rstrip())
    if result.returncode != 0:
        details = (result.stderr or "docker compose ps failed").strip()
        print(f"\n  Error: {details}")
    print()


def cmd_local_logs(args) -> None:
    """Show logs for a local Honcho service."""
    service = getattr(args, "service", "api")
    tail = getattr(args, "tail", 100)
    result = _run_local_compose("logs", service, "--tail", str(tail))
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode != 0:
        details = (result.stderr or "docker compose logs failed").strip()
        print(f"\n  Error: {details}")
    elif result.stderr.strip():
        print(result.stderr.rstrip())


def cmd_setup(args) -> None:
    """Interactive Honcho setup wizard."""
    cfg = _read_config()

    active_path = _config_path()
    print("\nHoncho memory setup\n" + "─" * 40)
    print("  Honcho gives Hermes persistent cross-session memory.")
    if active_path != GLOBAL_CONFIG_PATH:
        print(f"  Instance config: {active_path}")
    else:
        print("  Config is shared with other hosts at ~/.honcho/config.json")
    print()

    if not _ensure_sdk_installed():
        return

    # All writes go to hosts.hermes — root keys are managed by the user
    # or the honcho CLI only.
    hosts = cfg.setdefault("hosts", {})
    hermes_host = hosts.setdefault(HOST, {})

    # API key — shared credential, lives at root so all hosts can read it
    current_key = cfg.get("apiKey", "")
    masked = f"...{current_key[-8:]}" if len(current_key) > 8 else ("set" if current_key else "not set")
    print(f"  Current API key: {masked}")
    new_key = _prompt("Honcho API key (leave blank to keep current)", secret=True)
    if new_key:
        cfg["apiKey"] = new_key

    effective_key = cfg.get("apiKey", "")
    if not effective_key:
        print("\n  No API key configured.")
        print("  For hosted Honcho, get your API key at https://app.honcho.dev")
        print("  For local Honcho, run: hermes honcho local setup --start\n")
        return

    # Peer name
    current_peer = hermes_host.get("peerName") or cfg.get("peerName", "")
    new_peer = _prompt("Your name (user peer)", default=current_peer or os.getenv("USER", "user"))
    if new_peer:
        hermes_host["peerName"] = new_peer

    current_workspace = hermes_host.get("workspace") or cfg.get("workspace", "hermes")
    new_workspace = _prompt("Workspace ID", default=current_workspace)
    if new_workspace:
        hermes_host["workspace"] = new_workspace

    hermes_host.setdefault("aiPeer", HOST)

    # Memory mode
    current_mode = hermes_host.get("memoryMode") or cfg.get("memoryMode", "hybrid")
    print("\n  Memory mode options:")
    print("    hybrid  — write to both Honcho and local MEMORY.md (default)")
    print("    honcho  — Honcho only, skip MEMORY.md writes")
    new_mode = _prompt("Memory mode", default=current_mode)
    if new_mode in ("hybrid", "honcho"):
        hermes_host["memoryMode"] = new_mode
    else:
        hermes_host["memoryMode"] = "hybrid"

    # Write frequency
    current_wf = str(hermes_host.get("writeFrequency") or cfg.get("writeFrequency", "async"))
    print("\n  Write frequency options:")
    print("    async   — background thread, no token cost (recommended)")
    print("    turn    — sync write after every turn")
    print("    session — batch write at session end only")
    print("    N       — write every N turns (e.g. 5)")
    new_wf = _prompt("Write frequency", default=current_wf)
    try:
        hermes_host["writeFrequency"] = int(new_wf)
    except (ValueError, TypeError):
        hermes_host["writeFrequency"] = new_wf if new_wf in ("async", "turn", "session") else "async"

    # Recall mode
    _raw_recall = hermes_host.get("recallMode") or cfg.get("recallMode", "hybrid")
    current_recall = "hybrid" if _raw_recall not in ("hybrid", "context", "tools") else _raw_recall
    print("\n  Recall mode options:")
    print("    hybrid  — auto-injected context + Honcho tools available (default)")
    print("    context — auto-injected context only, Honcho tools hidden")
    print("    tools   — Honcho tools only, no auto-injected context")
    new_recall = _prompt("Recall mode", default=current_recall)
    if new_recall in ("hybrid", "context", "tools"):
        hermes_host["recallMode"] = new_recall

    # Session strategy
    current_strat = hermes_host.get("sessionStrategy") or cfg.get("sessionStrategy", "per-directory")
    print("\n  Session strategy options:")
    print("    per-directory — one session per working directory (default)")
    print("    per-session   — new Honcho session each run, named by Hermes session ID")
    print("    per-repo      — one session per git repository (uses repo root name)")
    print("    global        — single session across all directories")
    new_strat = _prompt("Session strategy", default=current_strat)
    if new_strat in ("per-session", "per-repo", "per-directory", "global"):
        hermes_host["sessionStrategy"] = new_strat

    hermes_host.setdefault("enabled", True)
    hermes_host.setdefault("saveMessages", True)

    _write_config(cfg)
    print(f"\n  Config written to {active_path}")

    # Test connection
    print("  Testing connection... ", end="", flush=True)
    try:
        from honcho_integration.client import HonchoClientConfig, get_honcho_client, reset_honcho_client
        reset_honcho_client()
        hcfg = HonchoClientConfig.from_global_config()
        get_honcho_client(hcfg)
        print("OK")
    except Exception as e:
        print(f"FAILED\n  Error: {e}")
        return

    print("\n  Honcho is ready.")
    print(f"  Session:   {hcfg.resolve_session_name()}")
    print(f"  Workspace: {hcfg.workspace_id}")
    print(f"  Peer:      {hcfg.peer_name}")
    _mode_str = hcfg.memory_mode
    if hcfg.peer_memory_modes:
        overrides = ", ".join(f"{k}={v}" for k, v in hcfg.peer_memory_modes.items())
        _mode_str = f"{hcfg.memory_mode}  (peers: {overrides})"
    print(f"  Mode:      {_mode_str}")
    print(f"  Frequency: {hcfg.write_frequency}")
    print("\n  Honcho tools available in chat:")
    print("    honcho_context  — ask Honcho a question about you (LLM-synthesized)")
    print("    honcho_search       — semantic search over your history (no LLM)")
    print("    honcho_profile      — your peer card, key facts (no LLM)")
    print("    honcho_conclude     — persist a user fact to Honcho memory (no LLM)")
    print("\n  Other commands:")
    print("    hermes honcho status     — show full config")
    print("    hermes honcho mode       — show or change memory mode")
    print("    hermes honcho tokens     — show or set token budgets")
    print("    hermes honcho identity   — seed or show AI peer identity")
    print("    hermes honcho map <name> — map this directory to a session name\n")


def cmd_status(args) -> None:
    """Show current Honcho config and connection status."""
    try:
        import honcho  # noqa: F401
    except ImportError:
        print("  honcho-ai is not installed. Run: hermes honcho setup\n")
        return

    cfg = _read_config()

    active_path = _config_path()

    if not cfg:
        print(f"  No Honcho config found at {active_path}")
        print("  Run 'hermes honcho setup' to configure.\n")
        return

    try:
        from honcho_integration.client import HonchoClientConfig, get_honcho_client
        hcfg = HonchoClientConfig.from_global_config()
    except Exception as e:
        print(f"  Config error: {e}\n")
        return

    api_key = hcfg.api_key or ""
    masked = f"...{api_key[-8:]}" if len(api_key) > 8 else ("set" if api_key else "not set")

    print("\nHoncho status\n" + "─" * 40)
    print(f"  Enabled:        {hcfg.enabled}")
    print(f"  API key:        {masked}")
    print(f"  Base URL:       {hcfg.base_url or 'hosted / default'}")
    print(f"  Workspace:      {hcfg.workspace_id}")
    print(f"  Host:           {hcfg.host}")
    print(f"  Config path:    {active_path}")
    print(f"  AI peer:        {hcfg.ai_peer}")
    print(f"  User peer:      {hcfg.peer_name or 'not set'}")
    print(f"  Session key:    {hcfg.resolve_session_name()}")
    print(f"  Recall mode:    {hcfg.recall_mode}")
    print(f"  Memory mode:    {hcfg.memory_mode}")
    if hcfg.peer_memory_modes:
        print("  Per-peer modes:")
        for peer, mode in hcfg.peer_memory_modes.items():
            print(f"    {peer}: {mode}")
    print(f"  Write freq:     {hcfg.write_frequency}")

    if hcfg.enabled and (hcfg.api_key or hcfg.base_url):
        print("\n  Connection... ", end="", flush=True)
        try:
            if hcfg.base_url:
                if _wait_for_local_honcho(hcfg.base_url, timeout_secs=2):
                    print("OK\n")
                else:
                    print(f"FAILED (HTTP endpoint not responding at {hcfg.base_url})\n")
            else:
                get_honcho_client(hcfg)
                print("OK\n")
        except Exception as e:
            print(f"FAILED ({e})\n")
    else:
        reason = "disabled" if not hcfg.enabled else "no API key or base URL"
        print(f"\n  Not connected ({reason})")
        print("  Use 'hermes honcho local setup --start' for a local stack.\n")


def cmd_sessions(args) -> None:
    """List known directory → session name mappings."""
    cfg = _read_config()
    sessions = cfg.get("sessions", {})

    if not sessions:
        print("  No session mappings configured.\n")
        print("  Add one with: hermes honcho map <session-name>")
        print(f"  Or edit {_config_path()} directly.\n")
        return

    cwd = os.getcwd()
    print(f"\nHoncho session mappings ({len(sessions)})\n" + "─" * 40)
    for path, name in sorted(sessions.items()):
        marker = " ←" if path == cwd else ""
        print(f"  {name:<30} {path}{marker}")
    print()


def cmd_map(args) -> None:
    """Map current directory to a Honcho session name."""
    if not args.session_name:
        cmd_sessions(args)
        return

    cwd = os.getcwd()
    session_name = args.session_name.strip()

    if not session_name:
        print("  Session name cannot be empty.\n")
        return

    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', session_name).strip('-')
    if sanitized != session_name:
        print(f"  Session name sanitized to: {sanitized}")
        session_name = sanitized

    cfg = _read_config()
    cfg.setdefault("sessions", {})[cwd] = session_name
    _write_config(cfg)
    print(f"  Mapped {cwd}\n     → {session_name}\n")


def cmd_peer(args) -> None:
    """Show or update peer names and dialectic reasoning level."""
    cfg = _read_config()
    changed = False

    user_name = getattr(args, "user", None)
    ai_name = getattr(args, "ai", None)
    reasoning = getattr(args, "reasoning", None)

    REASONING_LEVELS = ("minimal", "low", "medium", "high", "max")

    if user_name is None and ai_name is None and reasoning is None:
        # Show current values
        hosts = cfg.get("hosts", {})
        hermes = hosts.get(HOST, {})
        user = hermes.get('peerName') or cfg.get('peerName') or '(not set)'
        ai = hermes.get('aiPeer') or cfg.get('aiPeer') or HOST
        lvl = hermes.get("dialecticReasoningLevel") or cfg.get("dialecticReasoningLevel") or "low"
        max_chars = hermes.get("dialecticMaxChars") or cfg.get("dialecticMaxChars") or 600
        print("\nHoncho peers\n" + "─" * 40)
        print(f"  User peer:   {user}")
        print("    Your identity in Honcho. Messages you send build this peer's card.")
        print(f"  AI peer:     {ai}")
        print("    Hermes' identity in Honcho. Seed with 'hermes honcho identity <file>'.")
        print("    Dialectic calls ask this peer questions to warm session context.")
        print()
        print(f"  Dialectic reasoning:  {lvl}  ({', '.join(REASONING_LEVELS)})")
        print(f"  Dialectic cap:        {max_chars} chars\n")
        return

    if user_name is not None:
        cfg.setdefault("hosts", {}).setdefault(HOST, {})["peerName"] = user_name.strip()
        changed = True
        print(f"  User peer → {user_name.strip()}")

    if ai_name is not None:
        cfg.setdefault("hosts", {}).setdefault(HOST, {})["aiPeer"] = ai_name.strip()
        changed = True
        print(f"  AI peer   → {ai_name.strip()}")

    if reasoning is not None:
        if reasoning not in REASONING_LEVELS:
            print(f"  Invalid reasoning level '{reasoning}'. Options: {', '.join(REASONING_LEVELS)}")
            return
        cfg.setdefault("hosts", {}).setdefault(HOST, {})["dialecticReasoningLevel"] = reasoning
        changed = True
        print(f"  Dialectic reasoning level → {reasoning}")

    if changed:
        _write_config(cfg)
        print(f"  Saved to {_config_path()}\n")


def cmd_mode(args) -> None:
    """Show or set the memory mode."""
    MODES = {
        "hybrid": "write to both Honcho and local MEMORY.md (default)",
        "honcho": "Honcho only — MEMORY.md writes disabled",
    }
    cfg = _read_config()
    mode_arg = getattr(args, "mode", None)

    if mode_arg is None:
        current = (
            (cfg.get("hosts") or {}).get(HOST, {}).get("memoryMode")
            or cfg.get("memoryMode")
            or "hybrid"
        )
        print("\nHoncho memory mode\n" + "─" * 40)
        for m, desc in MODES.items():
            marker = " ←" if m == current else ""
            print(f"  {m:<8}  {desc}{marker}")
        print("\n  Set with: hermes honcho mode [hybrid|honcho]\n")
        return

    if mode_arg not in MODES:
        print(f"  Invalid mode '{mode_arg}'. Options: {', '.join(MODES)}\n")
        return

    cfg.setdefault("hosts", {}).setdefault(HOST, {})["memoryMode"] = mode_arg
    _write_config(cfg)
    print(f"  Memory mode → {mode_arg}  ({MODES[mode_arg]})\n")


def cmd_tokens(args) -> None:
    """Show or set token budget settings."""
    cfg = _read_config()
    hosts = cfg.get("hosts", {})
    hermes = hosts.get(HOST, {})

    context = getattr(args, "context", None)
    dialectic = getattr(args, "dialectic", None)

    if context is None and dialectic is None:
        ctx_tokens = hermes.get("contextTokens") or cfg.get("contextTokens") or "(Honcho default)"
        d_chars = hermes.get("dialecticMaxChars") or cfg.get("dialecticMaxChars") or 600
        d_level = hermes.get("dialecticReasoningLevel") or cfg.get("dialecticReasoningLevel") or "low"
        print("\nHoncho budgets\n" + "─" * 40)
        print()
        print(f"  Context     {ctx_tokens} tokens")
        print("    Raw memory retrieval. Honcho returns stored facts/history about")
        print("    the user and session, injected directly into the system prompt.")
        print()
        print(f"  Dialectic   {d_chars} chars, reasoning: {d_level}")
        print("    AI-to-AI inference. Hermes asks Honcho's AI peer a question")
        print("    (e.g. \"what were we working on?\") and Honcho runs its own model")
        print("    to synthesize an answer. Used for first-turn session continuity.")
        print("    Level controls how much reasoning Honcho spends on the answer.")
        print("\n  Set with: hermes honcho tokens [--context N] [--dialectic N]\n")
        return

    changed = False
    if context is not None:
        cfg.setdefault("hosts", {}).setdefault(HOST, {})["contextTokens"] = context
        print(f"  context tokens → {context}")
        changed = True
    if dialectic is not None:
        cfg.setdefault("hosts", {}).setdefault(HOST, {})["dialecticMaxChars"] = dialectic
        print(f"  dialectic cap  → {dialectic} chars")
        changed = True

    if changed:
        _write_config(cfg)
        print(f"  Saved to {_config_path()}\n")


def cmd_identity(args) -> None:
    """Seed AI peer identity or show both peer representations."""
    cfg = _read_config()
    if not _resolve_api_key(cfg):
        print("  No API key configured. Run 'hermes honcho setup' first.\n")
        return

    file_path = getattr(args, "file", None)
    show = getattr(args, "show", False)

    try:
        from honcho_integration.client import HonchoClientConfig, get_honcho_client
        from honcho_integration.session import HonchoSessionManager
        hcfg = HonchoClientConfig.from_global_config()
        client = get_honcho_client(hcfg)
        mgr = HonchoSessionManager(honcho=client, config=hcfg)
        session_key = hcfg.resolve_session_name()
        mgr.get_or_create(session_key)
    except Exception as e:
        print(f"  Honcho connection failed: {e}\n")
        return

    if show:
        # ── User peer ────────────────────────────────────────────────────────
        user_card = mgr.get_peer_card(session_key)
        print(f"\nUser peer ({hcfg.peer_name or 'not set'})\n" + "─" * 40)
        if user_card:
            for fact in user_card:
                print(f"  {fact}")
        else:
            print("  No user peer card yet. Send a few messages to build one.")

        # ── AI peer ──────────────────────────────────────────────────────────
        ai_rep = mgr.get_ai_representation(session_key)
        print(f"\nAI peer ({hcfg.ai_peer})\n" + "─" * 40)
        if ai_rep.get("representation"):
            print(ai_rep["representation"])
        elif ai_rep.get("card"):
            print(ai_rep["card"])
        else:
            print("  No representation built yet.")
            print("  Run 'hermes honcho identity <file>' to seed one.")
        print()
        return

    if not file_path:
        print("\nHoncho identity management\n" + "─" * 40)
        print(f"  User peer: {hcfg.peer_name or 'not set'}")
        print(f"  AI peer:   {hcfg.ai_peer}")
        print()
        print("    hermes honcho identity --show        — show both peer representations")
        print("    hermes honcho identity <file>        — seed AI peer from SOUL.md or any .md/.txt\n")
        return

    from pathlib import Path
    p = Path(file_path).expanduser()
    if not p.exists():
        print(f"  File not found: {p}\n")
        return

    content = p.read_text(encoding="utf-8").strip()
    if not content:
        print(f"  File is empty: {p}\n")
        return

    source = p.name
    ok = mgr.seed_ai_identity(session_key, content, source=source)
    if ok:
        print(f"  Seeded AI peer identity from {p.name} into session '{session_key}'")
        print(f"  Honcho will incorporate this into {hcfg.ai_peer}'s representation over time.\n")
    else:
        print("  Failed to seed identity. Check logs for details.\n")


def cmd_migrate(args) -> None:
    """Step-by-step migration guide: OpenClaw native memory → Hermes + Honcho."""
    from pathlib import Path

    # ── Detect OpenClaw native memory files ──────────────────────────────────
    cwd = Path(os.getcwd())
    openclaw_home = Path.home() / ".openclaw"

    # User peer: facts about the user
    user_file_names = ["USER.md", "MEMORY.md"]
    # AI peer: agent identity / configuration
    agent_file_names = ["SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "BOOTSTRAP.md"]

    user_files: list[Path] = []
    agent_files: list[Path] = []
    for name in user_file_names:
        for d in [cwd, openclaw_home]:
            p = d / name
            if p.exists() and p not in user_files:
                user_files.append(p)
    for name in agent_file_names:
        for d in [cwd, openclaw_home]:
            p = d / name
            if p.exists() and p not in agent_files:
                agent_files.append(p)

    cfg = _read_config()
    has_key = bool(_resolve_api_key(cfg))

    print("\nHoncho migration: OpenClaw native memory → Hermes\n" + "─" * 50)
    print()
    print("  OpenClaw's native memory stores context in local markdown files")
    print("  (USER.md, MEMORY.md, SOUL.md, ...) and injects them via QMD search.")
    print("  Honcho replaces that with a cloud-backed, LLM-observable memory layer:")
    print("  context is retrieved semantically, injected automatically each turn,")
    print("  and enriched by a dialectic reasoning layer that builds over time.")
    print()

    # ── Step 1: Honcho account ────────────────────────────────────────────────
    print("Step 1  Create a Honcho account")
    print()
    if has_key:
        masked = f"...{cfg['apiKey'][-8:]}" if len(cfg["apiKey"]) > 8 else "set"
        print(f"  Honcho API key already configured: {masked}")
        print("  Skip to Step 2.")
    else:
        print("  Honcho is a cloud memory service that gives Hermes persistent memory")
        print("  across sessions. You need an API key to use it.")
        print()
        print("  1. Get your API key at https://app.honcho.dev")
        print("  2. Run:  hermes honcho setup")
        print("     Paste the key when prompted.")
        print()
        answer = _prompt("  Run 'hermes honcho setup' now?", default="y")
        if answer.lower() in ("y", "yes"):
            cmd_setup(args)
            cfg = _read_config()
            has_key = bool(cfg.get("apiKey", ""))
        else:
            print()
            print("  Run 'hermes honcho setup' when ready, then re-run this walkthrough.")

    # ── Step 2: Detected files ────────────────────────────────────────────────
    print()
    print("Step 2  Detected OpenClaw memory files")
    print()
    if user_files or agent_files:
        if user_files:
            print(f"  User memory ({len(user_files)} file(s)) — will go to Honcho user peer:")
            for f in user_files:
                print(f"    {f}")
        if agent_files:
            print(f"  Agent identity ({len(agent_files)} file(s)) — will go to Honcho AI peer:")
            for f in agent_files:
                print(f"    {f}")
    else:
        print("  No OpenClaw native memory files found in cwd or ~/.openclaw/.")
        print("  If your files are elsewhere, copy them here before continuing,")
        print("  or seed them manually:  hermes honcho identity <path/to/file>")

    # ── Step 3: Migrate user memory ───────────────────────────────────────────
    print()
    print("Step 3  Migrate user memory files → Honcho user peer")
    print()
    print("  USER.md and MEMORY.md contain facts about you that the agent should")
    print("  remember across sessions. Honcho will store these under your user peer")
    print("  and inject relevant excerpts into the system prompt automatically.")
    print()
    if user_files:
        print(f"  Found: {', '.join(f.name for f in user_files)}")
        print()
        print("  These are picked up automatically the first time you run 'hermes'")
        print("  with Honcho configured and no prior session history.")
        print("  (Hermes calls migrate_memory_files() on first session init.)")
        print()
        print("  If you want to migrate them now without starting a session:")
        for f in user_files:
            print("    hermes honcho migrate  — this step handles it interactively")
        if has_key:
            answer = _prompt("  Upload user memory files to Honcho now?", default="y")
            if answer.lower() in ("y", "yes"):
                try:
                    from honcho_integration.client import (
                        HonchoClientConfig,
                        get_honcho_client,
                        reset_honcho_client,
                    )
                    from honcho_integration.session import HonchoSessionManager

                    reset_honcho_client()
                    hcfg = HonchoClientConfig.from_global_config()
                    client = get_honcho_client(hcfg)
                    mgr = HonchoSessionManager(honcho=client, config=hcfg)
                    session_key = hcfg.resolve_session_name()
                    mgr.get_or_create(session_key)
                    # Upload from each directory that had user files
                    dirs_with_files = set(str(f.parent) for f in user_files)
                    any_uploaded = False
                    for d in dirs_with_files:
                        if mgr.migrate_memory_files(session_key, d):
                            any_uploaded = True
                    if any_uploaded:
                        print(f"  Uploaded user memory files from: {', '.join(dirs_with_files)}")
                    else:
                        print("  Nothing uploaded (files may already be migrated or empty).")
                except Exception as e:
                    print(f"  Failed: {e}")
        else:
            print("  Run 'hermes honcho setup' first, then re-run this step.")
    else:
        print("  No user memory files detected. Nothing to migrate here.")

    # ── Step 4: Seed AI identity ──────────────────────────────────────────────
    print()
    print("Step 4  Seed AI identity files → Honcho AI peer")
    print()
    print("  SOUL.md, IDENTITY.md, AGENTS.md, TOOLS.md, BOOTSTRAP.md define the")
    print("  agent's character, capabilities, and behavioral rules. In OpenClaw")
    print("  these are injected via file search at prompt-build time.")
    print()
    print("  In Hermes, they are seeded once into Honcho's AI peer through the")
    print("  observation pipeline. Honcho builds a representation from them and")
    print("  from every subsequent assistant message (observe_me=True). Over time")
    print("  the representation reflects actual behavior, not just declaration.")
    print()
    if agent_files:
        print(f"  Found: {', '.join(f.name for f in agent_files)}")
        print()
        if has_key:
            answer = _prompt("  Seed AI identity from all detected files now?", default="y")
            if answer.lower() in ("y", "yes"):
                try:
                    from honcho_integration.client import (
                        HonchoClientConfig,
                        get_honcho_client,
                        reset_honcho_client,
                    )
                    from honcho_integration.session import HonchoSessionManager

                    reset_honcho_client()
                    hcfg = HonchoClientConfig.from_global_config()
                    client = get_honcho_client(hcfg)
                    mgr = HonchoSessionManager(honcho=client, config=hcfg)
                    session_key = hcfg.resolve_session_name()
                    mgr.get_or_create(session_key)
                    for f in agent_files:
                        content = f.read_text(encoding="utf-8").strip()
                        if content:
                            ok = mgr.seed_ai_identity(session_key, content, source=f.name)
                            status = "seeded" if ok else "failed"
                            print(f"    {f.name}: {status}")
                except Exception as e:
                    print(f"  Failed: {e}")
        else:
            print("  Run 'hermes honcho setup' first, then seed manually:")
            for f in agent_files:
                print(f"    hermes honcho identity {f}")
    else:
        print("  No agent identity files detected.")
        print("  To seed manually:  hermes honcho identity <path/to/SOUL.md>")

    # ── Step 5: What changes ──────────────────────────────────────────────────
    print()
    print("Step 5  What changes vs. OpenClaw native memory")
    print()
    print("  Storage")
    print("    OpenClaw: markdown files on disk, searched via QMD at prompt-build time.")
    print("    Hermes:   cloud-backed Honcho peers. Files can stay on disk as source")
    print("              of truth; Honcho holds the live representation.")
    print()
    print("  Context injection")
    print("    OpenClaw: file excerpts injected synchronously before each LLM call.")
    print("    Hermes:   Honcho context fetched async at turn end, injected next turn.")
    print("              First turn has no Honcho context; subsequent turns are loaded.")
    print()
    print("  Memory growth")
    print("    OpenClaw: you edit files manually to update memory.")
    print("    Hermes:   Honcho observes every message and updates representations")
    print("              automatically. Files become the seed, not the live store.")
    print()
    print("  Honcho tools (available to the agent during conversation)")
    print("    honcho_context   — ask Honcho a question, get a synthesized answer (LLM)")
    print("    honcho_search        — semantic search over stored context (no LLM)")
    print("    honcho_profile       — fast peer card snapshot (no LLM)")
    print("    honcho_conclude      — write a conclusion/fact back to memory (no LLM)")
    print()
    print("  Session naming")
    print("    OpenClaw: no persistent session concept — files are global.")
    print("    Hermes:   per-session by default — each run gets its own session")
    print("              Map a custom name:  hermes honcho map <session-name>")

    # ── Step 6: Next steps ────────────────────────────────────────────────────
    print()
    print("Step 6  Next steps")
    print()
    if not has_key:
        print("  1. hermes honcho setup              — configure API key (required)")
        print("  2. hermes honcho migrate            — re-run this walkthrough")
    else:
        print("  1. hermes honcho status             — verify Honcho connection")
        print("  2. hermes                           — start a session")
        print("     (user memory files auto-uploaded on first turn if not done above)")
        print("  3. hermes honcho identity --show    — verify AI peer representation")
        print("  4. hermes honcho tokens             — tune context and dialectic budgets")
        print("  5. hermes honcho mode               — view or change memory mode")
    print()


def honcho_command(args) -> None:
    """Route honcho subcommands."""
    sub = getattr(args, "honcho_command", None)
    if sub == "setup" or sub is None:
        cmd_setup(args)
    elif sub == "local":
        local_sub = getattr(args, "honcho_local_command", None)
        if local_sub == "setup" or local_sub is None:
            cmd_local_setup(args)
        elif local_sub == "start":
            cmd_local_start(args)
        elif local_sub == "stop":
            cmd_local_stop(args)
        elif local_sub == "status":
            cmd_local_status(args)
        elif local_sub == "logs":
            cmd_local_logs(args)
        elif local_sub == "model-audit":
            cmd_local_model_audit(args)
        elif local_sub == "model-apply":
            cmd_local_model_apply(args)
        else:
            print(f"  Unknown honcho local command: {local_sub}")
            print("  Available: setup, start, stop, status, logs, model-audit, model-apply\n")
    elif sub == "status":
        cmd_status(args)
    elif sub == "sessions":
        cmd_sessions(args)
    elif sub == "map":
        cmd_map(args)
    elif sub == "peer":
        cmd_peer(args)
    elif sub == "mode":
        cmd_mode(args)
    elif sub == "tokens":
        cmd_tokens(args)
    elif sub == "identity":
        cmd_identity(args)
    elif sub == "migrate":
        cmd_migrate(args)
    else:
        print(f"  Unknown honcho command: {sub}")
        print("  Available: setup, local, status, sessions, map, peer, mode, tokens, identity, migrate\n")
