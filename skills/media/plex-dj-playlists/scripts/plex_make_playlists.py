import argparse
import json
import os
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


def get_optional_env(name: str) -> str | None:
    val = os.environ.get(name)
    if not val:
        return None
    return val


def request(url: str, method: str = "GET") -> bytes:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def norm(s: str | None) -> str:
    return (s or "").strip().lower()


def build_artist_index(tracks: list[dict]) -> dict[str, list[dict]]:
    by_artist: dict[str, list[dict]] = {}
    for t in tracks:
        a = norm(t.get("artist"))
        if not a:
            continue
        by_artist.setdefault(a, []).append(t)
    for a in by_artist:
        by_artist[a] = sorted(by_artist[a], key=lambda x: (x.get("year") or "9999", x.get("album") or "", x.get("track") or "0"))
    return by_artist


def make_playlist_keys(by_artist: dict[str, list[dict]], artists: list[str], max_per_artist: int, max_tracks: int) -> list[str]:
    buckets = {a: by_artist.get(a, [])[:max_per_artist] for a in artists}
    order: list[dict] = []
    while True:
        added = False
        for a in artists:
            if buckets[a]:
                order.append(buckets[a].pop(0))
                added = True
        if not added:
            break

    seen = set()
    keys: list[str] = []
    for t in order:
        k = t.get("key")
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
        if len(keys) >= max_tracks:
            break
    return keys


def delete_existing(base: str, token: str, names: list[str]) -> None:
    xml = request(f"{base}/playlists?X-Plex-Token={token}").decode("utf-8")
    root = ET.fromstring(xml)
    for pl in root.findall("Playlist"):
        title = pl.get("title")
        rk = pl.get("ratingKey")
        if title in names and rk:
            request(f"{base}/playlists/{rk}?X-Plex-Token={token}", method="DELETE")


def create_playlist(base: str, token: str, machine: str, name: str, keys: list[str]) -> int:
    if not keys:
        return 0
    uri = f"server://{machine}/com.plexapp.plugins.library/library/metadata/" + ",".join(keys)
    params = {
        "type": "audio",
        "title": name,
        "smart": "0",
        "uri": uri,
        "X-Plex-Token": token,
    }
    url = f"{base}/playlists?" + urllib.parse.urlencode(params, safe=":/,", quote_via=urllib.parse.quote)
    request(url, method="POST")
    return len(keys)


def load_playlists(path: Path, example_path: Path | None = None) -> dict[str, list[str]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        message = f"Playlist file not found: {path}"
        if example_path is not None:
            message += f". Copy {example_path} to {path} and customize it, or pass --playlists-file."
        raise SystemExit(message) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Playlist file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"Playlist file must contain a JSON object mapping playlist names to artist lists: {path}")

    playlists: dict[str, list[str]] = {}
    for name, artists in data.items():
        if not isinstance(name, str) or not name.strip():
            raise SystemExit(f"Playlist names must be non-empty strings: {path}")
        if not isinstance(artists, list) or not all(isinstance(artist, str) and artist.strip() for artist in artists):
            raise SystemExit(f"Playlist '{name}' must map to a JSON array of non-empty artist strings: {path}")
        playlists[name] = artists

    if not playlists:
        raise SystemExit(f"Playlist file is empty: {path}")

    return playlists


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    skill_root = Path(__file__).resolve().parents[1]
    load_env_file(repo_root / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", required=True, help="Path to exported track JSON")
    ap.add_argument("--max-per-artist", type=int, default=4)
    ap.add_argument("--max-tracks", type=int, default=30)
    ap.add_argument(
        "--playlists-file",
        help="Path to JSON file mapping playlist names to artist lists. "
        "Defaults to PLEX_PLAYLISTS_FILE or skills/media/plex-dj-playlists/local/playlists.json.",
    )
    args = ap.parse_args()

    base = get_env("PLEX_BASE_URL")
    token = get_env("PLEX_SERVER_TOKEN")
    machine = get_env("PLEX_MACHINE_ID")
    example_playlists_file = skill_root / "playlists.example.json"
    playlists_file = Path(
        args.playlists_file
        or get_optional_env("PLEX_PLAYLISTS_FILE")
        or (skill_root / "local" / "playlists.json")
    ).expanduser()

    with open(args.tracks, "r", encoding="utf-8", errors="replace") as f:
        tracks = json.load(f)

    by_artist = build_artist_index(tracks)
    playlists = load_playlists(playlists_file, example_playlists_file)

    delete_existing(base, token, list(playlists.keys()))

    for name, artists in playlists.items():
        keys = make_playlist_keys(by_artist, artists, args.max_per_artist, args.max_tracks)
        count = create_playlist(base, token, machine, name, keys)
        print(f"{name}|{count}")


if __name__ == "__main__":
    main()
