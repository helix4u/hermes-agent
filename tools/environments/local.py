"""Local execution environment with interrupt support and non-blocking I/O."""

import os
import platform
import shutil
import signal
import subprocess
import threading
import time
import shlex
from pathlib import Path
from typing import Optional

_IS_WINDOWS = platform.system() == "Windows"

from tools.environments.base import BaseEnvironment


def _find_shell() -> str:
    """Find the best shell for command execution.

    On Unix: uses $SHELL, falls back to bash.
    On Windows: uses Git Bash (bundled with Git for Windows).
    Raises RuntimeError if no suitable shell is found on Windows.
    """
    if not _IS_WINDOWS:
        return os.environ.get("SHELL") or shutil.which("bash") or "/bin/bash"

    # Windows: look for Git Bash (installed with Git for Windows).
    # Allow override via env var (same pattern as Claude Code).
    custom = os.environ.get("HERMES_GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    # shutil.which finds bash.exe if Git\bin is on PATH
    found = shutil.which("bash")
    if found:
        return found

    # Check common Git for Windows install locations
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Git Bash not found. Hermes Agent requires Git for Windows on Windows.\n"
        "Install it from: https://git-scm.com/download/win\n"
        "Or set HERMES_GIT_BASH_PATH to your bash.exe location."
    )

# Noise lines emitted by interactive shells when stdin is not a terminal.
# Filtered from output to keep tool results clean.
_SHELL_NOISE_SUBSTRINGS = (
    "bash: cannot set terminal process group",
    "bash: no job control in this shell",
    "no job control in this shell",
    "cannot set terminal process group",
    "tcsetattr: Inappropriate ioctl for device",
)


def _clean_shell_noise(output: str) -> str:
    """Strip shell startup warnings that leak when using -i without a TTY.

    Removes all leading lines that match known noise patterns, not just the first.
    Some environments emit multiple noise lines (e.g. Docker, non-TTY sessions).
    """
    lines = output.split("\n")
    # Strip all leading noise lines
    while lines and any(noise in lines[0] for noise in _SHELL_NOISE_SUBSTRINGS):
        lines.pop(0)
    return "\n".join(lines)


def _venv_bin_dir(venv_path: Path) -> Path:
    """Return the executable directory for a virtual environment."""
    return venv_path / ("Scripts" if _IS_WINDOWS else "bin")


def _has_python_in_venv(venv_path: Path) -> bool:
    """Check whether a candidate venv has a Python executable."""
    bin_dir = _venv_bin_dir(venv_path)
    exe_name = "python.exe" if _IS_WINDOWS else "python"
    return (bin_dir / exe_name).is_file()


def _find_nearest_venv(start_dir: str) -> Optional[Path]:
    """Find the closest venv/.venv by walking upward from start_dir."""
    start = Path(start_dir).expanduser().resolve()
    for current in (start, *start.parents):
        for name in ("venv", ".venv"):
            candidate = current / name
            if candidate.is_dir() and _has_python_in_venv(candidate):
                return candidate
    return None


def _find_fallback_hermes_venv() -> Optional[Path]:
    """Fallback to the Hermes repo venv when cwd doesn't contain one."""
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "venv"
    if candidate.is_dir() and _has_python_in_venv(candidate):
        return candidate
    return None


def _inject_venv_env(base_env: dict, venv_path: Path) -> dict:
    """Return a copy of env with venv PATH/VIRTUAL_ENV activated."""
    env = dict(base_env)
    bin_dir = str(_venv_bin_dir(venv_path))
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    # Keep venv bin first even if startup scripts already added it elsewhere.
    path_parts = [p for p in path_parts if p != bin_dir]
    path_parts.insert(0, bin_dir)
    env["PATH"] = os.pathsep.join(path_parts)
    env["VIRTUAL_ENV"] = str(venv_path)
    return env


def _python3_shim_prefix(venv_path: Path) -> str:
    """Return shell snippet that maps python3 -> venv python when missing."""
    if _IS_WINDOWS:
        return ""
    bin_dir = _venv_bin_dir(venv_path)
    py = bin_dir / "python"
    py3 = bin_dir / "python3"
    if not py.is_file() or py3.is_file():
        return ""
    py_str = str(py)
    return f'python3() {{ "{py_str}" "$@"; }}'


def _venv_command_prefix(venv_path: Path) -> str:
    """Return shell snippet that force-activates venv for this command."""
    bin_dir = str(_venv_bin_dir(venv_path))
    venv_dir = str(venv_path)
    return (
        f'export VIRTUAL_ENV={shlex.quote(venv_dir)}; '
        f'export PATH={shlex.quote(bin_dir)}:"$PATH"'
    )


class LocalEnvironment(BaseEnvironment):
    """Run commands directly on the host machine.

    Features:
    - Popen + polling for interrupt support (user can cancel mid-command)
    - Background stdout drain thread to prevent pipe buffer deadlocks
    - stdin_data support for piping content (bypasses ARG_MAX limits)
    - sudo -S transform via SUDO_PASSWORD env var
    - Uses interactive login shell so full user env is available
    """

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)

    def execute(self, command: str, cwd: str = "", *,
                timeout: int | None = None,
                stdin_data: str | None = None) -> dict:
        from tools.terminal_tool import _interrupt_event

        work_dir = cwd or self.cwd or os.getcwd()
        effective_timeout = timeout or self.timeout
        exec_command = self._prepare_command(command)

        try:
            # Use the user's shell as an interactive login shell (-lic) so
            # that ALL rc files are sourced — including content after the
            # interactive guard in .bashrc (case $- in *i*)..esac) where
            # tools like nvm, pyenv, and cargo install their init scripts.
            # -l alone isn't enough: .profile sources .bashrc, but the guard
            # returns early because the shell isn't interactive.
            user_shell = _find_shell()
            effective_env = os.environ | self.env
            venv_path = _find_nearest_venv(work_dir) or _find_fallback_hermes_venv()
            if venv_path:
                effective_env = _inject_venv_env(effective_env, venv_path)
                exec_command = f"{_venv_command_prefix(venv_path)}; {exec_command}"
                shim = _python3_shim_prefix(venv_path)
                if shim:
                    exec_command = f"{shim}; {exec_command}"

            proc = subprocess.Popen(
                [user_shell, "-lic", exec_command],
                text=True,
                cwd=work_dir,
                env=effective_env,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                preexec_fn=None if _IS_WINDOWS else os.setsid,
            )

            if stdin_data is not None:
                def _write_stdin():
                    try:
                        proc.stdin.write(stdin_data)
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                threading.Thread(target=_write_stdin, daemon=True).start()

            _output_chunks: list[str] = []

            def _drain_stdout():
                try:
                    for line in proc.stdout:
                        _output_chunks.append(line)
                except ValueError:
                    pass
                finally:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass

            reader = threading.Thread(target=_drain_stdout, daemon=True)
            reader.start()
            deadline = time.monotonic() + effective_timeout

            while proc.poll() is None:
                if _interrupt_event.is_set():
                    try:
                        if _IS_WINDOWS:
                            proc.terminate()
                        else:
                            pgid = os.getpgid(proc.pid)
                            os.killpg(pgid, signal.SIGTERM)
                            try:
                                proc.wait(timeout=1.0)
                            except subprocess.TimeoutExpired:
                                os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    reader.join(timeout=2)
                    return {
                        "output": "".join(_output_chunks) + "\n[Command interrupted — user sent a new message]",
                        "returncode": 130,
                    }
                if time.monotonic() > deadline:
                    try:
                        if _IS_WINDOWS:
                            proc.terminate()
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    reader.join(timeout=2)
                    return self._timeout_result(effective_timeout)
                time.sleep(0.2)

            reader.join(timeout=5)
            output = _clean_shell_noise("".join(_output_chunks))
            return {"output": output, "returncode": proc.returncode}

        except Exception as e:
            return {"output": f"Execution error: {str(e)}", "returncode": 1}

    def cleanup(self):
        pass
