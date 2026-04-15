"""Tool handlers for the youtube-intake plugin."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}"
)


def _json_result(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _extract_video_id(url_or_id: str) -> str:
    value = str(url_or_id or "").strip()
    if _VIDEO_ID_RE.fullmatch(value):
        return value

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if not host:
        raise ValueError("A YouTube URL or 11-character video ID is required.")

    if "youtu.be" in host:
        candidate = path.strip("/").split("/", 1)[0]
        if _VIDEO_ID_RE.fullmatch(candidate):
            return candidate

    if "youtube.com" in host or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(parsed.query)
        candidate = (qs.get("v") or [""])[0].strip()
        if _VIDEO_ID_RE.fullmatch(candidate):
            return candidate
        pieces = [p for p in path.split("/") if p]
        if len(pieces) >= 2 and pieces[0] in {"shorts", "embed", "live"}:
            candidate = pieces[1]
            if _VIDEO_ID_RE.fullmatch(candidate):
                return candidate

    raise ValueError("Could not extract a valid YouTube video ID from the input.")


def _workspace_root() -> Path:
    return Path.cwd()


def _scratch_dir(video_id: str) -> Path:
    root = _workspace_root() / "TMP" / "youtube-intake" / video_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _metadata_with_ytdlp(url: str) -> dict:
    cmd = [
        "yt-dlp",
        "--print",
        "%(title)s",
        "--print",
        "%(channel)s",
        "--print",
        "%(duration_string)s",
        url,
    ]
    proc = _run(cmd, timeout=120)
    if proc.returncode != 0:
        return {
            "title": "",
            "channel": "",
            "duration": "",
            "warning": (proc.stderr or proc.stdout or "yt-dlp metadata fetch failed").strip()[:500],
        }
    lines = [line.strip().strip('"') for line in (proc.stdout or "").splitlines() if line.strip()]
    return {
        "title": lines[0] if len(lines) > 0 else "",
        "channel": lines[1] if len(lines) > 1 else "",
        "duration": lines[2] if len(lines) > 2 else "",
        "warning": "",
    }


def _preferred_languages(language: str) -> list[str]:
    requested = str(language or "").strip().replace("_", "-")
    if not requested:
        return ["en", "en-US"]
    base = requested.split("-", 1)[0]
    ordered: list[str] = []
    for code in [requested, base, "en", "en-US"]:
        if code and code not in ordered:
            ordered.append(code)
    return ordered


def _fetch_with_transcript_api(video_id: str, language: str, include_timestamps: bool) -> dict:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:
        raise RuntimeError(f"youtube_transcript_api unavailable: {exc}") from exc

    languages = _preferred_languages(language)
    fetched = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
    lines: list[str] = []
    clean_segments: list[str] = []
    for segment in fetched:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        clean_segments.append(text)
        if include_timestamps:
            start = float(segment.get("start") or 0.0)
            hh = int(start // 3600)
            mm = int((start % 3600) // 60)
            ss = int(start % 60)
            lines.append(f"{hh:02d}:{mm:02d}:{ss:02d} {text}")
        else:
            lines.append(text)
    transcript_text = "\n".join(lines).strip()
    return {
        "source": "youtube_transcript_api",
        "transcript_text": transcript_text,
        "segment_count": len(clean_segments),
        "raw_path": "",
    }


def _cleanup_hashdata_for_video(video_id: str, scratch: Path) -> list[str]:
    moved: list[str] = []
    ws = _workspace_root()
    weird_dir = ws / "#data" / "tmp" / "yt"
    if weird_dir.exists():
        for candidate in weird_dir.glob(f"*{video_id}*.vtt"):
            target = scratch / candidate.name
            if target.exists():
                candidate.unlink()
            else:
                shutil.move(str(candidate), str(target))
            moved.append(str(target))
        for d in [ws / "#data" / "tmp" / "yt", ws / "#data" / "tmp", ws / "#data"]:
            try:
                d.rmdir()
            except Exception:
                pass
    return moved


def _find_downloaded_vtt(scratch: Path, video_id: str) -> Path | None:
    patterns = [
        f"*{video_id}*.vtt",
        f"{video_id}.en.vtt",
        f"{video_id}.en-US.vtt",
        f"{video_id}.en-orig.vtt",
        f"{video_id}.webm#.en.vtt",
    ]
    for pattern in patterns:
        matches = sorted(scratch.glob(pattern))
        if matches:
            return matches[0]
    return None


def _download_with_ytdlp(url: str, video_id: str, scratch: Path) -> dict:
    template = str((scratch / "%(id)s.%(ext)s").resolve())
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-subs",
        "--sub-langs",
        "en",
        "--sub-format",
        "vtt",
        "-o",
        template,
        url,
    ]
    proc = _run(cmd, timeout=240)
    moved = _cleanup_hashdata_for_video(video_id, scratch)
    raw_vtt = _find_downloaded_vtt(scratch, video_id)
    if proc.returncode != 0 or raw_vtt is None:
        stderr = (proc.stderr or proc.stdout or "yt-dlp transcript fetch failed").strip()
        raise RuntimeError(stderr[:1000])
    return {
        "source": "yt-dlp-auto-subs",
        "raw_path": str(raw_vtt),
        "cleanup_moved": moved,
    }


def _normalize_vtt(raw_path: Path, include_timestamps: bool) -> tuple[str, int]:
    cleaned_lines: list[str] = []
    segment_count = 0
    current_timestamp = ""
    last_text = ""
    for raw in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line in {"WEBVTT", "Kind: captions"} or line.startswith("Language:"):
            continue
        if _TIMESTAMP_RE.match(line):
            current_timestamp = line.split(" --> ", 1)[0][:8]
            continue
        line = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", line)
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == last_text:
            continue
        last_text = line
        segment_count += 1
        if include_timestamps and current_timestamp:
            cleaned_lines.append(f"{current_timestamp} {line}")
        else:
            cleaned_lines.append(line)
    return ("\n".join(cleaned_lines).strip(), segment_count)


def youtube_digest(args: dict, **kwargs) -> str:
    try:
        url_or_id = str(args.get("url") or "").strip()
        if not url_or_id:
            return _json_result({"ok": False, "error": "A YouTube URL or video ID is required."})

        language = str(args.get("language") or "").strip()
        include_timestamps = bool(args.get("include_timestamps", False))
        keep_artifacts = bool(args.get("keep_artifacts", False))

        video_id = _extract_video_id(url_or_id)
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        scratch = _scratch_dir(video_id)
        metadata = _metadata_with_ytdlp(canonical_url)

        warnings: list[str] = []
        cleanup_moved: list[str] = []
        transcript_source = ""
        transcript_text = ""
        segment_count = 0
        raw_path = ""

        try:
            api_result = _fetch_with_transcript_api(video_id, language, include_timestamps)
            transcript_source = api_result["source"]
            transcript_text = api_result["transcript_text"]
            segment_count = int(api_result["segment_count"])
        except Exception as api_exc:
            warnings.append(str(api_exc)[:400])
            dl_result = _download_with_ytdlp(canonical_url, video_id, scratch)
            transcript_source = dl_result["source"]
            raw_path = dl_result["raw_path"]
            cleanup_moved = dl_result.get("cleanup_moved", [])
            transcript_text, segment_count = _normalize_vtt(Path(raw_path), include_timestamps)
            if not keep_artifacts and raw_path:
                try:
                    Path(raw_path).unlink()
                except Exception:
                    pass

        cleaned_path = scratch / ("transcript.timestamped.txt" if include_timestamps else "transcript.cleaned.txt")
        cleaned_path.write_text(transcript_text, encoding="utf-8")

        return _json_result(
            {
                "ok": True,
                "video_id": video_id,
                "url": canonical_url,
                "title": metadata.get("title", ""),
                "channel": metadata.get("channel", ""),
                "duration": metadata.get("duration", ""),
                "transcript_source": transcript_source,
                "transcript_path": str(cleaned_path),
                "transcript_text": transcript_text,
                "segment_count": segment_count,
                "scratch_dir": str(scratch),
                "cleanup_moved": cleanup_moved,
                "warnings": [w for w in warnings if w] + ([metadata.get("warning")] if metadata.get("warning") else []),
            }
        )
    except Exception as exc:
        return _json_result({"ok": False, "error": str(exc)})
