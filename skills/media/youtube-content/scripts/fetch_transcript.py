#!/usr/bin/env python3
"""Compatibility wrapper for legacy youtube-content transcript helper.

Canonical implementation lives in: skills/media/yt-dlp/scripts/fetch_transcript.py
"""

from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve()
CANONICAL = HERE.parents[2] / "yt-dlp" / "scripts" / "fetch_transcript.py"

if not CANONICAL.exists():
    print(f"Error: canonical transcript helper not found: {CANONICAL}", file=sys.stderr)
    sys.exit(1)

runpy.run_path(str(CANONICAL), run_name="__main__")
