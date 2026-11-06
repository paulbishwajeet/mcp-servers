"""Caches in STATE_ROOT/cache (outside the wiki), shared by the chat and the app.

- YouTube Mix results per seed, 24 hours: Next, Back and re-opening are instant.
- Karaoke-track lookups per normalised song title, 30 days: only new songs cost
  a search. A "no karaoke track" answer is cached too.

Writes go through the shared lock (locks.py), like every other state write.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import catalog
import locks
import youtube
from common import log, state_root

MIX_TTL = 24 * 3600
KARAOKE_TTL = 30 * 24 * 3600
KARAOKE_WORKERS = 4
KARAOKE_MIN_SCORE = 85


def _dir(name: str):
    d = state_root() / "cache" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path, data: dict) -> None:
    with locks.LOCK:
        catalog.atomic_write(path, json.dumps(data, ensure_ascii=False))


# --- YouTube Mix --------------------------------------------------------------------------

def mix(song: str = "", url: str = "", seed: str = "original", source: str = "both",
        limit: int = 40, film: str = "", cached_only: bool = False) -> dict | None:
    """youtube_mix, cached per seed for 24 h (only successful results).
    cached_only: return the cached result or None, never fetch."""
    key = hashlib.sha1(json.dumps([song.lower().strip(), url.strip(), seed, source, limit,
                                   film.lower().strip()]).encode()).hexdigest()[:16]
    path = _dir("mix") / f"{key}.json"
    hit = _read(path)
    if hit and time.time() - hit.get("at", 0) < MIX_TTL:
        return hit["result"] | {"cached": True}
    if cached_only:
        return None
    result = youtube.youtube_mix(song=song, url=url, seed=seed, source=source, limit=limit, film=film)
    if result.get("success"):
        _write(path, {"at": time.time(), "result": result})
    return result


# --- karaoke tracks ---------------------------------------------------------------------------

def _karaoke_path():
    return _dir("karaoke") / "karaoke.json"


def _lookup(title: str, film: str) -> dict | None:
    """karaoke_find: the channel index first (instant), then a YouTube search.
    Accept the top candidate that is clearly a karaoke track of this song."""
    r = youtube.karaoke_find(title, film)
    for c in (r.get("candidates") or []) if r.get("success") else []:
        if c["score"] >= KARAOKE_MIN_SCORE and (c["source"] == "index" or "karaoke" in c["title"].lower()):
            return {"url": c["url"], "title": c["title"], "channel": c["channel"], "source": c["source"]}
    return None


def karaoke_many(songs: list[tuple[str, str]]) -> list[dict | None]:
    """Karaoke tracks for (title, film) pairs, in order. Cached ones are instant;
    the rest are looked up KARAOKE_WORKERS at a time. None = no karaoke track."""
    path = _karaoke_path()
    store = _read(path) or {}
    now = time.time()
    keys = [youtube.normalize_title(t) for t, _ in songs]
    results: dict[str, dict | None] = {}
    todo = {}
    for k, (title, film) in zip(keys, songs):
        hit = store.get(k)
        if hit and now - hit.get("at", 0) < KARAOKE_TTL:
            results[k] = hit.get("track")
        elif k and k not in todo:
            todo[k] = (title, film)
    if todo:
        def work(item):
            k, (title, film) = item
            try:
                return k, _lookup(title, film)
            except Exception as e:  # one failed lookup must not break the page
                log(f"[karaoke] {title}: {e!r}")
                return k, "error"
        with ThreadPoolExecutor(max_workers=KARAOKE_WORKERS) as ex:
            found = list(ex.map(work, todo.items()))
        with locks.LOCK:
            store = _read(path) or {}
            for k, track in found:
                if track == "error":  # don't cache failures: try again next time
                    results[k] = None
                    continue
                results[k] = track
                store[k] = {"at": now, "track": track}
            catalog.atomic_write(path, json.dumps(store, ensure_ascii=False))
    return [results.get(k) for k in keys]
