#!/usr/bin/env python3
"""
Generate a short multi-voice comedic crewcast recap from plain text context.

Usage:
  /Users/gille/.hermes/hermes-agent/venv/bin/python3 \
    /Users/gille/.hermes/hermes-agent/scripts/make_crew_recap.py \
    --topic "MST3K workflow shipped" \
    --detail "All four tasks are complete and the wiki verifier passed clean"
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

import make_crew_debate as debate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PODCASTS_DIR = PROJECT_ROOT / "podcasts"
CONFIG_DIR = PROJECT_ROOT / "config"


def ensure_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Missing required binary: {name}")


def lines_for(topic: str, detail: str) -> list[tuple[str, str]]:
    topic = debate.normalize_text(topic)
    detail = debate.normalize_text(detail)

    return [
        (
            "HOUSE",
            f"Welcome back to Differential Diagnosis FM. Case file: {topic}. "
            "Wayne, Daryl, Squirrely Dan, Katy, Sheldon, Narrator... everybody in.",
        ),
        (
            "WAYNE",
            f"Well {detail}, and if we're being honest, that's what I appreciates about this update.",
        ),
        (
            "DARYL",
            "To be fair, House, the checklist actually got finished. That's rare enough to log as a weather event.",
        ),
        (
            "SQUIRRELY_DAN",
            "Allegedly, we dids the hard part: no busted links, no busted citations, and no busted nerves... mostly.",
        ),
        (
            "KATY",
            "Let's keep it cute and factual: done, verified, and actually readable. Miracles happen.",
        ),
        (
            "SHELDON",
            "Statistically speaking, completion plus validation implies competent execution. I am uncomfortable, but impressed.",
        ),
        (
            "NARRATOR",
            "In a world where every update wants applause before evidence, this one brought receipts.",
        ),
        (
            "HOUSE",
            "Final diagnosis: high signal, low nonsense. Treatment plan... keep the crew, keep the jokes, keep the verifier.",
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate short comedic multi-voice crew recap")
    parser.add_argument("--topic", required=True, help="Short topic title")
    parser.add_argument("--detail", required=True, help="One-line factual detail")
    parser.add_argument("--speed", type=float, default=1.75, help="Speed for alternate render")
    parser.add_argument("--pause-seconds", type=float, default=0.22, help="Pause between lines")
    parser.add_argument("--out-dir", default=None, help="Optional output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.speed <= 0:
        raise ValueError("--speed must be > 0")
    if args.pause_seconds <= 0:
        raise ValueError("--pause-seconds must be > 0")

    for binary in ("edge-tts", "ffmpeg"):
        ensure_binary(binary)

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = PODCASTS_DIR / f"crew_recap_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    voice_map_path = CONFIG_DIR / "voice_map.json"
    debate.write_voice_map(voice_map_path)

    lines = lines_for(args.topic, args.detail)
    script_path = debate.write_debate_script(out_dir, "local://crew-recap", args.topic, lines)

    normal_ogg, normal_mp3 = debate.render_tts(lines, out_dir, pause_seconds=args.pause_seconds)
    speed_ogg, speed_mp3 = debate.render_sped_up(normal_ogg, args.speed)

    print("=== Output files ===")
    print(f"Debate script:       {script_path}")
    print(f"Normal OGG:          {normal_ogg}")
    print(f"Normal MP3:          {normal_mp3}")
    print(f"Speed OGG ({args.speed}x): {speed_ogg}")
    print(f"Speed MP3 ({args.speed}x): {speed_mp3}")

    # Deterministic chat delivery hook for Hermes gateway/platform adapters.
    # The gateway scans tool output for MEDIA tags and auto-uploads attachments.
    delivery_ogg = speed_ogg if speed_ogg.exists() else normal_ogg
    print("\n=== Hermes delivery tags ===")
    print("[[audio_as_voice]]")
    print(f"MEDIA:{delivery_ogg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
