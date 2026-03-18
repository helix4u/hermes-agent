"""Shared ANSI color utilities for Hermes CLI modules."""

import os
import re
import sys


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


_ANSI_RE = re.compile(r"(?:\x1b\[|\x9b\[|\?\[)[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)


def _ansi_supported() -> bool:
    """Best-effort ANSI support detection for Hermes CLI output."""
    if not sys.stdout.isatty():
        return False
    if os.getenv("NO_COLOR"):
        return False
    if os.name == "nt":
        shell = (os.getenv("HERMES_WINDOWS_SHELL") or "").strip().lower()
        if shell == "cmd":
            return False
        comspec = os.path.basename((os.getenv("COMSPEC") or "").strip()).lower()
        if comspec == "cmd.exe" and not shell:
            return False
    return True


def color(text: str, *codes) -> str:
    """Apply color codes to text (only when output is a TTY)."""
    plain = strip_ansi(text)
    if not _ansi_supported():
        return plain
    return "".join(codes) + plain + Colors.RESET
