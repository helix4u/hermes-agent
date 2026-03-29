import argparse
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def get_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing env var {name}")
    return val


def request(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def detect_music_section(base: str, token: str) -> str | None:
    xml = request(f"{base}/library/sections?X-Plex-Token={token}").decode("utf-8")
    root = ET.fromstring(xml)
    for d in root.findall("Directory"):
        if d.get("type") == "artist":
            return d.get("key")
    return None


def export_tracks(base: str, token: str, section_id: str) -> list[dict]:
    url = f"{base}/library/sections/{section_id}/all?type=10&X-Plex-Token={token}"
    xml = request(url)
    root = ET.fromstring(xml)
    tracks: list[dict] = []
    for t in root.iter("Track"):
        media = t.find("Media")
        part = media.find("Part") if media is not None else None
        tracks.append(
            {
                "title": t.get("title"),
                "artist": t.get("grandparentTitle"),
                "album": t.get("parentTitle"),
                "album_artist": t.get("originalTitle"),
                "year": t.get("year"),
                "rating": t.get("userRating"),
                "duration_ms": t.get("duration"),
                "track": t.get("index"),
                "disc": t.get("parentIndex"),
                "genre": [g.get("tag") for g in t.findall("Genre")],
                "mood": [m.get("tag") for m in t.findall("Mood")],
                "style": [s.get("tag") for s in t.findall("Style")],
                "key": t.get("ratingKey"),
                "guid": t.get("guid"),
                "file": part.get("file") if part is not None else None,
            }
        )
    return tracks


def default_out_path() -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    out_dir = repo_root / "TMP" / "plex-dj-playlists"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return out_dir / f"plex_tracks_{stamp}.json"


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    load_env_file(repo_root / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="Path to write JSON export")
    ap.add_argument("--section-id", help="Plex music library section id")
    args = ap.parse_args()

    base = get_env("PLEX_BASE_URL")
    token = get_env("PLEX_SERVER_TOKEN")

    section_id = args.section_id or detect_music_section(base, token)
    if not section_id:
        raise SystemExit("Could not detect music section. Provide --section-id.")

    tracks = export_tracks(base, token, section_id)
    out_path = Path(args.out) if args.out else default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", errors="replace", newline="") as f:
        json.dump(tracks, f, indent=2)

    print(str(out_path))
    print(len(tracks))


if __name__ == "__main__":
    main()
