import argparse
import json
import os
import random
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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


def request(url: str, method: str = "GET") -> bytes:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def plex_get(base: str, token: str, path: str, params: dict | None = None) -> bytes:
    q = dict(params or {})
    q["X-Plex-Token"] = token
    url = f"{base}{path}?" + urllib.parse.urlencode(q, safe=":/,")
    return request(url)


def get_sections(base: str, token: str) -> list[dict]:
    root = ET.fromstring(plex_get(base, token, "/library/sections").decode("utf-8"))
    out = []
    for d in root.findall("Directory"):
        out.append({"key": d.get("key"), "title": d.get("title"), "type": d.get("type")})
    return out


def fetch_all_tracks(base: str, token: str, section_id: str) -> list[dict]:
    start = 0
    size = 200
    tracks: list[dict] = []
    while True:
        root = ET.fromstring(
            plex_get(
                base,
                token,
                f"/library/sections/{section_id}/all",
                {"type": 10, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size},
            ).decode("utf-8")
        )
        items = list(root.findall("Track"))
        if not items:
            break
        for t in items:
            tracks.append(
                {
                    "title": t.get("title") or "",
                    "artist": t.get("grandparentTitle") or "",
                    "album": t.get("parentTitle") or "",
                    "year": t.get("year") or "",
                    "rating": t.get("userRating") or "",
                    "duration_ms": t.get("duration") or "",
                    "genres": [x.get("tag") for x in t.findall("Genre") if x.get("tag")],
                    "moods": [x.get("tag") for x in t.findall("Mood") if x.get("tag")],
                    "styles": [x.get("tag") for x in t.findall("Style") if x.get("tag")],
                    "key": t.get("ratingKey") or "",
                }
            )
        start += len(items)
        if len(items) < size:
            break
    return tracks


def to_float(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def to_int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def build_keyword_profile(cues: str) -> tuple[set[str], set[str]]:
    cue_tokens = tokenize(cues)

    positive = {
        "chill",
        "calm",
        "focus",
        "coding",
        "study",
        "ambient",
        "lofi",
        "downtempo",
        "instrumental",
        "mellow",
        "jazz",
        "classical",
        "electronic",
        "post",
        "rock",
        "trip",
        "hop",
        "indie",
    }
    negative = {"metal", "death", "hardcore", "thrash", "screamo", "punk", "rage", "loud", "aggressive"}

    expansions = {
        "coding": {"focus", "instrumental", "ambient", "lofi"},
        "code": {"focus", "instrumental", "ambient", "lofi"},
        "study": {"focus", "instrumental", "mellow"},
        "sleep": {"calm", "ambient", "mellow", "instrumental"},
        "chill": {"calm", "mellow", "downtempo", "ambient"},
        "energy": {"electronic", "indie"},
        "workout": {"electronic", "rock"},
    }

    for tok in cue_tokens:
        positive.add(tok)
        if tok in expansions:
            positive.update(expansions[tok])

    if "chill" in cue_tokens or "coding" in cue_tokens or "focus" in cue_tokens:
        negative.update({"trap metal", "black metal", "grindcore"})

    return positive, negative


def track_blob(track: dict) -> str:
    parts = [
        track.get("title") or "",
        track.get("artist") or "",
        track.get("album") or "",
        " ".join(track.get("genres") or []),
        " ".join(track.get("moods") or []),
        " ".join(track.get("styles") or []),
    ]
    return " ".join(parts).lower()


def score_track(track: dict, positive: set[str], negative: set[str], rng: random.Random) -> float:
    blob = track_blob(track)
    score = 0.0

    for kw in positive:
        if kw and kw in blob:
            score += 2.0

    for kw in negative:
        if kw and kw in blob:
            score -= 2.5

    if "instrumental" in blob:
        score += 1.5
    if "ambient" in blob or "lofi" in blob or "downtempo" in blob:
        score += 1.0

    rating = to_float(track.get("rating") or "", 0.0)
    if rating > 0:
        score += min(rating / 2.0, 2.5)

    duration_ms = to_int(track.get("duration_ms") or "", 0)
    if 90_000 <= duration_ms <= 600_000:
        score += 0.3

    score += rng.uniform(-0.2, 0.2)
    return score


def pick_tracks(scored: list[tuple[float, dict]], limit: int, max_per_artist: int) -> list[dict]:
    picks: list[dict] = []
    artist_counts: dict[str, int] = {}
    seen_keys = set()

    for score, t in scored:
        key = t.get("key")
        if not key or key in seen_keys:
            continue
        artist = (t.get("artist") or "unknown").strip().lower()
        count = artist_counts.get(artist, 0)
        if count >= max_per_artist:
            continue
        seen_keys.add(key)
        artist_counts[artist] = count + 1
        tt = dict(t)
        tt["score"] = round(score, 3)
        picks.append(tt)
        if len(picks) >= limit:
            break

    return picks


def delete_playlist_if_exists(base: str, token: str, name: str) -> None:
    root = ET.fromstring(plex_get(base, token, "/playlists").decode("utf-8"))
    for pl in root.findall("Playlist"):
        if (pl.get("title") or "") == name:
            rk = pl.get("ratingKey")
            if rk:
                request(f"{base}/playlists/{rk}?X-Plex-Token={token}", method="DELETE")


def create_audio_playlist(base: str, token: str, machine_id: str, name: str, keys: list[str]) -> None:
    uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/" + ",".join(keys)
    params = {"type": "audio", "title": name, "smart": "0", "uri": uri, "X-Plex-Token": token}
    url = f"{base}/playlists?" + urllib.parse.urlencode(params, safe=":/,", quote_via=urllib.parse.quote)
    request(url, method="POST")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    load_env_file(repo_root / ".env")

    ap = argparse.ArgumentParser(description="Create a Plex music playlist from free-form style cues.")
    ap.add_argument("--name", required=True, help="Playlist title")
    ap.add_argument("--cues", default="chill coding focus instrumental", help="Free-form style cues")
    ap.add_argument("--limit", type=int, default=45, help="Max tracks in playlist")
    ap.add_argument("--max-per-artist", type=int, default=3, help="Artist cap for diversity")
    ap.add_argument("--section-id", help="Music library section id (optional)")
    ap.add_argument("--seed", type=int, default=313, help="Random seed for tie breaks")
    ap.add_argument("--dry-run", action="store_true", help="Score and print picks without writing playlist")
    args = ap.parse_args()

    base = get_env("PLEX_BASE_URL")
    token = get_env("PLEX_SERVER_TOKEN")
    machine = get_env("PLEX_MACHINE_ID")

    sections = get_sections(base, token)
    music_sections = [s for s in sections if s.get("type") == "artist"]
    if not music_sections and not args.section_id:
        raise SystemExit("No music sections found.")

    section = next((s for s in music_sections if s.get("key") == args.section_id), None)
    if args.section_id and section is None:
        section = {"key": args.section_id, "title": "manual", "type": "artist"}
    if section is None:
        section = music_sections[0]

    tracks = fetch_all_tracks(base, token, section["key"])
    if not tracks:
        raise SystemExit("No tracks found in selected music section.")

    positive, negative = build_keyword_profile(args.cues)
    rng = random.Random(args.seed)

    scored = [(score_track(t, positive, negative, rng), t) for t in tracks if t.get("key")]
    scored.sort(key=lambda x: x[0], reverse=True)
    picks = pick_tracks(scored, limit=max(1, args.limit), max_per_artist=max(1, args.max_per_artist))

    keys = [t["key"] for t in picks]
    if not args.dry_run:
        delete_playlist_if_exists(base, token, args.name)
        create_audio_playlist(base, token, machine, args.name, keys)

    print(
        json.dumps(
            {
                "section": section,
                "tracks_total": len(tracks),
                "playlist_name": args.name,
                "cues": args.cues,
                "playlist_count": len(keys),
                "dry_run": bool(args.dry_run),
                "sample_tracks": [
                    {
                        "title": t.get("title"),
                        "artist": t.get("artist"),
                        "album": t.get("album"),
                        "score": t.get("score"),
                    }
                    for t in picks[:20]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
