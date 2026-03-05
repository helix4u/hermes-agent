#!/usr/bin/env python3
"""
One-command YouTube crew debate podcast generator.

Pipeline:
1) Fetch YouTube metadata + transcript
2) Build transcript files and debate script (House + crew)
3) Render speaker lines with Edge TTS using fixed voice map
4) Stitch into OGG/MP3
5) Create speed-adjusted version (default 1.75x)

Usage:
  python3 scripts/make_crew_debate.py "https://youtu.be/<VIDEO_ID>" --speed 1.75
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PODCASTS_DIR = PROJECT_ROOT / "podcasts"
CONFIG_DIR = PROJECT_ROOT / "config"

DEFAULT_VOICE_MAP: Dict[str, Dict[str, str]] = {
    "HOUSE": {"voice": "en-US-ChristopherNeural", "rate": "+0%"},
    "WAYNE": {"voice": "en-US-BrianNeural", "rate": "-6%"},
    "DARYL": {"voice": "en-US-RogerNeural", "rate": "+2%"},
    "SQUIRRELY_DAN": {"voice": "en-US-GuyNeural", "rate": "-4%"},
    "KATY": {"voice": "en-US-AriaNeural", "rate": "+0%"},
    "SHELDON": {"voice": "en-US-EricNeural", "rate": "+15%"},
    "NARRATOR": {"voice": "en-US-AndrewNeural", "rate": "-10%"},
}


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: List[str], capture_output: bool = False) -> str:
    """Run command and raise readable errors."""
    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )
        return (result.stdout or "").strip()
    except subprocess.CalledProcessError as exc:
        cmd_str = " ".join(shlex.quote(x) for x in cmd)
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        details = stderr or stdout or str(exc)
        raise RuntimeError(f"Command failed: {cmd_str}\n{details}") from exc


def ensure_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(
            f"Missing required binary: {name}. Please install it and retry."
        )


def extract_video_id(url_or_id: str) -> str:
    value = url_or_id.strip()
    patterns = [
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise ValueError("Could not extract a valid 11-character YouTube video ID.")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def normalize_text(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_words(text: str, max_words: int = 22) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(" ,;:-") + "…"


def sentence_candidates(text: str) -> List[str]:
    raw = re.split(r"(?<=[.!?])\s+", text)
    clean = [normalize_text(x) for x in raw if normalize_text(x)]
    return clean


def summarize_chunk(text: str) -> str:
    text = normalize_text(text)
    if not text:
        return "the key facts and how the story was framed"

    for candidate in sentence_candidates(text):
        word_count = len(candidate.split())
        if 8 <= word_count <= 28:
            return truncate_words(candidate, 24)

    return truncate_words(text, 20)


def youtube_metadata(url: str) -> Tuple[str, List[dict]]:
    output = run_cmd(["yt-dlp", "-J", "--skip-download", "--no-warnings", url], capture_output=True)
    data = json.loads(output)
    title = normalize_text(data.get("title") or "Untitled video")
    chapters = data.get("chapters") or []
    return title, chapters


def fetch_transcript(video_id: str, languages: List[str] | None) -> List[dict]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "python package 'youtube-transcript-api' is required. "
            "Install it in the active environment."
        ) from exc

    api = YouTubeTranscriptApi()

    def _convert(items: Iterable) -> List[dict]:
        converted = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("text", "")
                start = float(item.get("start", 0.0))
                duration = float(item.get("duration", 0.0))
            else:
                text = getattr(item, "text", "")
                start = float(getattr(item, "start", 0.0))
                duration = float(getattr(item, "duration", 0.0))
            clean_text = normalize_text(str(text))
            if clean_text:
                converted.append({"text": clean_text, "start": start, "duration": duration})
        return converted

    # New API path (v1.x)
    if hasattr(api, "fetch"):
        try:
            fetched = api.fetch(video_id, languages=languages) if languages else api.fetch(video_id)
            return _convert(fetched)
        except Exception:
            if languages:
                # fallback to auto language selection
                fetched = api.fetch(video_id)
                return _convert(fetched)
            raise

    # Legacy API path
    from youtube_transcript_api import YouTubeTranscriptApi as LegacyApi  # type: ignore

    if languages:
        return _convert(LegacyApi.get_transcript(video_id, languages=languages))
    return _convert(LegacyApi.get_transcript(video_id))


def chapterized_transcript(segments: List[dict], chapters: List[dict]) -> str:
    if not chapters:
        text = " ".join(seg["text"] for seg in segments)
        return textwrap.fill(text, width=100)

    lines: List[str] = []
    for idx, chapter in enumerate(chapters):
        title = normalize_text(str(chapter.get("title") or f"Section {idx + 1}"))
        start = float(chapter.get("start_time", 0.0) or 0.0)
        next_start = None
        if idx + 1 < len(chapters):
            next_start = float(chapters[idx + 1].get("start_time", 0.0) or 0.0)
        end = chapter.get("end_time")
        if end is None and next_start is not None:
            end = next_start
        end_float = float(end) if end is not None else None

        chunk = [
            seg["text"]
            for seg in segments
            if seg["start"] >= start and (end_float is None or seg["start"] < end_float)
        ]

        if not chunk:
            continue

        lines.append(f"## {title}")
        lines.append("")
        lines.append(textwrap.fill(" ".join(chunk), width=100))
        lines.append("")

    if not lines:
        text = " ".join(seg["text"] for seg in segments)
        return textwrap.fill(text, width=100)

    return "\n".join(lines).strip()


def derive_topics(chapters: List[dict], segments: List[dict], count: int = 3) -> List[str]:
    topics: List[str] = []

    skip_exact = {"intro", "introduction", "outro", "conclusion"}
    skip_contains = {"sponsor", "sponsored", "ad read", "advert", "patreon", "merch"}

    for chapter in chapters:
        title = normalize_text(str(chapter.get("title") or ""))
        lower_title = title.lower()
        if not title:
            continue
        if lower_title in skip_exact:
            continue
        if any(word in lower_title for word in skip_contains):
            continue

        topics.append(title)
        if len(topics) >= count:
            break

    if len(topics) < count:
        n = len(segments)
        if n == 0:
            fallback = [
                "the opening claims",
                "the accountability section",
                "the closing evidence check",
            ]
            return fallback[:count]

        # Fill remaining topics with chunk summaries from transcript text
        chunk_size = max(1, n // count)
        for idx in range(count):
            if len(topics) >= count:
                break
            start = idx * chunk_size
            end = n if idx == count - 1 else min(n, (idx + 1) * chunk_size)
            chunk_text = " ".join(seg["text"] for seg in segments[start:end])
            summary = summarize_chunk(chunk_text)
            if summary:
                topics.append(summary)

    while len(topics) < count:
        topics.append("the next major argument in the video")

    return [truncate_words(topic, 18) for topic in topics[:count]]


def debate_lines(title: str, topics: List[str]) -> List[Tuple[str, str]]:
    t1, t2, t3 = topics

    return [
        (
            "HOUSE",
            f"All right, welcome to Clinic of Bad Decisions. Case file is {title}. "
            f"Wayne, Sheldon, Katy, Daryl, Squirrely Dan, and Narrator are here. "
            f"Topic one: {t1}. Wayne, go.",
        ),
        (
            "WAYNE",
            f"On {t1}, the headline's loud, but the real test is whether anyone can explain the objective without dodging.",
        ),
        (
            "SHELDON",
            f"Correct. {t1} is a systems problem: when messaging lags behind action, public trust decays predictably.",
        ),
        (
            "KATY",
            "Translation: people hear confident talking points, but they still don't get a straight answer in plain English.",
        ),
        (
            "SQUIRRELY_DAN",
            "Katy, that's exactly it. If regular folks can't describe what success even means, they assume they're being handled.",
        ),
        (
            "DARYL",
            "To be fair, once that pattern starts, every new statement feels like damage control instead of information.",
        ),
        (
            "NARRATOR",
            "When certainty arrives before evidence, the room doesn't get calmer. It gets louder.",
        ),
        (
            "HOUSE",
            f"Good. Topic two: {t2}. Daryl, start us off.",
        ),
        (
            "DARYL",
            f"On {t2}, the video keeps pointing to the gap between what officials claim and what the record actually shows.",
        ),
        (
            "WAYNE",
            "If the receipts and the rhetoric don't match, trust doesn't wobble. It drops.",
        ),
        (
            "SHELDON",
            "And once competence signals collapse, every subsequent claim is discounted. That's basic incentive math.",
        ),
        (
            "KATY",
            "Exactly. If they stonewall in public, people assume the private version is worse.",
        ),
        (
            "SQUIRRELY_DAN",
            "Trust doesn't break all at once. It breaks one evasive answer at a time.",
        ),
        (
            "HOUSE",
            f"Final block: {t3}. Narrator, your poetry hour.",
        ),
        (
            "NARRATOR",
            f"In {t3}, everybody fights for first interpretation. One clip, three narratives, and a sprint to define reality.",
        ),
        (
            "WAYNE",
            "Then watch the source material yourself and decide. Don't rent your opinion from somebody's panel segment.",
        ),
        (
            "KATY",
            "Raw footage first, commentary second. That's the whole survival kit.",
        ),
        (
            "SHELDON",
            "Consensus recommendation: prioritize primary evidence, then evaluate commentary for incentives and omissions.",
        ),
        (
            "HOUSE",
            "Diagnosis: high noise, medium signal, chronic spin. Treatment: read the transcript, watch the footage, distrust certainty. Session over.",
        ),
    ]


def write_voice_map(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_VOICE_MAP, indent=2) + "\n", encoding="utf-8")


def write_transcript_files(out_dir: Path, segments: List[dict], chapters: List[dict]) -> None:
    timestamped_lines = [f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments]
    (out_dir / "transcript_timestamped.txt").write_text(
        "\n".join(timestamped_lines) + "\n", encoding="utf-8"
    )
    (out_dir / "transcript_clean.txt").write_text(
        chapterized_transcript(segments, chapters).strip() + "\n", encoding="utf-8"
    )


def write_debate_script(out_dir: Path, source_url: str, title: str, lines: List[Tuple[str, str]]) -> Path:
    script_path = out_dir / "debate_script.txt"
    out_lines = [f"Source video: {source_url}", f"Title: {title}", ""]

    for speaker, text in lines:
        meta = DEFAULT_VOICE_MAP[speaker]
        cleaned = normalize_text(text)
        out_lines.append(
            f"[{speaker}] ({meta['voice']}, rate {meta['rate']}) {cleaned}"
        )

    script_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return script_path


def render_tts(lines: List[Tuple[str, str]], out_dir: Path, pause_seconds: float) -> Tuple[Path, Path]:
    segments_dir = out_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    mp3_files: List[Path] = []

    for idx, (speaker, text) in enumerate(lines, start=1):
        voice = DEFAULT_VOICE_MAP[speaker]["voice"]
        rate = DEFAULT_VOICE_MAP[speaker]["rate"]
        mp3_path = segments_dir / f"{idx:02d}_{speaker.lower()}.mp3"
        log(f"  - [{idx:02d}/{len(lines)}] {speaker}")
        run_cmd(
            [
                "edge-tts",
                "--voice",
                voice,
                f"--rate={rate}",
                "--text",
                text,
                "--write-media",
                str(mp3_path),
            ]
        )
        mp3_files.append(mp3_path)

    wav_files: List[Path] = []
    for mp3 in mp3_files:
        wav = mp3.with_suffix(".wav")
        run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", "24000", str(wav)])
        wav_files.append(wav)

    pause_wav = segments_dir / f"pause_{int(pause_seconds * 1000)}ms.wav"
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            f"{pause_seconds:.3f}",
            str(pause_wav),
        ]
    )

    concat_file = out_dir / "concat.txt"
    concat_lines: List[str] = []
    for i, wav in enumerate(wav_files):
        concat_lines.append(f"file '{wav.resolve()}'")
        if i != len(wav_files) - 1:
            concat_lines.append(f"file '{pause_wav.resolve()}'")
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    out_ogg = out_dir / "podcast_debate_multi_voice.ogg"
    out_mp3 = out_dir / "podcast_debate_multi_voice.mp3"

    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            str(out_ogg),
        ]
    )
    run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out_ogg), "-codec:a", "libmp3lame", "-q:a", "3", str(out_mp3)])

    return out_ogg, out_mp3


def atempo_filter(speed: float) -> str:
    if speed <= 0:
        raise ValueError("speed must be > 0")

    filters: List[str] = []
    remaining = speed

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining *= 2.0

    filters.append(f"atempo={remaining:.5f}".rstrip("0").rstrip("."))
    return ",".join(filters)


def speed_suffix(speed: float) -> str:
    txt = f"{speed:.2f}".rstrip("0").rstrip(".")
    return txt.replace(".", "p") + "x"


def render_sped_up(input_ogg: Path, speed: float) -> Tuple[Path, Path]:
    suffix = speed_suffix(speed)
    out_ogg = input_ogg.with_name(f"podcast_debate_multi_voice_{suffix}.ogg")
    out_mp3 = input_ogg.with_name(f"podcast_debate_multi_voice_{suffix}.mp3")

    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_ogg),
            "-filter:a",
            atempo_filter(speed),
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            str(out_ogg),
        ]
    )
    run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out_ogg), "-codec:a", "libmp3lame", "-q:a", "3", str(out_mp3)])

    return out_ogg, out_mp3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a House + crew debate podcast from a YouTube URL in one command."
    )
    parser.add_argument("youtube_url", help="YouTube URL or 11-char video ID")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.75,
        help="Playback speed for additional render (default: 1.75)",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Comma-separated transcript language preferences (default: en)",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.26,
        help="Pause inserted between lines in seconds (default: 0.26)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Optional output directory. Defaults to podcasts/youtube_debate_<timestamp>",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=None,
        help="Optional cap on transcript segments (debug/testing)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.speed <= 0:
        raise ValueError("--speed must be > 0")
    if args.pause_seconds <= 0:
        raise ValueError("--pause-seconds must be > 0")

    for binary in ("edge-tts", "ffmpeg", "yt-dlp"):
        ensure_binary(binary)

    video_id = extract_video_id(args.youtube_url)
    language_list = [x.strip() for x in args.language.split(",") if x.strip()] or None

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = PODCASTS_DIR / f"youtube_debate_{ts}"

    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"[1/6] Output directory: {out_dir}")

    voice_map_path = CONFIG_DIR / "voice_map.json"
    write_voice_map(voice_map_path)
    log(f"[2/6] Voice map written: {voice_map_path}")

    log("[3/6] Fetching metadata + transcript...")
    try:
        title, chapters = youtube_metadata(args.youtube_url)
    except Exception as exc:
        log(f"  ! Metadata fetch warning: {exc}")
        title, chapters = (f"YouTube video {video_id}", [])

    segments = fetch_transcript(video_id, language_list)
    if args.max_segments:
        segments = segments[: args.max_segments]
    if not segments:
        raise RuntimeError("No transcript segments were returned.")

    write_transcript_files(out_dir, segments, chapters)
    log("  - Wrote transcript_timestamped.txt")
    log("  - Wrote transcript_clean.txt")

    topics = derive_topics(chapters, segments, count=3)
    lines = debate_lines(title, topics)
    script_path = write_debate_script(out_dir, args.youtube_url, title, lines)
    log(f"  - Wrote debate script: {script_path}")

    log("[4/6] Rendering speaker lines with Edge TTS...")
    normal_ogg, normal_mp3 = render_tts(lines, out_dir, pause_seconds=args.pause_seconds)

    log("[5/6] Rendering speed-adjusted version...")
    speed_ogg, speed_mp3 = render_sped_up(normal_ogg, args.speed)

    log("[6/6] Done ✅")
    print("\n=== Output files ===")
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
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
