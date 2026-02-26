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
from typing import Any, Dict, Tuple, Union

CommandType = Union[str, list[str]]

_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]*(.*)$")
_MNT_PATH_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")
logger = logging.getLogger(__name__)
_last_logged_mode: str | None = None


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


def get_local_shell_mode() -> str:
    """Return one of: posix, wsl, powershell, cmd."""
    global _last_logged_mode

    if not is_windows():
        mode = "posix"
    else:
        override = os.getenv("HERMES_WINDOWS_SHELL", "auto").strip().lower()
        if override == "wsl":
            mode = "wsl" if _wsl_available() else "powershell" if _powershell_executable() else "cmd"
        elif override in {"powershell", "pwsh"}:
            mode = "powershell" if _powershell_executable() else "cmd"
        elif override in {"cmd", "cmd.exe"}:
            mode = "cmd"
        else:
            if _wsl_available():
                mode = "wsl"
            elif _powershell_executable():
                mode = "powershell"
            else:
                mode = "cmd"

    if _last_logged_mode != mode:
        if is_windows():
            override_label = os.getenv("HERMES_WINDOWS_SHELL", "auto")
            logger.info("Local shell mode selected: %s (HERMES_WINDOWS_SHELL=%s)", mode, override_label)
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


def build_local_subprocess_invocation(
    command: str,
    work_dir: str | None = None,
) -> Tuple[CommandType, Dict[str, Any], str]:
    """
    Build a subprocess invocation tuple for local backend execution.

    Returns:
      (args, popen_overrides, shell_mode)
    where popen_overrides contains platform-specific kwargs like `shell`,
    `cwd`, and process-group flags.
    """
    mode = get_local_shell_mode()

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

    if mode == "powershell":
        ps_command = command
        if work_dir:
            ps_dir = to_windows_path(work_dir).replace("'", "''")
            ps_command = f"Set-Location -LiteralPath '{ps_dir}'; {command}"
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
            },
            mode,
        )

    cmd_command = command
    if work_dir:
        cmd_dir = to_windows_path(work_dir)
        cmd_command = f'cd /d "{cmd_dir}" && {command}'
    return (
        cmd_command,
        {
            "shell": True,
            "creationflags": creationflags,
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
