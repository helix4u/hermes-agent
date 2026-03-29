"""
Gateway subcommand for hermes CLI.

Handles: hermes gateway [run|start|stop|restart|status|install|uninstall|setup]
"""

import asyncio
import collections
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from hermes_cli.config import get_env_value, get_hermes_home, save_env_value, is_managed, managed_error
from hermes_cli.setup import (
    print_header, print_info, print_success, print_warning, print_error,
    prompt, prompt_choice, prompt_yes_no,
)
from hermes_cli.colors import Colors, color


# =============================================================================
# Process Management (for manual gateway runs)
# =============================================================================

def find_gateway_pids() -> list:
    """Find PIDs of running gateway processes."""
    pids = []

    try:
        from gateway.status import get_running_pid

        pid_from_file = get_running_pid()
        if pid_from_file:
            return [pid_from_file]

        if is_windows():
            ps_cmd = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^(python|python3|pythonw|hermes|uv)(\\.exe)?$' -and $_.CommandLine -and ("
                "$_.CommandLine -like '*hermes.exe* gateway*' -or "
                "$_.CommandLine -like '*hermes gateway*' -or "
                "$_.CommandLine -like '*hermes_cli.main gateway*' -or "
                "$_.CommandLine -like '*gateway/run.py*'"
                ") } | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
            )
            # Fallback for shells where CIM is restricted.
            if result.returncode != 0:
                ps_cmd = (
                    "Get-WmiObject Win32_Process | "
                    "Where-Object { $_.Name -match '^(python|pythonw|hermes|uv)(\\.exe)?$' -and $_.CommandLine -and ("
                    "$_.CommandLine -like '*hermes.exe* gateway*' -or "
                    "$_.CommandLine -like '*hermes gateway*' -or "
                    "$_.CommandLine -like '*hermes_cli.main gateway*' -or "
                    "$_.CommandLine -like '*gateway/run.py*'"
                    ") } | "
                    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                )

            if result.returncode == 0 and result.stdout.strip():
                import json

                data = json.loads(result.stdout.strip())
                if isinstance(data, dict):
                    data = [data]
                excluded = (
                    " gateway status",
                    " gateway stop",
                    " gateway restart",
                    " gateway install",
                    " gateway uninstall",
                )
                for item in data:
                    pid = int(item.get("ProcessId", item.get("Id", 0)))
                    cmdline = str(item.get("CommandLine", "")).lower()
                    if any(marker in cmdline for marker in excluded):
                        continue
                    if pid and pid != os.getpid() and pid not in pids:
                        pids.append(pid)
            return pids

        # Non-Windows: search process list by command patterns.
        patterns = [
            "hermes_cli.main gateway",
            "hermes gateway",
            "gateway/run.py",
        ]
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True
        )
        for line in result.stdout.split('\n'):
            # Skip grep and current process
            if 'grep' in line or str(os.getpid()) in line:
                continue
            for pattern in patterns:
                if pattern in line:
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            pid = int(parts[1])
                            if pid not in pids:
                                pids.append(pid)
                        except ValueError:
                            continue
                    break
    except Exception:
        pass

    return pids


def kill_gateway_processes(force: bool = False) -> int:
    """Kill any running gateway processes. Returns count killed."""
    pids = find_gateway_pids()
    killed = 0
    
    for pid in pids:
        try:
            if is_windows():
                cmd = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    cmd.append("/F")
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    killed += 1
                continue
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            # Process already gone
            pass
        except PermissionError:
            print(f"⚠ Permission denied to kill PID {pid}")
    
    return killed


def is_linux() -> bool:
    return sys.platform.startswith('linux')

def is_macos() -> bool:
    return sys.platform == 'darwin'

def is_windows() -> bool:
    return sys.platform == 'win32'


# =============================================================================
# Service Configuration
# =============================================================================

_SERVICE_BASE = "hermes-gateway"
SERVICE_DESCRIPTION = "Hermes Agent Gateway - Messaging Platform Integration"


def _profile_suffix() -> str:
    """Derive a service-name suffix from the current HERMES_HOME.

    Returns ``""`` for the default ``~/.hermes``, the profile name for
    ``~/.hermes/profiles/<name>``, or a short hash for any other custom
    HERMES_HOME path.
    """
    import hashlib
    import re
    from pathlib import Path as _Path
    home = get_hermes_home().resolve()
    default = (_Path.home() / ".hermes").resolve()
    if home == default:
        return ""
    # Detect ~/.hermes/profiles/<name> pattern → use the profile name
    profiles_root = (default / "profiles").resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", parts[0]):
            return parts[0]
    except ValueError:
        pass
    # Fallback: short hash for arbitrary HERMES_HOME paths
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]


def get_service_name() -> str:
    """Derive a systemd service name scoped to this HERMES_HOME.

    Default ``~/.hermes`` returns ``hermes-gateway`` (backward compatible).
    Profile ``~/.hermes/profiles/coder`` returns ``hermes-gateway-coder``.
    Any other HERMES_HOME appends a short hash for uniqueness.
    """
    suffix = _profile_suffix()
    if not suffix:
        return _SERVICE_BASE
    return f"{_SERVICE_BASE}-{suffix}"


SERVICE_NAME = _SERVICE_BASE  # backward-compat for external importers; prefer get_service_name()


def get_systemd_unit_path(system: bool = False) -> Path:
    name = get_service_name()
    if system:
        return Path("/etc/systemd/system") / f"{name}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def _systemctl_cmd(system: bool = False) -> list[str]:
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


def _journalctl_cmd(system: bool = False) -> list[str]:
    return ["journalctl"] if system else ["journalctl", "--user"]


def _service_scope_label(system: bool = False) -> str:
    return "system" if system else "user"


def get_installed_systemd_scopes() -> list[str]:
    scopes = []
    seen_paths: set[Path] = set()
    for system, label in ((False, "user"), (True, "system")):
        unit_path = get_systemd_unit_path(system=system)
        if unit_path in seen_paths:
            continue
        if unit_path.exists():
            scopes.append(label)
            seen_paths.add(unit_path)
    return scopes


def has_conflicting_systemd_units() -> bool:
    return len(get_installed_systemd_scopes()) > 1


def print_systemd_scope_conflict_warning() -> None:
    scopes = get_installed_systemd_scopes()
    if len(scopes) < 2:
        return

    rendered_scopes = " + ".join(scopes)
    print_warning(f"Both user and system gateway services are installed ({rendered_scopes}).")
    print_info("  This is confusing and can make start/stop/status behavior ambiguous.")
    print_info("  Default gateway commands target the user service unless you pass --system.")
    print_info("  Keep one of these:")
    print_info("    hermes gateway uninstall")
    print_info("    sudo hermes gateway uninstall --system")


def _effective_uid() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        return int(geteuid())
    return None


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return int(getuid())
    return None


def _require_root_for_system_service(action: str) -> None:
    if _effective_uid() != 0:
        print(f"System gateway {action} requires root. Re-run with sudo.")
        sys.exit(1)


def _system_service_identity(run_as_user: str | None = None) -> tuple[str, str, str]:
    import getpass

    try:
        import grp
        import pwd
    except ImportError:
        grp = None
        pwd = None

    username = (run_as_user or os.getenv("SUDO_USER") or os.getenv("USER") or os.getenv("LOGNAME") or getpass.getuser()).strip()
    if not username:
        raise ValueError("Could not determine which user the gateway service should run as")
    if username == "root":
        raise ValueError("Refusing to install the gateway system service as root; pass --run-as USER")

    if pwd is None or grp is None:
        home_dir = os.getenv("HOME") or os.path.expanduser("~") or str(Path.home())
        return username, username, home_dir

    try:
        user_info = pwd.getpwnam(username)
    except KeyError as e:
        raise ValueError(f"Unknown user: {username}") from e

    group_name = grp.getgrgid(user_info.pw_gid).gr_name
    return username, group_name, user_info.pw_dir


def _read_systemd_user_from_unit(unit_path: Path) -> str | None:
    if not unit_path.exists():
        return None

    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("User="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _default_system_service_user() -> str | None:
    for candidate in (os.getenv("SUDO_USER"), os.getenv("USER"), os.getenv("LOGNAME")):
        if candidate and candidate.strip() and candidate.strip() != "root":
            return candidate.strip()
    return None


def prompt_linux_gateway_install_scope() -> str | None:
    choice = prompt_choice(
        "  Choose how the gateway should run in the background:",
        [
            "User service (no sudo; best for laptops/dev boxes; may need linger after logout)",
            "System service (starts on boot; requires sudo; still runs as your user)",
            "Skip service install for now",
        ],
        default=0,
    )
    return {0: "user", 1: "system", 2: None}[choice]


def install_linux_gateway_from_setup(force: bool = False) -> tuple[str | None, bool]:
    scope = prompt_linux_gateway_install_scope()
    if scope is None:
        return None, False

    if scope == "system":
        run_as_user = _default_system_service_user()
        if _effective_uid() != 0:
            print_warning("  System service install requires sudo, so Hermes can't create it from this user session.")
            if run_as_user:
                print_info(f"  After setup, run: sudo hermes gateway install --system --run-as-user {run_as_user}")
            else:
                print_info("  After setup, run: sudo hermes gateway install --system --run-as-user <your-user>")
            print_info("  Then start it with: sudo hermes gateway start --system")
            return scope, False

        if not run_as_user:
            while True:
                run_as_user = prompt("  Run the system gateway service as which user?", default="")
                run_as_user = (run_as_user or "").strip()
                if run_as_user and run_as_user != "root":
                    break
                print_error("  Enter a non-root username.")

        systemd_install(force=force, system=True, run_as_user=run_as_user)
        return scope, True

    systemd_install(force=force, system=False)
    return scope, True


def get_systemd_linger_status() -> tuple[bool | None, str]:
    """Return whether systemd user lingering is enabled for the current user.

    Returns:
        (True, "") when linger is enabled.
        (False, "") when linger is disabled.
        (None, detail) when the status could not be determined.
    """
    if not is_linux():
        return None, "not supported on this platform"

    import shutil

    if not shutil.which("loginctl"):
        return None, "loginctl not found"

    username = os.getenv("USER") or os.getenv("LOGNAME")
    if not username:
        try:
            import pwd
            username = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            return None, "could not determine current user"

    try:
        result = subprocess.run(
            ["loginctl", "show-user", username, "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        return None, str(e)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return None, detail or "loginctl query failed"

    value = (result.stdout or "").strip().lower()
    if value in {"yes", "true", "1"}:
        return True, ""
    if value in {"no", "false", "0"}:
        return False, ""

    rendered = value or "<empty>"
    return None, f"unexpected loginctl output: {rendered}"


def print_systemd_linger_guidance() -> None:
    """Print the current linger status and the fix when it is disabled."""
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print("✓ Systemd linger is enabled (service survives logout)")
    elif linger_enabled is False:
        print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
        print("  Run: sudo loginctl enable-linger $USER")
    else:
        print(f"⚠ Could not verify systemd linger ({linger_detail})")
        print("  If you want the gateway user service to survive logout, run:")
        print("  sudo loginctl enable-linger $USER")


def _ensure_user_systemd_env() -> None:
    """Populate systemd user-session env vars when they are missing."""
    if not is_linux():
        return

    if not os.environ.get("XDG_RUNTIME_DIR"):
        uid = _current_uid()
        if uid is not None:
            runtime_dir = Path("/run/user") / str(uid)
            if runtime_dir.exists():
                os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)

    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if runtime_dir:
            bus_socket = Path(runtime_dir) / "bus"
            if bus_socket.exists():
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_socket}"

def get_launchd_plist_path() -> Path:
    """Return the launchd plist path, scoped per profile.

    Default ``~/.hermes`` → ``ai.hermes.gateway.plist`` (backward compatible).
    Profile ``~/.hermes/profiles/coder`` → ``ai.hermes.gateway-coder.plist``.
    """
    suffix = _profile_suffix()
    name = f"ai.hermes.gateway-{suffix}" if suffix else "ai.hermes.gateway"
    return Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"

def _detect_venv_dir() -> Path | None:
    """Detect the active virtualenv directory.

    Checks ``sys.prefix`` first (works regardless of the directory name),
    then falls back to probing common directory names under PROJECT_ROOT.
    Returns ``None`` when no virtualenv can be found.
    """
    # If we're running inside a virtualenv, sys.prefix points to it.
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv

    # Fallback: check common virtualenv directory names under the project root.
    for candidate in (".venv", "venv"):
        venv = PROJECT_ROOT / candidate
        if venv.is_dir():
            return venv

    return None


def get_python_path() -> str:
    venv = _detect_venv_dir()
    if venv is not None:
        if is_windows():
            venv_python = venv / "Scripts" / "python.exe"
        else:
            venv_python = venv / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
    return sys.executable

def get_hermes_cli_path() -> str:
    """Get the path to the hermes CLI."""
    # Check if installed via pip
    import shutil
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return hermes_bin
    
    # Fallback to direct module execution
    return f"{get_python_path()} -m hermes_cli.main"


def get_windows_hermes_command() -> list[str]:
    """Resolve the best command to launch the Hermes CLI on Windows."""
    candidates = [
        PROJECT_ROOT / "venv" / "Scripts" / "hermes.exe",
        PROJECT_ROOT / ".venv" / "Scripts" / "hermes.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate)]

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]

    return [sys.executable, "-m", "hermes_cli.main"]


def build_windows_gateway_shell_launch() -> list[str]:
    """Launch gateway run through a real interactive shell on Windows.

    Running the Hermes executable directly with ``CREATE_NEW_CONSOLE`` produces
    a bare console host window without the normal shell affordances users expect.
    Instead, open a real shell session and run ``hermes gateway run`` inside it.
    """
    hermes_cmd = subprocess.list2cmdline([*get_windows_hermes_command(), "gateway", "run"])
    shell_command = (
        f'set "HERMES_GATEWAY_DETACHED=1" && '
        f'cd /d "{PROJECT_ROOT}" && '
        f"{hermes_cmd}"
    )

    wt_exe = shutil.which("wt")
    if wt_exe:
        return [
            wt_exe,
            "new-tab",
            "--title",
            "Hermes Gateway",
            "cmd.exe",
            "/k",
            shell_command,
        ]

    return [
        "cmd.exe",
        "/c",
        "start",
        "\"Hermes Gateway\"",
        "cmd.exe",
        "/k",
        shell_command,
    ]


def get_gateway_log_paths() -> tuple[Path, Path]:
    """Return stdout/stderr gateway log paths."""
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "gateway.log", log_dir / "gateway-error.log"


def reset_gateway_logs() -> tuple[Path, Path]:
    """Clear gateway log files before starting a fresh detached session."""
    stdout_log, stderr_log = get_gateway_log_paths()
    for path in (stdout_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return stdout_log, stderr_log


def read_recent_gateway_logs(lines: int = 30, *, include_error: bool = False) -> str:
    """Return the most recent gateway log lines from disk."""
    stdout_log, stderr_log = get_gateway_log_paths()
    targets = [stdout_log]
    if include_error:
        targets.append(stderr_log)

    chunks: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                tail = collections.deque(handle, maxlen=max(lines, 1))
            if tail:
                header = f"== {path.name} =="
                chunks.append(header)
                chunks.append("".join(tail).rstrip())
        except Exception as exc:
            chunks.append(f"== {path.name} ==\n[failed to read log: {exc}]")

    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def follow_gateway_logs(lines: int = 30, *, include_error: bool = False) -> None:
    """Print recent gateway logs, then follow appended output until interrupted."""
    snapshot = read_recent_gateway_logs(lines=lines, include_error=include_error)
    if snapshot:
        print(snapshot)
    else:
        print("No gateway logs found yet.")

    stdout_log, stderr_log = get_gateway_log_paths()
    targets = [stdout_log]
    if include_error:
        targets.append(stderr_log)

    print()
    print("Following gateway logs. Press Ctrl+C to stop.")

    positions = {}
    for path in targets:
        if path.exists():
            positions[path] = path.stat().st_size
        else:
            positions[path] = 0

    try:
        while True:
            for path in targets:
                if not path.exists():
                    continue
                current_size = path.stat().st_size
                previous_size = positions.get(path, 0)
                if current_size < previous_size:
                    previous_size = 0
                if current_size == previous_size:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(previous_size)
                    chunk = handle.read()
                positions[path] = current_size
                if chunk:
                    print(chunk, end="" if chunk.endswith("\n") else "\n")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("Stopped following gateway logs.")


def show_gateway_logs(lines: int = 30, *, follow: bool = False, include_error: bool = False) -> None:
    """Display gateway logs from on-disk log files."""
    if follow:
        follow_gateway_logs(lines=lines, include_error=include_error)
        return

    snapshot = read_recent_gateway_logs(lines=lines, include_error=include_error)
    if snapshot:
        print(snapshot)
    else:
        print("No gateway logs found yet.")


def _summarize_startup_logs(log_lines: list[str]) -> list[str]:
    """Build a compact startup summary from recent log lines."""
    summary: list[str] = []

    def _last_matching(needle: str) -> str:
        for line in reversed(log_lines):
            if needle in line:
                return line
        return ""

    running_line = _last_matching("gateway.run: Gateway running with")
    if running_line:
        summary.append(running_line.split("gateway.run: ", 1)[-1])

    for platform in ("discord", "telegram", "slack", "whatsapp", "signal", "email"):
        if _last_matching(f"gateway.run: {platform} connected"):
            summary.append(f"{platform} connected")

    synced_line = _last_matching("slash command(s)")
    if synced_line and "Synced " in synced_line:
        summary.append(synced_line.split("gateway.platforms.discord: ", 1)[-1])

    bridge_line = _last_matching("gateway.browser_bridge: Browser bridge listening on")
    if bridge_line:
        payload = bridge_line.split("gateway.browser_bridge: ", 1)[-1]
        endpoint = payload.split(" (token file:", 1)[0]
        summary.append(endpoint)

    if _last_matching("gateway.run: Cron ticker started"):
        summary.append("Cron ticker started")

    return summary


def stream_gateway_startup_logs(
    timeout_seconds: float = 8.0,
    *,
    include_error: bool = True,
) -> None:
    """Stream startup logs for a short window in the current terminal.

    Intended for Windows detached starts so users get immediate feedback
    without needing a separate `hermes gateway logs` command.
    """
    stdout_log, stderr_log = get_gateway_log_paths()
    targets = [stdout_log]
    if include_error:
        targets.append(stderr_log)

    print()
    print(f"Startup status (checking for ~{int(timeout_seconds)}s):")
    positions: dict[Path, int] = {path: 0 for path in targets}
    printed_any = False
    collected_lines: list[str] = []
    issues: list[str] = []
    seen_issues: set[str] = set()
    deadline = time.time() + max(1.0, float(timeout_seconds))

    while time.time() < deadline:
        emitted = False
        for path in targets:
            if not path.exists():
                continue
            current_size = path.stat().st_size
            previous_size = positions.get(path, 0)
            if current_size < previous_size:
                previous_size = 0
            if current_size == previous_size:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(previous_size)
                chunk = handle.read()
            positions[path] = current_size
            if chunk:
                emitted = True
                printed_any = True
                for raw_line in chunk.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    collected_lines.append(line)
                    if " ERROR " in line or " WARNING " in line:
                        if line not in seen_issues:
                            seen_issues.add(line)
                            issues.append(line)

        if not emitted:
            time.sleep(0.4)

    if not printed_any:
        print("  (No startup log lines yet.)")
        print("Tip: run `hermes gateway logs -f` to continue watching output.")
        return

    summary = _summarize_startup_logs(collected_lines)
    if summary:
        for line in summary:
            print(f"  - {line}")
    else:
        print("  (No stable startup markers yet.)")

    if issues:
        print("  Issues:")
        for line in issues[-5:]:
            print(f"    {line}")

    print("Tip: run `hermes gateway logs -f` for full output.")


def wait_for_gateway_pid(
    timeout_seconds: float = 8.0,
    *,
    poll_interval: float = 0.25,
) -> int | None:
    """Wait briefly for the detached gateway PID file to appear."""
    from gateway.status import get_running_pid

    deadline = time.time() + max(0.5, float(timeout_seconds))
    sleep_interval = max(0.05, float(poll_interval))

    while time.time() < deadline:
        gateway_pid = get_running_pid()
        if gateway_pid:
            return gateway_pid
        time.sleep(sleep_interval)

    return get_running_pid()


def _wait_for_gateway_exit(
    timeout: float = 10.0,
    *,
    force_after: float = 5.0,
    poll_interval: float = 0.25,
) -> None:
    """Wait for the gateway PID file to disappear, force-killing if needed."""
    from gateway.status import get_running_pid

    force_signal = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM"))
    deadline = time.monotonic() + max(0.0, float(timeout))
    force_deadline = time.monotonic() + max(0.0, float(force_after))
    sleep_interval = max(0.05, float(poll_interval))
    killed = False

    while time.monotonic() < deadline:
        pid = get_running_pid()
        if not pid:
            return

        if not killed and time.monotonic() >= force_deadline:
            try:
                os.kill(pid, force_signal)
            except ProcessLookupError:
                return
            killed = True

        time.sleep(sleep_interval)


# =============================================================================
# Systemd (Linux)
# =============================================================================

def _build_user_local_paths(home: Path, path_entries: list[str]) -> list[str]:
    """Return user-local bin dirs that exist and aren't already in *path_entries*."""
    candidates = [
        str(home / ".local" / "bin"),       # uv, uvx, pip-installed CLIs
        str(home / ".cargo" / "bin"),        # Rust/cargo tools
        str(home / "go" / "bin"),            # Go tools
        str(home / ".npm-global" / "bin"),   # npm global packages
    ]
    return [p for p in candidates if p not in path_entries and Path(p).exists()]


def generate_systemd_unit(system: bool = False, run_as_user: str | None = None) -> str:
    python_path = get_python_path()
    working_dir = str(PROJECT_ROOT)
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")
    venv_bin = str(detected_venv / "bin") if detected_venv else str(PROJECT_ROOT / "venv" / "bin")
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")

    path_entries = [venv_bin, node_bin]
    resolved_node = shutil.which("node")
    if resolved_node:
        if "/" in resolved_node and "\\" not in resolved_node:
            resolved_node_dir = str(PurePosixPath(resolved_node).parent)
        else:
            resolved_node_dir = str(Path(resolved_node).parent)
        if resolved_node_dir not in path_entries:
            path_entries.append(resolved_node_dir)

    hermes_home = str(get_hermes_home().resolve())

    common_bin_paths = ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]

    if system:
        username, group_name, home_dir = _system_service_identity(run_as_user)
        path_entries.extend(_build_user_local_paths(Path(home_dir), path_entries))
        path_entries.extend(common_bin_paths)
        sane_path = ":".join(path_entries)
        return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User={username}
Group={group_name}
ExecStart={python_path} -m hermes_cli.main gateway run --replace
WorkingDirectory={working_dir}
Environment="HOME={home_dir}"
Environment="USER={username}"
Environment="LOGNAME={username}"
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="HERMES_HOME={hermes_home}"
Restart=on-failure
RestartSec=30
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    path_entries.extend(_build_user_local_paths(Path.home(), path_entries))
    path_entries.extend(common_bin_paths)
    sane_path = ":".join(path_entries)
    return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart={python_path} -m hermes_cli.main gateway run --replace
WorkingDirectory={working_dir}
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="HERMES_HOME={hermes_home}"
Restart=on-failure
RestartSec=30
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

def _normalize_service_definition(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def systemd_unit_is_current(system: bool = False) -> bool:
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False

    installed = unit_path.read_text(encoding="utf-8")
    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    expected = generate_systemd_unit(system=system, run_as_user=expected_user)
    return _normalize_service_definition(installed) == _normalize_service_definition(expected)



def refresh_systemd_unit_if_needed(system: bool = False) -> bool:
    """Rewrite the installed systemd unit when the generated definition has changed."""
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists() or systemd_unit_is_current(system=system):
        return False

    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    unit_path.write_text(generate_systemd_unit(system=system, run_as_user=expected_user), encoding="utf-8")
    subprocess.run(_systemctl_cmd(system) + ["daemon-reload"], check=True)
    print(f"↻ Updated gateway {_service_scope_label(system)} service definition to match the current Hermes install")
    return True



def _print_linger_enable_warning(username: str, detail: str | None = None) -> None:
    print()
    print("⚠ Linger not enabled — gateway may stop when you close this terminal.")
    if detail:
        print(f"  Auto-enable failed: {detail}")
    print()
    print("  On headless servers (VPS, cloud instances) run:")
    print(f"    sudo loginctl enable-linger {username}")
    print()
    print("  Then restart the gateway:")
    print(f"    systemctl --user restart {get_service_name()}.service")
    print()



def _ensure_linger_enabled() -> None:
    """Enable linger when possible so the user gateway survives logout."""
    if not is_linux():
        return

    import getpass
    import shutil

    username = getpass.getuser()
    linger_file = Path(f"/var/lib/systemd/linger/{username}")
    if linger_file.exists():
        print("✓ Systemd linger is enabled (service survives logout)")
        return

    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print("✓ Systemd linger is enabled (service survives logout)")
        return

    if not shutil.which("loginctl"):
        _print_linger_enable_warning(username, linger_detail or "loginctl not found")
        return

    print("Enabling linger so the gateway survives SSH logout...")
    try:
        result = subprocess.run(
            ["loginctl", "enable-linger", username],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        _print_linger_enable_warning(username, str(e))
        return

    if result.returncode == 0:
        print("✓ Linger enabled — gateway will persist after logout")
        return

    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    _print_linger_enable_warning(username, detail or linger_detail)


def _select_systemd_scope(system: bool = False) -> bool:
    if system:
        return True
    return get_systemd_unit_path(system=True).exists() and not get_systemd_unit_path(system=False).exists()


def systemd_install(force: bool = False, system: bool = False, run_as_user: str | None = None):
    if system:
        _require_root_for_system_service("install")

    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    if unit_path.exists() and not force:
        if systemd_unit_is_current(system=system):
            print(f"Service already installed at: {unit_path}")
            print("Use --force to reinstall")
            return
        print(f"Refreshing outdated {_service_scope_label(system)} systemd service at: {unit_path}")

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Installing {_service_scope_label(system)} systemd service to: {unit_path}")
    unit_path.write_text(generate_systemd_unit(system=system, run_as_user=run_as_user), encoding="utf-8")

    subprocess.run(_systemctl_cmd(system) + ["daemon-reload"], check=True)
    subprocess.run(_systemctl_cmd(system) + ["enable", get_service_name()], check=True)

    print()
    print(f"✓ {_service_scope_label(system).capitalize()} service installed and enabled!")
    print()
    print("Next steps:")
    print(f"  {'sudo ' if system else ''}hermes gateway start{scope_flag}              # Start the service")
    print(f"  {'sudo ' if system else ''}hermes gateway status{scope_flag}             # Check status")
    print(f"  {'journalctl' if system else 'journalctl --user'} -u {get_service_name()} -f  # View logs")
    print()

    if system:
        configured_user = _read_systemd_user_from_unit(unit_path)
        if configured_user:
            print(f"Configured to run as: {configured_user}")
    else:
        _ensure_linger_enabled()

    print_systemd_scope_conflict_warning()


def systemd_uninstall(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("uninstall")

    subprocess.run(_systemctl_cmd(system) + ["stop", get_service_name()], check=False)
    subprocess.run(_systemctl_cmd(system) + ["disable", get_service_name()], check=False)

    unit_path = get_systemd_unit_path(system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f"✓ Removed {unit_path}")

    subprocess.run(_systemctl_cmd(system) + ["daemon-reload"], check=True)
    print(f"✓ {_service_scope_label(system).capitalize()} service uninstalled")


def systemd_start(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("start")
    refresh_systemd_unit_if_needed(system=system)
    subprocess.run(_systemctl_cmd(system) + ["start", get_service_name()], check=True)
    print(f"✓ {_service_scope_label(system).capitalize()} service started")



def systemd_stop(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("stop")
    subprocess.run(_systemctl_cmd(system) + ["stop", get_service_name()], check=True)
    print(f"✓ {_service_scope_label(system).capitalize()} service stopped")



def systemd_restart(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("restart")
    refresh_systemd_unit_if_needed(system=system)
    subprocess.run(_systemctl_cmd(system) + ["restart", get_service_name()], check=True)
    print(f"✓ {_service_scope_label(system).capitalize()} service restarted")



def systemd_status(deep: bool = False, system: bool = False):
    system = _select_systemd_scope(system)
    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    if not unit_path.exists():
        print("✗ Gateway service is not installed")
        print(f"  Run: {'sudo ' if system else ''}hermes gateway install{scope_flag}")
        return

    if has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if not systemd_unit_is_current(system=system):
        print("⚠ Installed gateway service definition is outdated")
        print(f"  Run: {'sudo ' if system else ''}hermes gateway restart{scope_flag}  # auto-refreshes the unit")
        print()

    subprocess.run(
        _systemctl_cmd(system) + ["status", get_service_name(), "--no-pager"],
        capture_output=False,
    )

    result = subprocess.run(
        _systemctl_cmd(system) + ["is-active", get_service_name()],
        capture_output=True,
        text=True,
    )

    status = result.stdout.strip()

    if status == "active":
        print(f"✓ {_service_scope_label(system).capitalize()} gateway service is running")
    else:
        print(f"✗ {_service_scope_label(system).capitalize()} gateway service is stopped")
        print(f"  Run: {'sudo ' if system else ''}hermes gateway start{scope_flag}")

    configured_user = _read_systemd_user_from_unit(unit_path) if system else None
    if configured_user:
        print(f"Configured to run as: {configured_user}")

    runtime_lines = _runtime_health_lines()
    if runtime_lines:
        print()
        print("Recent gateway health:")
        for line in runtime_lines:
            print(f"  {line}")

    if system:
        print("✓ System service starts at boot without requiring systemd linger")
    elif deep:
        print_systemd_linger_guidance()
    else:
        linger_enabled, _ = get_systemd_linger_status()
        if linger_enabled is True:
            print("✓ Systemd linger is enabled (service survives logout)")
        elif linger_enabled is False:
            print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
            print("  Run: sudo loginctl enable-linger $USER")

    if deep:
        print()
        print("Recent logs:")
        subprocess.run(_journalctl_cmd(system) + ["-u", get_service_name(), "-n", "20", "--no-pager"])


# =============================================================================
# Launchd (macOS)
# =============================================================================

def get_launchd_label() -> str:
    """Return the launchd service label, scoped per profile."""
    suffix = _profile_suffix()
    return f"ai.hermes.gateway-{suffix}" if suffix else "ai.hermes.gateway"


def generate_launchd_plist() -> str:
    python_path = get_python_path()
    working_dir = str(PROJECT_ROOT)
    hermes_home = str(get_hermes_home().resolve())
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = get_launchd_label()
    # Build a sane PATH for the launchd plist.  launchd provides only a
    # minimal default (/usr/bin:/bin:/usr/sbin:/sbin) which misses Homebrew,
    # nvm, cargo, etc.  We prepend venv/bin and node_modules/.bin (matching
    # the systemd unit), then capture the user's full shell PATH so every
    # user-installed tool (node, ffmpeg, …) is reachable.
    detected_venv = _detect_venv_dir()
    venv_bin = str(detected_venv / "bin") if detected_venv else str(PROJECT_ROOT / "venv" / "bin")
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")
    # Resolve the directory containing the node binary (e.g. Homebrew, nvm)
    # so it's explicitly in PATH even if the user's shell PATH changes later.
    priority_dirs = [venv_bin, node_bin]
    resolved_node = shutil.which("node")
    if resolved_node:
        resolved_node_dir = str(Path(resolved_node).resolve().parent)
        if resolved_node_dir not in priority_dirs:
            priority_dirs.append(resolved_node_dir)
    sane_path = ":".join(
        dict.fromkeys(priority_dirs + [p for p in os.environ.get("PATH", "").split(":") if p])
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>hermes_cli.main</string>
        <string>gateway</string>
        <string>run</string>
        <string>--replace</string>
    </array>
    
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{sane_path}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
        <key>HERMES_HOME</key>
        <string>{hermes_home}</string>
    </dict>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    
    <key>StandardOutPath</key>
    <string>{log_dir}/gateway.log</string>
    
    <key>StandardErrorPath</key>
    <string>{log_dir}/gateway.error.log</string>
</dict>
</plist>
"""


def refresh_launchd_plist_if_needed() -> bool:
    """Rewrite the installed launchd plist when the generated definition changes."""
    plist_path = get_launchd_plist_path()
    if not plist_path.exists():
        return False

    generated = generate_launchd_plist()
    if (
        _normalize_service_definition(plist_path.read_text(encoding="utf-8"))
        == _normalize_service_definition(generated)
    ):
        return False

    plist_path.write_text(generated, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print("↻ Updated gateway launchd plist to match the current Hermes install")
    return True

def launchd_install(force: bool = False):
    plist_path = get_launchd_plist_path()

    generated = generate_launchd_plist()
    plist_is_current = (
        plist_path.exists()
        and _normalize_service_definition(plist_path.read_text(encoding="utf-8"))
        == _normalize_service_definition(generated)
    )
    if plist_path.exists() and not force and plist_is_current:
        print(f"Service already installed at: {plist_path}")
        print("Use --force to reinstall")
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if plist_path.exists():
        print(f"Refreshing launchd service at: {plist_path}")
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
    else:
        print(f"Installing launchd service to: {plist_path}")
    plist_path.write_text(generated, encoding="utf-8")

    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    
    print()
    print("✓ Service installed and loaded!")
    print()
    print("Next steps:")
    print("  hermes gateway status             # Check status")
    print("  tail -f ~/.hermes/logs/gateway.log  # View logs")

def launchd_uninstall():
    plist_path = get_launchd_plist_path()
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
    
    if plist_path.exists():
        plist_path.unlink()
        print(f"✓ Removed {plist_path}")
    
    print("✓ Service uninstalled")

def launchd_start():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()

    # Self-heal if the plist is missing entirely (e.g., manual cleanup, failed upgrade)
    if not plist_path.exists():
        print("↻ launchd plist missing; regenerating service definition")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(generate_launchd_plist(), encoding="utf-8")
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        subprocess.run(["launchctl", "start", label], check=True)
        print("✓ Service started")
        return

    refresh_launchd_plist_if_needed()
    try:
        subprocess.run(["launchctl", "start", label], check=True)
    except subprocess.CalledProcessError as exc:
        error_text = f"{exc.stderr or exc.stdout or exc}"
        service_missing = exc.returncode == 3 or "Could not find service" in error_text
        if not service_missing:
            raise
        if not plist_path.exists():
            raise
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        subprocess.run(["launchctl", "start", label], check=True)
    print("✓ Service started")

def launchd_stop():
    label = get_launchd_label()
    subprocess.run(["launchctl", "stop", label], check=True)
    print("✓ Service stopped")

def launchd_restart():
    launchd_stop()
    launchd_start()

def launchd_status(deep: bool = False):
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    result = subprocess.run(
        ["launchctl", "list", label],
        capture_output=True,
        text=True
    )
    
    if result.returncode == 0:
        print("✓ Gateway service is loaded")
        print(result.stdout)
    else:
        print("✗ Gateway service is not loaded")
        if plist_path.exists():
            print(f"  Local plist exists but appears stale or not loaded: {plist_path}")

    if deep:
        log_file = get_hermes_home() / "logs" / "gateway.log"
        if log_file.exists():
            print()
            print("Recent logs:")
            subprocess.run(["tail", "-20", str(log_file)])


# =============================================================================
# Gateway Runner
# =============================================================================

def run_gateway(verbose: bool = False, replace: bool = False):
    """Run the gateway in foreground.
    
    Args:
        verbose: Enable verbose logging output.
        replace: If True, kill any existing gateway instance before starting.
                 This prevents systemd restart loops when the old process
                 hasn't fully exited yet.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    
    from gateway.run import start_gateway
    
    print("┌─────────────────────────────────────────────────────────┐")
    print("│           ⚕ Hermes Gateway Starting...                 │")
    print("├─────────────────────────────────────────────────────────┤")
    print("│  Messaging platforms + cron scheduler                    │")
    print("│  Press Ctrl+C to stop                                   │")
    print("└─────────────────────────────────────────────────────────┘")
    print()
    
    detached_mode = os.getenv("HERMES_GATEWAY_DETACHED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Exit with code 1 if gateway fails to connect any platform,
    # so systemd Restart=on-failure will retry on transient errors.
    # In detached mode, treat stray KeyboardInterrupt as recoverable and restart.
    interrupt_restarts = 0
    while True:
        try:
            success = asyncio.run(start_gateway(replace=replace))
            break
        except KeyboardInterrupt:
            if detached_mode:
                interrupt_restarts += 1
                print(f"\n⚠ Detached gateway received unexpected interrupt (attempt {interrupt_restarts}); restarting...")
                if interrupt_restarts >= 3:
                    print("✗ Detached gateway kept receiving interrupts; aborting this launch.")
                    return
                time.sleep(1.0)
                continue
            print("\nGateway stopped.")
            return
    if not success:
        sys.exit(1)


# =============================================================================
# Gateway Setup (Interactive Messaging Platform Configuration)
# =============================================================================

# Per-platform config: each entry defines the env vars, setup instructions,
# and prompts needed to configure a messaging platform.
_PLATFORMS = [
    {
        "key": "telegram",
        "label": "Telegram",
        "emoji": "📱",
        "token_var": "TELEGRAM_BOT_TOKEN",
        "setup_instructions": [
            "1. Open Telegram and message @BotFather",
            "2. Send /newbot and follow the prompts to create your bot",
            "3. Copy the bot token BotFather gives you",
            "4. To find your user ID: message @userinfobot — it replies with your numeric ID",
        ],
        "vars": [
            {"name": "TELEGRAM_BOT_TOKEN", "prompt": "Bot token", "password": True,
             "help": "Paste the token from @BotFather (step 3 above)."},
            {"name": "TELEGRAM_ALLOWED_USERS", "prompt": "Allowed user IDs (comma-separated)", "password": False,
             "is_allowlist": True,
             "help": "Paste your user ID from step 4 above."},
            {"name": "TELEGRAM_HOME_CHANNEL", "prompt": "Home channel ID (for cron/notification delivery, or empty to set later with /set-home)", "password": False,
             "help": "For DMs, this is your user ID. You can set it later by typing /set-home in chat."},
        ],
    },
    {
        "key": "discord",
        "label": "Discord",
        "emoji": "💬",
        "token_var": "DISCORD_BOT_TOKEN",
        "setup_instructions": [
            "1. Go to https://discord.com/developers/applications → New Application",
            "2. Go to Bot → Reset Token → copy the bot token",
            "3. Enable: Bot → Privileged Gateway Intents → Message Content Intent",
            "4. Invite the bot to your server:",
            "   OAuth2 → URL Generator → check BOTH scopes:",
            "     - bot",
            "     - applications.commands  (required for slash commands!)",
            "   Bot Permissions: Send Messages, Read Message History, Attach Files",
            "   Copy the URL and open it in your browser to invite.",
            "5. Get your user ID: enable Developer Mode in Discord settings,",
            "   then right-click your name → Copy ID",
        ],
        "vars": [
            {"name": "DISCORD_BOT_TOKEN", "prompt": "Bot token", "password": True,
             "help": "Paste the token from step 2 above."},
            {"name": "DISCORD_ALLOWED_USERS", "prompt": "Allowed user IDs or usernames (comma-separated)", "password": False,
             "is_allowlist": True,
             "help": "Paste your user ID from step 5 above."},
            {"name": "DISCORD_HOME_CHANNEL", "prompt": "Home channel ID (for cron/notification delivery, or empty to set later with /set-home)", "password": False,
             "help": "Right-click a channel → Copy Channel ID (requires Developer Mode)."},
        ],
    },
    {
        "key": "slack",
        "label": "Slack",
        "emoji": "💼",
        "token_var": "SLACK_BOT_TOKEN",
        "setup_instructions": [
            "1. Go to https://api.slack.com/apps → Create New App → From Scratch",
            "2. Enable Socket Mode: Settings → Socket Mode → Enable",
            "   Create an App-Level Token with scope: connections:write → copy xapp-... token",
            "3. Add Bot Token Scopes: Features → OAuth & Permissions → Scopes",
            "   Required: chat:write, app_mentions:read, channels:history, channels:read,",
            "   groups:history, im:history, im:read, im:write, users:read, files:write",
            "4. Subscribe to Events: Features → Event Subscriptions → Enable",
            "   Required events: message.im, message.channels, app_mention",
            "   Optional: message.groups (for private channels)",
            "   ⚠ Without message.channels the bot will ONLY work in DMs!",
            "5. Install to Workspace: Settings → Install App → copy xoxb-... token",
            "6. Reinstall the app after any scope or event changes",
            "7. Find your user ID: click your profile → three dots → Copy member ID",
            "8. Invite the bot to channels: /invite @YourBot",
        ],
        "vars": [
            {"name": "SLACK_BOT_TOKEN", "prompt": "Bot Token (xoxb-...)", "password": True,
             "help": "Paste the bot token from step 3 above."},
            {"name": "SLACK_APP_TOKEN", "prompt": "App Token (xapp-...)", "password": True,
             "help": "Paste the app-level token from step 4 above."},
            {"name": "SLACK_ALLOWED_USERS", "prompt": "Allowed user IDs (comma-separated)", "password": False,
             "is_allowlist": True,
             "help": "Paste your member ID from step 7 above."},
        ],
    },
    {
        "key": "whatsapp",
        "label": "WhatsApp",
        "emoji": "📲",
        "token_var": "WHATSAPP_ENABLED",
    },
    {
        "key": "signal",
        "label": "Signal",
        "emoji": "📡",
        "token_var": "SIGNAL_HTTP_URL",
    },
    {
        "key": "email",
        "label": "Email",
        "emoji": "📧",
        "token_var": "EMAIL_ADDRESS",
        "setup_instructions": [
            "1. Use a dedicated email account for your Hermes agent",
            "2. For Gmail: enable 2FA, then create an App Password at",
            "   https://myaccount.google.com/apppasswords",
            "3. For other providers: use your email password or app-specific password",
            "4. IMAP must be enabled on your email account",
        ],
        "vars": [
            {"name": "EMAIL_ADDRESS", "prompt": "Email address", "password": False,
             "help": "The email address Hermes will use (e.g., hermes@gmail.com)."},
            {"name": "EMAIL_PASSWORD", "prompt": "Email password (or app password)", "password": True,
             "help": "For Gmail, use an App Password (not your regular password)."},
            {"name": "EMAIL_IMAP_HOST", "prompt": "IMAP host", "password": False,
             "help": "e.g., imap.gmail.com for Gmail, outlook.office365.com for Outlook."},
            {"name": "EMAIL_SMTP_HOST", "prompt": "SMTP host", "password": False,
             "help": "e.g., smtp.gmail.com for Gmail, smtp.office365.com for Outlook."},
            {"name": "EMAIL_ALLOWED_USERS", "prompt": "Allowed sender emails (comma-separated)", "password": False,
             "is_allowlist": True,
             "help": "Only emails from these addresses will be processed."},
        ],
    },
]


def _platform_status(platform: dict) -> str:
    """Return a plain-text status string for a platform.

    Returns uncolored text so it can safely be embedded in
    simple_term_menu items (ANSI codes break width calculation).
    """
    token_var = platform["token_var"]
    val = get_env_value(token_var)
    if token_var == "WHATSAPP_ENABLED":
        if val and val.lower() == "true":
            session_file = get_hermes_home() / "whatsapp" / "session" / "creds.json"
            if session_file.exists():
                return "configured + paired"
            return "enabled, not paired"
        return "not configured"
    if platform.get("key") == "signal":
        account = get_env_value("SIGNAL_ACCOUNT")
        if val and account:
            return "configured"
        if val or account:
            return "partially configured"
        return "not configured"
    if platform.get("key") == "email":
        pwd = get_env_value("EMAIL_PASSWORD")
        imap = get_env_value("EMAIL_IMAP_HOST")
        smtp = get_env_value("EMAIL_SMTP_HOST")
        if all([val, pwd, imap, smtp]):
            return "configured"
        if any([val, pwd, imap, smtp]):
            return "partially configured"
        return "not configured"
    if val:
        return "configured"
    return "not configured"


def _runtime_health_lines() -> list[str]:
    """Summarize the latest persisted gateway runtime health state."""
    try:
        from gateway.status import read_runtime_status
    except Exception:
        return []

    state = read_runtime_status()
    if not state:
        return []

    lines: list[str] = []
    gateway_state = state.get("gateway_state")
    exit_reason = state.get("exit_reason")
    platforms = state.get("platforms", {}) or {}

    for platform, pdata in platforms.items():
        if pdata.get("state") == "fatal":
            message = pdata.get("error_message") or "unknown error"
            lines.append(f"⚠ {platform}: {message}")

    if gateway_state == "startup_failed" and exit_reason:
        lines.append(f"⚠ Last startup issue: {exit_reason}")
    elif gateway_state == "stopped" and exit_reason:
        lines.append(f"⚠ Last shutdown reason: {exit_reason}")

    return lines


def print_runtime_health_summary(runtime_status: dict | None) -> None:
    """Print persisted gateway runtime health details when available."""
    if not runtime_status:
        return

    gateway_state = runtime_status.get("gateway_state") or "unknown"
    updated_at = runtime_status.get("updated_at")
    exit_reason = runtime_status.get("exit_reason")
    platforms = runtime_status.get("platforms") or {}

    print(f"  Runtime state: {gateway_state}")
    if exit_reason:
        print(f"  Exit reason: {exit_reason}")
    if updated_at:
        print(f"  Updated at: {updated_at}")

    if not isinstance(platforms, dict) or not platforms:
        return

    print("  Platform health:")
    for platform_name in sorted(platforms):
        platform_status = platforms.get(platform_name) or {}
        state = platform_status.get("state") or "unknown"
        error_code = platform_status.get("error_code")
        error_message = platform_status.get("error_message")
        detail = state
        if error_code:
            detail = f"{detail} ({error_code})"
        print(f"    - {platform_name}: {detail}")
        if error_message:
            print(f"      {error_message}")


def windows_start_detached_gateway(*, stream_startup: bool = True) -> None:
    """Start the gateway in its own console window on Windows."""
    from gateway.status import get_running_pid, is_gateway_running

    if is_gateway_running():
        pid = get_running_pid()
        if pid:
            print(f"✓ Gateway is already running (PID: {pid})")
        else:
            print("✓ Gateway is already running")
        return

    command = build_windows_gateway_shell_launch()
    stdout_log, stderr_log = reset_gateway_logs()
    flags = 0
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    child_env = os.environ.copy()
    # Detached gateway instances should ignore stray task cancellations
    # (for example unexpected SIGINT delivery) unless shutdown is requested.
    child_env["HERMES_GATEWAY_DETACHED"] = "1"
    proc = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=child_env,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )

    gateway_pid = wait_for_gateway_pid()
    if gateway_pid:
        print(f"✓ Gateway started in an interactive terminal window (PID: {gateway_pid})")
        print("  Live status updates should appear in the Hermes Gateway shell.")
        print(f"  Logs were reset at startup: {stdout_log}")
        if stream_startup:
            stream_gateway_startup_logs()
        return

    if proc.poll() is None:
        print(f"✓ Gateway launch requested in an interactive terminal window (PID: {proc.pid})")
        print("  Waiting for PID file; watch the Hermes Gateway shell window.")
        if stream_startup:
            stream_gateway_startup_logs()
        return

    print("✗ Gateway failed to stay running")
    print("  Check the Hermes Gateway shell window for output.")
    sys.exit(1)


def windows_stop_gateway() -> None:
    """Stop the Windows gateway process using the PID file when possible."""
    from gateway.status import get_running_pid, remove_pid_file

    pid = get_running_pid()
    if pid:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            remove_pid_file()
            print(f"✓ Stopped gateway process {pid}")
            return

    killed = kill_gateway_processes(force=True)
    if killed:
        remove_pid_file()
        print(f"✓ Stopped {killed} gateway process(es)")
    else:
        print("✗ No gateway processes found")


def windows_gateway_status() -> None:
    """Show detached gateway status on Windows."""
    from gateway.status import get_running_pid, read_runtime_status

    pid = get_running_pid()
    runtime_status = read_runtime_status()
    if pid:
        print(f"✓ Gateway is running (PID: {pid})")
        print("  (Running in its own console window on Windows)")
        print("  Live status updates appear in the Hermes Gateway console.")
        print_runtime_health_summary(runtime_status)
        return

    pids = find_gateway_pids()
    if pids:
        print(f"✓ Gateway appears to be running (PID: {', '.join(map(str, pids))})")
        print("  Warning: no PID file was found; status is based on process scan.")
        print("  Live status updates appear in the Hermes Gateway console.")
        print_runtime_health_summary(runtime_status)
        return

    print("✗ Gateway is not running")
    print_runtime_health_summary(runtime_status)
    print()
    print("To start:")
    print("  hermes gateway start")


def _setup_standard_platform(platform: dict):
    """Interactive setup for Telegram, Discord, or Slack."""
    emoji = platform["emoji"]
    label = platform["label"]
    token_var = platform["token_var"]

    print()
    print(color(f"  ─── {emoji} {label} Setup ───", Colors.CYAN))

    # Show step-by-step setup instructions if this platform has them
    instructions = platform.get("setup_instructions")
    if instructions:
        print()
        for line in instructions:
            print_info(f"  {line}")

    existing_token = get_env_value(token_var)
    if existing_token:
        print()
        print_success(f"{label} is already configured.")
        if not prompt_yes_no(f"  Reconfigure {label}?", False):
            return

    allowed_val_set = None  # Track if user set an allowlist (for home channel offer)

    for var in platform["vars"]:
        print()
        print_info(f"  {var['help']}")
        existing = get_env_value(var["name"])
        if existing and var["name"] != token_var:
            print_info(f"  Current: {existing}")

        # Allowlist fields get special handling for the deny-by-default security model
        if var.get("is_allowlist"):
            print_info("  The gateway DENIES all users by default for security.")
            print_info("  Enter user IDs to create an allowlist, or leave empty")
            print_info("  and you'll be asked about open access next.")
            value = prompt(f"  {var['prompt']}", password=False)
            if value:
                cleaned = value.replace(" ", "")
                # For Discord, strip common prefixes (user:123, <@123>, <@!123>)
                if "DISCORD" in var["name"]:
                    parts = []
                    for uid in cleaned.split(","):
                        uid = uid.strip()
                        if uid.startswith("<@") and uid.endswith(">"):
                            uid = uid.lstrip("<@!").rstrip(">")
                        if uid.lower().startswith("user:"):
                            uid = uid[5:]
                        if uid:
                            parts.append(uid)
                    cleaned = ",".join(parts)
                save_env_value(var["name"], cleaned)
                print_success("  Saved — only these users can interact with the bot.")
                allowed_val_set = cleaned
            else:
                # No allowlist — ask about open access vs DM pairing
                print()
                access_choices = [
                    "Enable open access (anyone can message the bot)",
                    "Use DM pairing (unknown users request access, you approve with 'hermes pairing approve')",
                    "Skip for now (bot will deny all users until configured)",
                ]
                access_idx = prompt_choice("  How should unauthorized users be handled?", access_choices, 1)
                if access_idx == 0:
                    save_env_value("GATEWAY_ALLOW_ALL_USERS", "true")
                    print_warning("  Open access enabled — anyone can use your bot!")
                elif access_idx == 1:
                    print_success("  DM pairing mode — users will receive a code to request access.")
                    print_info("  Approve with: hermes pairing approve {platform} {code}")
                else:
                    print_info("  Skipped — configure later with 'hermes gateway setup'")
            continue

        value = prompt(f"  {var['prompt']}", password=var.get("password", False))
        if value:
            save_env_value(var["name"], value)
            print_success(f"  Saved {var['name']}")
        elif var["name"] == token_var:
            print_warning(f"  Skipped — {label} won't work without this.")
            return
        else:
            print_info("  Skipped (can configure later)")

    # If an allowlist was set and home channel wasn't, offer to reuse
    # the first user ID (common for Telegram DMs).
    home_var = f"{label.upper()}_HOME_CHANNEL"
    home_val = get_env_value(home_var)
    if allowed_val_set and not home_val and label == "Telegram":
        first_id = allowed_val_set.split(",")[0].strip()
        if first_id and prompt_yes_no(f"  Use your user ID ({first_id}) as the home channel?", True):
            save_env_value(home_var, first_id)
            print_success(f"  Home channel set to {first_id}")

    print()
    print_success(f"{emoji} {label} configured!")


def _setup_whatsapp():
    """Delegate to the existing WhatsApp setup flow."""
    from hermes_cli.main import cmd_whatsapp
    import argparse
    cmd_whatsapp(argparse.Namespace())


def _is_service_installed() -> bool:
    """Check if the gateway is installed as a system service."""
    if is_linux():
        return get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()
    elif is_macos():
        return get_launchd_plist_path().exists()
    return False


def _is_service_running() -> bool:
    """Check if the gateway service is currently running."""
    if is_linux():
        user_unit_exists = get_systemd_unit_path(system=False).exists()
        system_unit_exists = get_systemd_unit_path(system=True).exists()

        if user_unit_exists:
            result = subprocess.run(
                _systemctl_cmd(False) + ["is-active", get_service_name()],
                capture_output=True, text=True
            )
            if result.stdout.strip() == "active":
                return True

        if system_unit_exists:
            result = subprocess.run(
                _systemctl_cmd(True) + ["is-active", get_service_name()],
                capture_output=True, text=True
            )
            if result.stdout.strip() == "active":
                return True

        return False
    elif is_macos() and get_launchd_plist_path().exists():
        result = subprocess.run(
            ["launchctl", "list", get_launchd_label()],
            capture_output=True, text=True
        )
        return result.returncode == 0
    # Check for manual processes
    return len(find_gateway_pids()) > 0


def _setup_signal():
    """Interactive setup for Signal messenger."""
    import shutil

    print()
    print(color("  ─── 📡 Signal Setup ───", Colors.CYAN))

    existing_url = get_env_value("SIGNAL_HTTP_URL")
    existing_account = get_env_value("SIGNAL_ACCOUNT")
    if existing_url and existing_account:
        print()
        print_success("Signal is already configured.")
        if not prompt_yes_no("  Reconfigure Signal?", False):
            return

    # Check if signal-cli is available
    print()
    if shutil.which("signal-cli"):
        print_success("signal-cli found on PATH.")
    else:
        print_warning("signal-cli not found on PATH.")
        print_info("  Signal requires signal-cli running as an HTTP daemon.")
        print_info("  Install options:")
        print_info("    Linux:  sudo apt install signal-cli")
        print_info("            or download from https://github.com/AsamK/signal-cli")
        print_info("    macOS:  brew install signal-cli")
        print_info("    Docker: bbernhard/signal-cli-rest-api")
        print()
        print_info("  After installing, link your account and start the daemon:")
        print_info("    signal-cli link -n \"HermesAgent\"")
        print_info("    signal-cli --account +YOURNUMBER daemon --http 127.0.0.1:8080")
        print()

    # HTTP URL
    print()
    print_info("  Enter the URL where signal-cli HTTP daemon is running.")
    default_url = existing_url or "http://127.0.0.1:8080"
    try:
        url = input(f"  HTTP URL [{default_url}]: ").strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    # Test connectivity
    print_info("  Testing connection...")
    try:
        import httpx
        resp = httpx.get(f"{url.rstrip('/')}/api/v1/check", timeout=10.0)
        if resp.status_code == 200:
            print_success("  signal-cli daemon is reachable!")
        else:
            print_warning(f"  signal-cli responded with status {resp.status_code}.")
            if not prompt_yes_no("  Continue anyway?", False):
                return
    except Exception as e:
        print_warning(f"  Could not reach signal-cli at {url}: {e}")
        if not prompt_yes_no("  Save this URL anyway? (you can start signal-cli later)", True):
            return

    save_env_value("SIGNAL_HTTP_URL", url)

    # Account phone number
    print()
    print_info("  Enter your Signal account phone number in E.164 format.")
    print_info("  Example: +15551234567")
    default_account = existing_account or ""
    try:
        account = input(f"  Account number{f' [{default_account}]' if default_account else ''}: ").strip()
        if not account:
            account = default_account
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    if not account:
        print_error("  Account number is required.")
        return

    save_env_value("SIGNAL_ACCOUNT", account)

    # Allowed users
    print()
    print_info("  The gateway DENIES all users by default for security.")
    print_info("  Enter phone numbers or UUIDs of allowed users (comma-separated).")
    existing_allowed = get_env_value("SIGNAL_ALLOWED_USERS") or ""
    default_allowed = existing_allowed or account
    try:
        allowed = input(f"  Allowed users [{default_allowed}]: ").strip() or default_allowed
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    save_env_value("SIGNAL_ALLOWED_USERS", allowed)

    # Group messaging
    print()
    if prompt_yes_no("  Enable group messaging? (disabled by default for security)", False):
        print()
        print_info("  Enter group IDs to allow, or * for all groups.")
        existing_groups = get_env_value("SIGNAL_GROUP_ALLOWED_USERS") or ""
        try:
            groups = input(f"  Group IDs [{existing_groups or '*'}]: ").strip() or existing_groups or "*"
        except (EOFError, KeyboardInterrupt):
            print("\n  Setup cancelled.")
            return
        save_env_value("SIGNAL_GROUP_ALLOWED_USERS", groups)

    print()
    print_success("Signal configured!")
    print_info(f"  URL: {url}")
    print_info(f"  Account: {account}")
    print_info("  DM auth: via SIGNAL_ALLOWED_USERS + DM pairing")
    print_info(f"  Groups: {'enabled' if get_env_value('SIGNAL_GROUP_ALLOWED_USERS') else 'disabled'}")


def gateway_setup():
    """Interactive setup for messaging platforms + gateway service."""
    if is_managed():
        managed_error("run gateway setup")
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.MAGENTA))
    print(color("│             ⚕ Gateway Setup                            │", Colors.MAGENTA))
    print(color("├─────────────────────────────────────────────────────────┤", Colors.MAGENTA))
    print(color("│  Configure messaging platforms and the gateway service. │", Colors.MAGENTA))
    print(color("│  Press Ctrl+C at any time to exit.                     │", Colors.MAGENTA))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.MAGENTA))

    # ── Gateway service status ──
    print()
    service_installed = _is_service_installed()
    service_running = _is_service_running()

    if is_linux() and has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if service_installed and service_running:
        print_success("Gateway service is installed and running.")
    elif service_installed:
        print_warning("Gateway service is installed but not running.")
        if prompt_yes_no("  Start it now?", True):
            try:
                if is_linux():
                    systemd_start()
                elif is_macos():
                    launchd_start()
            except subprocess.CalledProcessError as e:
                print_error(f"  Failed to start: {e}")
    else:
        print_info("Gateway service is not installed yet.")
        print_info("You'll be offered to install it after configuring platforms.")

    # ── Platform configuration loop ──
    while True:
        print()
        print_header("Messaging Platforms")

        menu_items = []
        for plat in _PLATFORMS:
            status = _platform_status(plat)
            menu_items.append(f"{plat['label']}  ({status})")
        menu_items.append("Done")

        choice = prompt_choice("Select a platform to configure:", menu_items, len(menu_items) - 1)

        if choice == len(_PLATFORMS):
            break

        platform = _PLATFORMS[choice]

        if platform["key"] == "whatsapp":
            _setup_whatsapp()
        elif platform["key"] == "signal":
            _setup_signal()
        else:
            _setup_standard_platform(platform)

    # ── Post-setup: offer to install/restart gateway ──
    any_configured = any(
        bool(get_env_value(p["token_var"]))
        for p in _PLATFORMS
        if p["key"] != "whatsapp"
    ) or (get_env_value("WHATSAPP_ENABLED") or "").lower() == "true"

    if any_configured:
        print()
        print(color("─" * 58, Colors.DIM))
        service_installed = _is_service_installed()
        service_running = _is_service_running()

        if service_running:
            if prompt_yes_no("  Restart the gateway to pick up changes?", True):
                try:
                    if is_linux():
                        systemd_restart()
                    elif is_macos():
                        launchd_restart()
                    else:
                        kill_gateway_processes()
                        print_info("Start manually: hermes gateway")
                except subprocess.CalledProcessError as e:
                    print_error(f"  Restart failed: {e}")
        elif service_installed:
            if prompt_yes_no("  Start the gateway service?", True):
                try:
                    if is_linux():
                        systemd_start()
                    elif is_macos():
                        launchd_start()
                except subprocess.CalledProcessError as e:
                    print_error(f"  Start failed: {e}")
        else:
            print()
            if is_linux() or is_macos():
                platform_name = "systemd" if is_linux() else "launchd"
                if prompt_yes_no(f"  Install the gateway as a {platform_name} service? (runs in background, starts on boot)", True):
                    try:
                        installed_scope = None
                        did_install = False
                        if is_linux():
                            installed_scope, did_install = install_linux_gateway_from_setup(force=False)
                        else:
                            launchd_install(force=False)
                            did_install = True
                        print()
                        if did_install and prompt_yes_no("  Start the service now?", True):
                            try:
                                if is_linux():
                                    systemd_start(system=installed_scope == "system")
                                else:
                                    launchd_start()
                            except subprocess.CalledProcessError as e:
                                print_error(f"  Start failed: {e}")
                    except subprocess.CalledProcessError as e:
                        print_error(f"  Install failed: {e}")
                        print_info("  You can try manually: hermes gateway install")
                else:
                    print_info("  You can install later: hermes gateway install")
                    if is_linux():
                        print_info("  Or as a boot-time service: sudo hermes gateway install --system")
                    print_info("  Or run in foreground:  hermes gateway")
            else:
                print_info("  Service install not supported on this platform.")
                print_info("  Run in foreground: hermes gateway")
    else:
        print()
        print_info("No platforms configured. Run 'hermes gateway setup' when ready.")

    print()


# =============================================================================
# Main Command Handler
# =============================================================================

def gateway_command(args):
    """Handle gateway subcommands."""
    subcmd = getattr(args, 'gateway_command', None)
    
    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        verbose = getattr(args, 'verbose', False)
        replace = getattr(args, 'replace', False)
        run_gateway(verbose, replace=replace)
        return

    if subcmd == "setup":
        gateway_setup()
        return

    # Service management commands
    if subcmd == "install":
        if is_managed():
            managed_error("install gateway service (managed by NixOS)")
            return
        force = getattr(args, 'force', False)
        system = getattr(args, 'system', False)
        run_as_user = getattr(args, 'run_as_user', None)
        if is_linux():
            systemd_install(force=force, system=system, run_as_user=run_as_user)
        elif is_macos():
            launchd_install(force)
        else:
            print("Service installation not supported on this platform.")
            print("Run manually: hermes gateway run")
            sys.exit(1)
    
    elif subcmd == "uninstall":
        if is_managed():
            managed_error("uninstall gateway service (managed by NixOS)")
            return
        system = getattr(args, 'system', False)
        if is_linux():
            systemd_uninstall(system=system)
        elif is_macos():
            launchd_uninstall()
        else:
            print("Not supported on this platform.")
            sys.exit(1)
    
    elif subcmd == "start":
        system = getattr(args, 'system', False)
        if is_linux():
            systemd_start(system=system)
        elif is_macos():
            launchd_start()
        elif is_windows():
            stream_startup = not bool(getattr(args, "no_startup_stream", False))
            windows_start_detached_gateway(stream_startup=stream_startup)
        else:
            print("Not supported on this platform.")
            sys.exit(1)
    
    elif subcmd == "stop":
        if is_windows():
            windows_stop_gateway()
            return

        # Try service first, then sweep any stray/manual gateway processes.
        service_available = False
        system = getattr(args, 'system', False)
        
        if is_linux() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            try:
                systemd_stop(system=system)
                service_available = True
            except subprocess.CalledProcessError:
                pass  # Fall through to process kill
        elif is_macos() and get_launchd_plist_path().exists():
            try:
                launchd_stop()
                service_available = True
            except subprocess.CalledProcessError:
                pass

        killed = kill_gateway_processes()
        if not service_available:
            if killed:
                print(f"✓ Stopped {killed} gateway process(es)")
            else:
                print("✗ No gateway processes found")
        elif killed:
            print(f"✓ Stopped {killed} additional manual gateway process(es)")
    
    elif subcmd == "restart":
        if is_windows():
            windows_stop_gateway()
            time.sleep(1)
            stream_startup = not bool(getattr(args, "no_startup_stream", False))
            windows_start_detached_gateway(stream_startup=stream_startup)
            return

        # Try service first, fall back to killing and restarting
        service_available = False
        system = getattr(args, 'system', False)
        
        if is_linux() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            try:
                systemd_restart(system=system)
                service_available = True
            except subprocess.CalledProcessError as e:
                print_error(f"Gateway service restart failed: {e}")
                sys.exit(1)
        elif is_macos() and get_launchd_plist_path().exists():
            try:
                launchd_restart()
                service_available = True
            except subprocess.CalledProcessError as e:
                print_error(f"Gateway service restart failed: {e}")
                sys.exit(1)
        
        if not service_available:
            # Manual restart: kill existing processes
            killed = kill_gateway_processes()
            if killed:
                print(f"✓ Stopped {killed} gateway process(es)")

            time.sleep(2)
            
            # Start fresh
            print("Starting gateway...")
            run_gateway(verbose=False)
    
    elif subcmd == "status":
        deep = getattr(args, 'deep', False)
        system = getattr(args, 'system', False)
        if is_windows():
            windows_gateway_status()
            return
        
        # Check for service first
        if is_linux() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            systemd_status(deep, system=system)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
        else:
            # Check for manually running processes
            pids = find_gateway_pids()
            if pids:
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                print("  (Running manually, not as a system service)")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To install as a service:")
                print("  hermes gateway install")
                print("  sudo hermes gateway install --system")
            else:
                print("✗ Gateway is not running")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To start:")
                print("  hermes gateway          # Run in foreground")
                print("  hermes gateway install  # Install as user service")
                print("  sudo hermes gateway install --system  # Install as boot-time system service")

    elif subcmd == "logs":
        lines = max(1, int(getattr(args, "lines", 30)))
        follow = bool(getattr(args, "follow", False))
        include_error = bool(getattr(args, "error", False))
        show_gateway_logs(lines=lines, follow=follow, include_error=include_error)
