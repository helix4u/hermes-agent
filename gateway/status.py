"""
Gateway runtime status helpers.

Provides PID-file based detection of whether the gateway daemon is running,
used by send_message's check_fn to gate availability in the CLI.
"""

import os
from pathlib import Path

_PID_FILE = Path.home() / ".hermes" / "gateway.pid"
_LOCK_FILE = Path.home() / ".hermes" / "gateway.lock"


def write_pid_file() -> None:
    """Write the current process PID to the gateway PID file."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove the gateway PID file if it exists."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _pid_is_alive(pid: int) -> bool:
    """Best-effort process liveness check."""
    try:
        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we do not have permission to signal it.
        return True


def _read_live_gateway_pid() -> int | None:
    """Return gateway PID from pid file if it exists and is alive."""
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except ValueError:
        remove_pid_file()
        return None
    if _pid_is_alive(pid):
        return pid
    remove_pid_file()
    return None


def acquire_gateway_lock() -> tuple[bool, int | None]:
    """Acquire singleton lock for gateway startup.

    Returns:
        (True, None) when lock acquired
        (False, owner_pid) when another live process already holds the lock
    """
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    existing_pid = _read_live_gateway_pid()
    if existing_pid and existing_pid != current_pid:
        return False, existing_pid

    while True:
        try:
            # O_EXCL gives atomic "create if absent" semantics across processes.
            fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(str(current_pid))
            except Exception:
                # If writing fails, ensure we do not leave a broken lock behind.
                try:
                    _LOCK_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            return True, None
        except FileExistsError:
            try:
                owner_pid = int(_LOCK_FILE.read_text().strip())
            except Exception:
                owner_pid = None

            if owner_pid and owner_pid != current_pid and _pid_is_alive(owner_pid):
                return False, owner_pid

            # Stale/invalid lock: remove and retry acquiring atomically.
            try:
                _LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                # Another process may have replaced it between read/unlink.
                pass


def release_gateway_lock() -> None:
    """Release singleton lock if current process owns it."""
    try:
        if not _LOCK_FILE.exists():
            return
        try:
            owner_pid = int(_LOCK_FILE.read_text().strip())
        except Exception:
            owner_pid = None

        if owner_pid is not None and owner_pid != os.getpid():
            return
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def is_gateway_running() -> bool:
    """Check if the gateway daemon is currently running."""
    return _read_live_gateway_pid() is not None
