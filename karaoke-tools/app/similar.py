"""Suggest Similar sessions for the app.

Opening a seed ranks the catalog at once without a mix (suggest.rank with
use_mix=False), then fetches the seed's YouTube Mix in the background and
re-ranks with it (the same suggest.rank the chat uses). Mix results are cached
per seed for 24 h (cache.mix), so re-opening a seed is instant. Karaoke tracks
for outside songs are looked up per page (cache.karaoke_many), and the next
page is prefetched.

Live state (likes, ratings, status) is read fresh on every page, so a page
always shows what is saved, even when the ranking itself is cached.
"""

import copy
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

from rapidfuzz import fuzz

import cache
import catalog
import feedback
import suggest
import youtube
from common import log

PAGE_SIZE = 10
PIN_ALT_MIN = 85   # catalog_get score for a "Not this one?" alternative
PIN_ALT_MAX = 3
SESSION_TTL = 24 * 3600
MAX_SESSIONS = 40

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="similar")
_sessions: dict[str, "Session"] = {}
_sessions_lock = threading.Lock()
# yt:<id> -> song_id for outside songs added from the app (set by jobs.py).
added_keys: dict[str, str] = {}


class Session:
    def __init__(self, sid: str, seed: str, url: str):
        self.sid, self.seed, self.url = sid, seed, url
        self.created = time.time()
        self.ref: dict = {}
        self.seed_key = ""
        self.seed_title = seed or url
        self.prelim: list[dict] = []
        self.catalog: list[dict] | None = None
        self.outside: list[dict] | None = None
        self.mix_status = "loading"
        self.warnings: list[str] = []
        self.lock = threading.Lock()  # karaoke fill for this session's lists
        self.done = threading.Event()
        self.pin_karaoke = ""          # the exact track a ↪ Similar row was playing
        self.pinned: dict | None = None  # 🎯 the seed itself, shown first on page 1
        self.alternatives: list[dict] = []
        self.mix_seed: dict = {}         # the mix's seed video (the original song)


def session_id(seed: str, url: str) -> str:
    return hashlib.sha1(f"{(seed or '').strip().lower()}|{(url or '').strip()}".encode()).hexdigest()[:12]


def _rank(s: Session, mix: dict | None, use_mix: bool) -> dict:
    W, _ = suggest.load_weights()
    ref = copy.deepcopy(s.ref)
    ranked = suggest.rank("like", [ref], [s.url] if s.url else [], "", [],
                          feedback.load_relations(), W,
                          mixes=[mix] if mix is not None else None, use_mix=use_mix)
    ranked["ref"] = ref
    return ranked


def _finish(s: Session, mix: dict | None = None, fill: bool = True) -> None:
    """Fetch the mix (unless given) and re-rank with it; fill: look up the
    first page's karaoke tracks too. Never raises."""
    try:
        if mix is None:
            mix = suggest.fetch_mix(s.ref, s.url)
        ranked = _rank(s, mix, use_mix=True)
        ref = ranked["ref"]
        if mix and mix.get("success"):
            s.mix_seed = mix.get("seed") or {}
        with s.lock:
            s.seed_key = s.seed_key or (ranked["ref_keys"] or [""])[0]
            s.seed_title = ref["title"] or s.seed_title
            s.catalog, s.outside = ranked["catalog"], ranked["outside"]
            s.warnings = ranked["warnings"]
            s.mix_status = "error" if ranked["warnings"] else "done"
        if s.pinned is None:
            s.pinned = _pin_outside(s)
        if fill:
            fill_page(s, 0)
    except Exception as e:  # the catalog section still works without the mix
        log(f"[similar] mix for {s.seed_title!r} failed: {e!r}")
        with s.lock:
            s.catalog = s.catalog if s.catalog is not None else s.prelim
            s.outside = s.outside or []
            s.mix_status = "error"
            s.warnings = [f"YouTube Mix unavailable: {e}"]
        if s.pinned is None:
            try:
                s.pinned = _pin_outside(s)
            except Exception as e2:  # never block the page on the pinned row
                log(f"[similar] pinned row for {s.seed_title!r} failed: {e2!r}")
    finally:
        s.done.set()


def _url_row(url: str) -> dict | None:
    """The catalog song whose karaoke link (or an alternate) is this video."""
    vid = youtube.video_id_from_url(url)
    if not vid:
        return None
    for r in catalog.load_songs():
        links = [r["karaoke_url"]] + [u for u in r["karaoke_alt_urls"].split(";") if u]
        if any(youtube.video_id_from_url(u) == vid for u in links if u):
            return r
    return None


def _alternatives(seed: str, chosen_id: str) -> list[dict]:
    """Other catalog songs the typed title matches closely ("Not this one?").
    None for an exact song_id (from a row's ↪ Similar)."""
    if not seed or catalog.SONG_ID_RE.match(seed) and catalog.catalog_get(seed).get("found"):
        return []
    g = catalog.catalog_get(seed)
    # catalog_get scores title + film loosely; an alternative must be close on the
    # title itself ("Mere Sapnon Ki Rani" in another film, an unplugged version).
    want = suggest._norm(seed)
    alts = [m for m in g.get("matches") or [] if m["score"] >= PIN_ALT_MIN and m["song_id"] != chosen_id
            and fuzz.token_sort_ratio(want, suggest._norm(m["title"])) >= PIN_ALT_MIN]
    return [{k: m[k] for k in ("song_id", "title", "film", "year")} for m in alts[:PIN_ALT_MAX]]


def _search_url(title: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(f"{title} karaoke")


def _pin_catalog(s: Session) -> dict:
    row = s.ref["row"]
    return {"group": "catalog", "song_key": row["song_id"], "title": row["title"], "film": row["film"],
            "karaoke_url": row["karaoke_url"] or s.pin_karaoke,
            "video_url": s.url if s.url and youtube.video_id_from_url(s.url) else "",
            "karaoke": "found" if row["karaoke_url"] or s.pin_karaoke else "none",
            "search_url": _search_url(row["title"]), "reasons": "🎯 your song"}


def _pin_outside(s: Session) -> dict:
    """The seed when it isn't in the catalog: its karaoke track found the usual
    way (channel index, then search), or the exact link it was opened with."""
    seed_vid = s.mix_seed.get("video_id", "")
    title = s.seed if s.seed and not s.url else (s.seed_title or s.seed)
    original = s.url or (youtube.watch_url(seed_vid) if seed_vid else "")
    if s.pin_karaoke:
        track = {"url": s.pin_karaoke, "title": "", "channel": ""}
    elif s.url and not s.seed:
        track = {"url": s.url, "title": "", "channel": ""}  # a pasted link: play that exact video
    else:
        track = cache.karaoke_many([(title, "")])[0] if title else None
    key = s.seed_key or (f"yt:{seed_vid}" if seed_vid else "")
    if not key and track and youtube.video_id_from_url(track["url"]):
        key = "yt:" + youtube.video_id_from_url(track["url"])
    return {"group": "outside", "song_key": key, "title": title, "film": "",
            "karaoke_url": track["url"] if track else "", "karaoke_title": track["title"] if track else "",
            "video_url": original, "karaoke": "found" if track else "none",
            "search_url": _search_url(title), "reasons": "🎯 your song"}


def pinned_out(s: Session) -> dict | None:
    """The 🎯 row with live rating/status (it may have been rated or added)."""
    if not s.pinned:
        return None
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    out = _live(s.pinned, by_id, [], "")
    out.pop("liked", None)
    out.update({"pinned": True, "search_url": s.pinned["search_url"], "alternatives": s.alternatives})
    return out


def open_session(seed: str, url: str = "", karaoke: str = "") -> Session:
    """The session for a seed: reused while fresh, else ranked anew."""
    sid = session_id(seed, url)
    with _sessions_lock:
        s = _sessions.get(sid)
        if s and time.time() - s.created < SESSION_TTL and s.mix_status != "error":
            return s
        s = Session(sid, seed, url)
        _sessions[sid] = s
        for old in sorted(_sessions.values(), key=lambda x: x.created)[:-MAX_SESSIONS]:
            _sessions.pop(old.sid, None)
    s.pin_karaoke = karaoke
    s.ref = suggest._resolve(seed) if seed else {"key": "", "title": url, "row": None}
    if not s.ref["row"] and url:  # a pasted link that is a catalog song's karaoke track
        row = _url_row(url)
        if row:
            s.ref = {"key": row["song_id"], "title": row["title"], "row": row}
    s.seed_key = s.ref["key"]
    s.seed_title = s.ref["title"] or seed or url
    s.alternatives = _alternatives(seed, s.ref["key"]) if seed and not url else []
    if s.ref["row"]:
        s.pinned = _pin_catalog(s)
    cached = suggest.fetch_mix(s.ref, url, cached_only=True)
    if cached is not None:
        s.prelim = []
        _finish(s, cached, fill=False)
    else:
        s.prelim = _rank(s, None, use_mix=False)["catalog"]
        _pool.submit(_finish, s)
    return s


def get_session(sid: str) -> Session | None:
    with _sessions_lock:
        return _sessions.get(sid)


def fill_page(s: Session, page: int) -> None:
    """Karaoke tracks for one page of outside songs (cached after)."""
    with s.lock:
        items = (s.outside or [])[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        todo = [it for it in items if "karaoke" not in it]
        if todo:
            suggest._fill_karaoke(todo)


def prefetch(s: Session, page: int) -> None:
    if s.outside and page * PAGE_SIZE < len(s.outside):
        _pool.submit(fill_page, s, page)


# --- page output ------------------------------------------------------------------------

FIELDS = ("group", "song_key", "title", "film", "year", "singers", "status", "energy",
          "tempo_class", "my_rating", "my_key_change", "karaoke_url", "video_url", "reasons")


def _live(it: dict, by_id: dict, rel_rows: list[dict], seed_key: str) -> dict:
    out = {k: it.get(k, "") for k in FIELDS}
    out["karaoke"] = it.get("karaoke", "found" if it.get("karaoke_url") else "")
    out["karaoke_title"] = it.get("karaoke_title", "")
    key = it["song_key"]
    song_id = key if key in by_id else added_keys.get(key, "")
    row = by_id.get(song_id)
    if row:  # current values, also for an outside song added in this session
        out.update({k: row[k] for k in ("title", "film", "year", "singers", "status", "energy",
                                        "tempo_class", "my_rating", "my_key_change")})
        out["karaoke_url"] = row["karaoke_url"] or out["karaoke_url"]
        out["catalog_id"] = song_id
    else:
        out["catalog_id"] = ""
    out["liked"] = bool(seed_key) and (feedback.is_liked(seed_key, key, rel_rows) or
                                       bool(song_id) and feedback.is_liked(seed_key, song_id, rel_rows))
    return out


def seed_info(s: Session) -> dict:
    row = s.ref.get("row")
    return {"key": s.seed_key, "title": s.seed_title, "in_catalog": bool(row),
            "film": row["film"] if row else "", "year": row["year"] if row else ""}


def catalog_page(s: Session, page: int) -> dict:
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    rel = feedback.load_relations()
    source = s.catalog if s.catalog is not None else s.prelim
    chunk = source[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    return {"items": [_live(it, by_id, rel, s.seed_key) for it in chunk],
            "end": (page + 1) * PAGE_SIZE >= len(source),
            "final": s.catalog is not None}


def outside_page(s: Session, page: int) -> dict:
    fill_page(s, page)
    prefetch(s, page + 1)
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    rel = feedback.load_relations()
    src = s.outside or []
    chunk = src[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    return {"items": [_live(it, by_id, rel, s.seed_key) for it in chunk],
            "end": (page + 1) * PAGE_SIZE >= len(src)}
