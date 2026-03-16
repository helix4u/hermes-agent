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


def get_all_movies(base: str, token: str, section_id: str) -> list[dict]:
    start = 0
    size = 200
    movies: list[dict] = []
    while True:
        root = ET.fromstring(
            plex_get(
                base,
                token,
                f"/library/sections/{section_id}/all",
                {"type": 1, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size},
            ).decode("utf-8")
        )
        items = list(root.findall("Video"))
        if not items:
            break
        for v in items:
            movies.append(
                {
                    "title": v.get("title"),
                    "year": v.get("year"),
                    "summary": v.get("summary"),
                    "rating": v.get("rating"),
                    "userRating": v.get("userRating"),
                    "contentRating": v.get("contentRating"),
                    "duration": v.get("duration"),
                    "studio": v.get("studio"),
                    "genres": [g.get("tag") for g in v.findall("Genre") if g.get("tag")],
                    "countries": [c.get("tag") for c in v.findall("Country") if c.get("tag")],
                    "directors": [d.get("tag") for d in v.findall("Director") if d.get("tag")],
                    "key": v.get("ratingKey"),
                }
            )
        start += len(items)
        if len(items) < size:
            break
    return movies


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def score_movie(m: dict) -> float:
    genres = set(g.lower() for g in (m.get("genres") or []))
    title = (m.get("title") or "").lower()
    year = safe_int(m.get("year"), 0)
    score = 0.0

    chill_boost = {
        "drama": 2.0,
        "romance": 2.2,
        "comedy": 1.6,
        "adventure": 1.2,
        "fantasy": 1.1,
        "science fiction": 1.1,
        "animation": 1.0,
        "indie": 1.0,
        "family": 0.8,
    }
    for g, w in chill_boost.items():
        if g in genres:
            score += w

    if "action" in genres:
        score -= 0.8
    if "horror" in genres:
        score -= 1.2
    if "thriller" in genres:
        score -= 0.4

    if 1985 <= year <= 2018:
        score += 0.8

    score += safe_float(m.get("rating"), 0.0) * 0.35
    score += safe_float(m.get("userRating"), 0.0) * 0.45

    vibe_titles = [
        "lost in translation", "eternal sunshine", "her", "amelie", "grand budapest", "walter mitty",
        "princess bride", "shoplifters", "garden state", "into the wild", "big fish", "wall-e", "fight club",
        "ocean", "harry potter",
    ]
    if any(k in title for k in vibe_titles):
        score += 3.0

    return score


def choose_movies(movies: list[dict], n: int = 36) -> list[dict]:
    scored = []
    for m in movies:
        if m.get("key"):
            scored.append((score_movie(m), m))
    scored.sort(key=lambda x: (x[0], safe_float(x[1].get("rating"), 0.0)), reverse=True)

    picked = []
    seen = set()
    for s, m in scored:
        t = (m.get("title") or "").strip().lower()
        if not t or t in seen:
            continue
        seen.add(t)
        mm = dict(m)
        mm["score"] = round(s, 3)
        picked.append(mm)
        if len(picked) >= n:
            break
    return picked


def delete_playlist_if_exists(base: str, token: str, name: str) -> None:
    root = ET.fromstring(plex_get(base, token, "/playlists").decode("utf-8"))
    for pl in root.findall("Playlist"):
        if pl.get("title") == name:
            rk = pl.get("ratingKey")
            request(f"{base}/playlists/{rk}?X-Plex-Token={token}", method="DELETE")


def create_video_playlist(base: str, token: str, machine_id: str, name: str, keys: list[str]) -> None:
    uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/" + ",".join(keys)
    params = {"type": "video", "title": name, "smart": "0", "uri": uri, "X-Plex-Token": token}
    url = f"{base}/playlists?" + urllib.parse.urlencode(params, safe=":/,", quote_via=urllib.parse.quote)
    request(url, method="POST")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    load_env_file(repo_root / ".env")

    base = get_env("PLEX_BASE_URL")
    token = get_env("PLEX_SERVER_TOKEN")
    machine = get_env("PLEX_MACHINE_ID")

    sections = get_sections(base, token)
    movie_sections = [s for s in sections if s.get("type") == "movie"]
    if not movie_sections:
        raise SystemExit("No movie sections found.")

    section = movie_sections[0]
    movies = get_all_movies(base, token, section["key"])

    out_dir = repo_root / "TMP" / "plex-dj-playlists"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    export_path = out_dir / f"plex_movies_{stamp}.json"
    with open(export_path, "w", encoding="utf-8", errors="replace", newline="") as f:
        json.dump(movies, f, indent=2)

    picked = choose_movies(movies, n=36)
    keys = [m["key"] for m in picked]

    playlist_name = f"Sam Picks - Movie Night ({stamp})"
    delete_playlist_if_exists(base, token, playlist_name)
    create_video_playlist(base, token, machine, playlist_name, keys)

    print(
        json.dumps(
            {
                "section": section,
                "movies_total": len(movies),
                "export_path": str(export_path),
                "playlist_name": playlist_name,
                "playlist_count": len(keys),
                "sample_titles": [m.get("title") for m in picked[:20]],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
