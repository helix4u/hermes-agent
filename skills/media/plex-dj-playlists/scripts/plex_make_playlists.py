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


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    load_env_file(repo_root / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", required=True, help="Path to exported track JSON")
    ap.add_argument("--max-per-artist", type=int, default=4)
    ap.add_argument("--max-tracks", type=int, default=30)
    args = ap.parse_args()

    base = get_env("PLEX_BASE_URL")
    token = get_env("PLEX_SERVER_TOKEN")
    machine = get_env("PLEX_MACHINE_ID")

    with open(args.tracks, "r", encoding="utf-8", errors="replace") as f:
        tracks = json.load(f)

    by_artist = build_artist_index(tracks)

    playlists = {
        "Good Mood Songs": ["gorillaz", "sublime", "beastie boys", "twenty one pilots", "polyphia", "coheed and cambria", "son lux", "tom waits", "modest mouse"],
        "Check These Gems Out": ["the mars volta", "aesop rock", "the dresden dolls", "ren", "polyphia", "coheed and cambria", "tom waits", "sigur ros", "kings of convenience", "bright eyes", "conor oberst", "dashboard confessional", "queens of the stone age", "deftones", "portishead", "massive attack", "modest mouse"],
        "Melancholy Songs": ["sigur ros", "kings of convenience", "son lux", "tom waits", "ren", "bright eyes", "conor oberst", "dashboard confessional", "portishead", "massive attack", "modest mouse"],
        "Songs to Sleep To": ["sigur ros", "kings of convenience", "son lux", "tom waits", "bright eyes", "conor oberst", "portishead"],
        "Songs to Wake You Up": ["the prodigy", "rage against the machine", "twenty one pilots", "polyphia", "aesop rock", "coheed and cambria", "queens of the stone age", "deftones"],
        "Songs to Rage Out To": ["rage against the machine", "the prodigy", "aesop rock", "ren", "the mars volta", "coheed and cambria", "queens of the stone age", "deftones"],
        "Songs for Driving Fast": ["the prodigy", "polyphia", "rage against the machine", "coheed and cambria", "gorillaz", "twenty one pilots", "queens of the stone age", "deftones"],
        "Calm Sunday Drives": ["kings of convenience", "sigur ros", "sublime", "tom waits", "son lux", "bright eyes", "conor oberst", "dashboard confessional", "modest mouse"],
    }

    delete_existing(base, token, list(playlists.keys()))

    for name, artists in playlists.items():
        keys = make_playlist_keys(by_artist, artists, args.max_per_artist, args.max_tracks)
        count = create_playlist(base, token, machine, name, keys)
        print(f"{name}|{count}")


if __name__ == "__main__":
    main()
