"""Cross-platform helpers for local shell execution and process lifecycle."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple, Union

import yaml

CommandType = Union[str, list[str]]

_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]*(.*)$")
_MNT_PATH_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")
logger = logging.getLogger(__name__)
_last_logged_mode: str | None = None
_VALID_WINDOWS_SHELL_OVERRIDES = {"auto", "wsl", "powershell", "cmd"}


def is_windows() -> bool:
    return os.name == "nt"


@lru_cache(maxsize=1)
def _wsl_executable() -> str:
    return shutil.which("wsl.exe") or shutil.which("wsl") or ""


@lru_cache(maxsize=1)
def _powershell_executable() -> str:
    return (
        shutil.which("pwsh.exe")
        or shutil.which("pwsh")
        or shutil.which("powershell.exe")
        or shutil.which("powershell")
        or ""
    )


@lru_cache(maxsize=1)
def _wsl_available() -> bool:
    if not is_windows():
        return False
    exe = _wsl_executable()
    if not exe:
        return False
    try:
        probe = subprocess.run(
            [exe, "-e", "sh", "-lc", "exit 0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _normalize_windows_shell_override(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"pwsh", "powershell.exe"}:
        return "powershell"
    if value in {"cmd.exe"}:
        return "cmd"
    if value in _VALID_WINDOWS_SHELL_OVERRIDES:
        return value
    return "auto"


def normalize_windows_shell_override(raw: str) -> str:
    """Public wrapper used by tool schemas and UI plumbing."""
    return _normalize_windows_shell_override(raw)


@lru_cache(maxsize=1)
def _config_windows_shell_override() -> str:
    """Read optional Windows shell override from ~/.hermes/config.yaml."""
    try:
        hermes_home_raw = (os.getenv("HERMES_HOME") or "").strip()
        if hermes_home_raw:
            hermes_home = Path(hermes_home_raw)
        else:
            hermes_home = Path.home() / ".hermes"
        config_path = hermes_home / "config.yaml"
        if not config_path.exists():
            return ""
        with open(config_path, encoding="utf-8", errors="replace") as f:
            config = yaml.safe_load(f) or {}
        terminal_cfg = config.get("terminal") or {}
        candidates = [
            config.get("HERMES_WINDOWS_SHELL"),
            terminal_cfg.get("windows_shell"),
            terminal_cfg.get("shell_mode"),
            terminal_cfg.get("shell"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            normalized = _normalize_windows_shell_override(str(candidate))
            if normalized != "auto" or str(candidate).strip().lower() == "auto":
                return normalized
    except Exception:
        return ""
    return ""


def _resolve_windows_shell_override_with_source() -> tuple[str, str]:
    env_value = os.getenv("HERMES_WINDOWS_SHELL")
    if env_value and env_value.strip():
        return _normalize_windows_shell_override(env_value), "env"
    config_value = _config_windows_shell_override()
    if config_value:
        return config_value, "config"
    return "auto", "default"


def resolve_windows_shell_override() -> str:
    """Return normalized override value from env or config.yaml."""
    return _resolve_windows_shell_override_with_source()[0]


def _resolve_windows_shell_mode(override: str) -> str:
    normalized = _normalize_windows_shell_override(override)
    if normalized == "wsl":
        if _wsl_available():
            return "wsl"
        if _powershell_executable():
            return "powershell"
        return "cmd"
    if normalized in {"powershell", "pwsh"}:
        if _powershell_executable():
            return "powershell"
        return "cmd"
    if normalized in {"cmd", "cmd.exe"}:
        return "cmd"
    if shutil.which("cmd.exe") or os.environ.get("COMSPEC"):
        return "cmd"
    if _powershell_executable():
        return "powershell"
    if _wsl_available():
        return "wsl"
    return "cmd"


def get_local_shell_mode(shell_override: str | None = None) -> str:
    """Return one of: posix, wsl, powershell, cmd."""
    global _last_logged_mode

    if not is_windows():
        mode = "posix"
        override = "auto"
        override_source = "default"
    else:
        if shell_override is not None and str(shell_override).strip():
            override = _normalize_windows_shell_override(str(shell_override))
            override_source = "call"
        else:
            override, override_source = _resolve_windows_shell_override_with_source()
        mode = _resolve_windows_shell_mode(override)

    if shell_override is None and _last_logged_mode != mode:
        if is_windows():
            override_label = override
            logger.info(
                "Local shell mode selected: %s (HERMES_WINDOWS_SHELL=%s source=%s)",
                mode,
                override_label,
                override_source,
            )
        else:
            logger.info("Local shell mode selected: %s", mode)
        _last_logged_mode = mode

    return mode


def to_wsl_path(path: str) -> str:
    """Convert C:\\path style to /mnt/c/path when possible."""
    if not path:
        return path
    normalized = path.replace("\\", "/")
    if normalized.startswith("/mnt/"):
        return normalized
    match = _DRIVE_PATH_RE.match(path)
    if not match:
        return normalized
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/").lstrip("/")
    if rest:
        return f"/mnt/{drive}/{rest}"
    return f"/mnt/{drive}"


def to_windows_path(path: str) -> str:
    """Convert /mnt/c/path style to C:\\path when possible."""
    if not path:
        return path
    normalized = path.replace("\\", "/")
    match = _MNT_PATH_RE.match(normalized)
    if not match:
        return path
    drive = match.group(1).upper()
    rest = (match.group(2) or "").replace("/", "\\")
    if rest:
        return f"{drive}:\\{rest}"
    return f"{drive}:\\"


def _windows_path_safe_for_quotes(path: str) -> str:
    """Return path safe to embed in cmd double-quotes or PowerShell LiteralPath.

    On Windows, \" inside double-quotes escapes the quote, so C:\\path\\
    becomes invalid. Strip trailing backslash (and slash) so we never pass that.
    """
    if not path:
        return path
    # Keep drive roots intact (C:\, C:/, or repeated trailing slashes).
    normalized = path.replace("/", "\\")
    if re.match(r"^[A-Za-z]:\\*$", normalized):
        return normalized[:2] + "\\"
    s = path.rstrip("\\/")
    return s if s else path


def _windows_safe_cwd(work_dir: str | None) -> str:
    """Return a directory that exists for use as Popen cwd on Windows.

    If the gateway was started from a dir that no longer exists (e.g. removed drive),
    subprocesses inherit that cwd and fail with 'The filename, directory name, or
    volume label syntax is incorrect.' So we always pass an explicit cwd that exists.
    """
    if work_dir:
        try:
            resolved = os.path.abspath(os.path.expanduser(work_dir))
            if os.path.isdir(resolved):
                return resolved
        except (OSError, ValueError):
            pass
    try:
        home = os.path.expanduser("~")
        if home and os.path.isdir(home):
            return home
    except (OSError, ValueError):
        pass
    for fallback in ("C:\\", "C:\\Users", "."):
        try:
            if os.path.isdir(fallback):
                return os.path.abspath(fallback)
        except (OSError, ValueError):
            continue
    return os.getcwd()


def build_local_subprocess_invocation(
    command: str,
    work_dir: str | None = None,
    shell_override: str | None = None,
) -> Tuple[CommandType, Dict[str, Any], str]:
    """
    Build a subprocess invocation tuple for local backend execution.

    Returns:
      (args, popen_overrides, shell_mode)
    where popen_overrides contains platform-specific kwargs like `shell`,
    `cwd`, and process-group flags.
    """
    mode = get_local_shell_mode(shell_override=shell_override)

    if mode == "posix":
        return (
            command,
            {
                "shell": True,
                "cwd": work_dir,
                "preexec_fn": os.setsid,
            },
            mode,
        )

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    if mode == "wsl":
        wrapped = command
        if work_dir:
            wrapped = f"cd {shlex.quote(to_wsl_path(work_dir))} && {command}"
        return (
            [_wsl_executable(), "-e", "bash", "-lc", wrapped],
            {
                "shell": False,
                "creationflags": creationflags,
            },
            mode,
        )

    # Windows cmd/PowerShell: pass explicit cwd so we never inherit a broken gateway cwd
    safe_cwd = _windows_safe_cwd(work_dir) if is_windows() else None
    try:
        work_dir_exists = bool(
            work_dir
            and os.path.isdir(os.path.abspath(os.path.expanduser(work_dir)))
        )
    except (OSError, ValueError, TypeError):
        work_dir_exists = False

    if mode == "powershell":
        ps_command = command
        if work_dir_exists:
            raw = to_windows_path(work_dir)
            ps_dir = _windows_path_safe_for_quotes(raw).replace("'", "''")
            ps_command = f"Set-Location -LiteralPath '{ps_dir}'; {command}"
        logger.info(
            "Local invocation: mode=%s cwd=%r ps_command=%s",
            mode,
            safe_cwd,
            ps_command[:300] + ("..." if len(ps_command) > 300 else ""),
        )
        return (
            [
                _powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_command,
            ],
            {
                "shell": False,
                "creationflags": creationflags,
                "cwd": safe_cwd,
            },
            mode,
        )

    # Rely on Popen cwd=safe_cwd for working directory. Do not embed "cd /d \"path\""
    # in the /c argument: subprocess escapes the inner quotes when building the
    # Windows command line, so cmd sees \"...\" and fails with "The filename,
    # directory name, or volume label syntax is incorrect."
    cmd_command = command
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
    logger.info(
        "Local invocation: mode=%s cwd=%r cmd_command=%s",
        mode,
        safe_cwd,
        cmd_command[:300] + ("..." if len(cmd_command) > 300 else ""),
    )
    return (
        [comspec, "/d", "/s", "/c", cmd_command],
        {
            "shell": False,
            "creationflags": creationflags,
            "cwd": safe_cwd,
        },
        mode,
    )


def terminate_process_tree(proc: subprocess.Popen, *, force: bool = False) -> None:
    """Terminate a process and its children on both POSIX and Windows."""
    if proc is None or proc.poll() is not None:
        return

    if is_windows():
        try:
            cmd = ["taskkill", "/PID", str(proc.pid), "/T"]
            if force:
                cmd.append("/F")
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

        if proc.poll() is None:
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except Exception:
                pass
        return

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass
