"""KaraokeGayak Mini App server: FastAPI on 127.0.0.1:8765 behind Tailscale Funnel.

Every /api/* request must carry Telegram's signed initData in the
X-Telegram-Init-Data header (auth.py); anything else gets 401, so opening the
URL in a normal browser shows the page but no data. The API offers app actions
only (read the catalog, suggest, like, rate, edit via catalog_upsert, add a
song): no shell, no file paths, no URL fetching beyond allowlisted YouTube.
The static front end in app/static is public; it holds no data.
"""

import json
import re
import threading
import time
from datetime import date
from pathlib import Path

import requests
from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ROOT  # noqa: F401  (puts karaoke-tools on sys.path)
import appstart  # noqa: E402
import catalog  # noqa: E402
import feedback  # noqa: E402
import locks  # noqa: E402
import tg  # noqa: E402
import vocab  # noqa: E402
import youtube  # noqa: E402
from common import is_youtube_url, state_root, telegram_chat_id, telegram_token  # noqa: E402

from . import auth, jobs, similar  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
YT_KEY_RE = re.compile(r"^yt:[A-Za-z0-9_-]{11}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
MAX_RECENT = 10
MIX_WAIT_SEC = 20
OEMBED = "https://www.youtube.com/oembed"

# Columns the edit sheet may change (§4.6). Everything else (song_id, added_on,
# lyrics_file, counters...) stays with the agent and the pipeline.
EDITABLE = ["title", "film", "year", "singers", "music_director", "lyricist", "voice_type",
            "genre", "mood", "theme", "situation", "tempo_class", "energy", "status",
            "my_rating", "my_key_change", "notes", "karaoke_url", "karaoke_alt_urls"]


# --- auth --------------------------------------------------------------------------------

def require_user(x_telegram_init_data: str = Header(default="")) -> dict:
    try:
        return auth.validate(x_telegram_init_data, telegram_token(), telegram_chat_id())
    except auth.AuthError:
        raise HTTPException(status_code=401, detail="unauthorized") from None


api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


# --- per-open activity, for the one-line summary on close ---------------------------------

_activity: dict[str, dict] = {}
_activity_lock = threading.Lock()


def _act(session: str) -> dict | None:
    if not SESSION_RE.match(session or ""):
        return None
    with _activity_lock:
        return _activity.setdefault(session, {"likes": {}, "ratings": {}, "added": set(),
                                              "edited": set(), "seed": ""})


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(a: dict) -> str:
    parts = []
    likes = sum(1 for v in a["likes"].values() if v)
    if likes:
        parts.append(_plural(likes, "like"))
    if a["ratings"]:
        parts.append(_plural(len(a["ratings"]), "rating"))
    if a["added"]:
        parts.append(_plural(len(a["added"]), "song") + " added")
    if a["edited"] - a["added"]:
        parts.append(_plural(len(a["edited"] - a["added"]), "song") + " edited")
    if not parts:
        return ""
    line = "Saved " + ", ".join(parts)
    return line + (f" (similar to {a['seed']})" if a["seed"] else "")


# --- helpers -------------------------------------------------------------------------------

def _key_ok(key: str) -> bool:
    return bool(catalog.SONG_ID_RE.match(key or "") or YT_KEY_RE.match(key or ""))


def _song(key: str) -> dict | None:
    """The catalog row for a song_id, or for an outside key added in this run."""
    key = similar.added_keys.get(key, key)
    if not catalog.SONG_ID_RE.match(key or ""):
        return None
    g = catalog.catalog_get(key)
    return g["row"] if g.get("found") else None


def _field_errors(errors: list[str]) -> dict:
    out = {}
    for e in errors or []:
        col, _, msg = e.partition(":")
        out.setdefault(col.strip() if col.strip() in vocab.SONG_COLUMNS else "_", msg.strip() or e)
    return out


def _clean_fields(fields) -> tuple[dict, dict]:
    """Only editable columns; YouTube-only links. Returns (fields, errors)."""
    if not isinstance(fields, dict):
        return {}, {"_": "fields must be an object."}
    errors = {k: "not editable here." for k in fields if k not in EDITABLE}
    clean = {k: v for k, v in fields.items() if k in EDITABLE}
    url = str(clean.get("karaoke_url") or "").strip()
    if url and not is_youtube_url(url):
        errors["karaoke_url"] = "must be a YouTube link."
    alts = clean.get("karaoke_alt_urls") or []
    alts = alts if isinstance(alts, list) else re.split(r"[;\s]+", str(alts))
    if any(a and not is_youtube_url(a) for a in alts):
        errors["karaoke_alt_urls"] = "must be YouTube links."
    return clean, errors


def _fail(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": message} | extra)


def _recent_path() -> Path:
    return state_root() / "app_recent.json"


def recent() -> list[dict]:
    try:
        return json.loads(_recent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _remember(seed: str, url: str, title: str) -> None:
    with locks.LOCK:
        items = [r for r in recent() if (r["seed"], r["url"]) != (seed, url)]
        items.insert(0, {"seed": seed, "url": url, "title": title, "at": int(time.time())})
        _recent_path().parent.mkdir(parents=True, exist_ok=True)
        catalog.atomic_write(_recent_path(), json.dumps(items[:MAX_RECENT], ensure_ascii=False))


# --- start + home ----------------------------------------------------------------------------

@api.get("/start/{code}")
def start(code: str):
    hit = appstart.get_code(code)
    if not hit:
        raise HTTPException(status_code=404, detail="This link has expired. Open the app from the menu.")
    return {"success": True} | hit


@api.get("/home")
def home():
    return {"success": True, "recent": recent()}


@api.get("/vocab")
def get_vocab():
    return {"success": True, "enums": vocab.ENUM_FIELDS, "multi": vocab.MULTI_ENUM_FIELDS,
            "editable": EDITABLE, "ints": {k: list(v) for k, v in vocab.INT_FIELDS.items()}}


@api.get("/search")
def search(q: str = Query("", max_length=200)):
    q = q.strip()
    if is_youtube_url(q):
        return {"success": True, "url": q, "rows": []}
    if not q:
        return {"success": True, "rows": []}
    r = catalog.catalog_search(text=q, limit=20)
    return {"success": True, "rows": r.get("rows", [])}


# --- Suggest Similar ---------------------------------------------------------------------------

def _seed_args(seed: str, url: str) -> tuple[str, str]:
    seed, url = seed.strip(), url.strip()
    if url and not is_youtube_url(url):
        raise HTTPException(status_code=400, detail="url must be a YouTube link.")
    if not seed and not url:
        raise HTTPException(status_code=400, detail="Give a seed song or a YouTube url.")
    return seed, url


def _similar_out(s: similar.Session, page: int, with_mix: bool) -> dict:
    out = {"success": True, "session": s.sid, "page": page, "seed": similar.seed_info(s),
           "catalog": similar.catalog_page(s, page), "mix": s.mix_status, "warnings": s.warnings}
    if with_mix:
        out["outside"] = similar.outside_page(s, page)
    return out


@api.get("/similar")
def get_similar(seed: str = Query("", max_length=200), url: str = Query("", max_length=300),
                page: int = Query(0, ge=0, le=50), x_app_session: str = Header(default="")):
    seed, url = _seed_args(seed, url)
    s = similar.open_session(seed, url)
    if page == 0:
        _remember(seed, url, s.seed_title)
    a = _act(x_app_session)
    if a is not None and not a["seed"]:
        a["seed"] = s.seed_title
    # The outside section comes with the page only when it's ready without a
    # wait; otherwise the client long-polls /api/similar/mix.
    ready = s.done.is_set() and all("karaoke" in it for it in
                                    (s.outside or [])[page * similar.PAGE_SIZE:(page + 1) * similar.PAGE_SIZE])
    return _similar_out(s, page, with_mix=ready)


@api.get("/similar/mix")
def get_similar_mix(session: str = Query(..., max_length=20), page: int = Query(0, ge=0, le=50)):
    s = similar.get_session(session)
    if not s:
        raise HTTPException(status_code=404, detail="Session expired; open the seed again.")
    if not s.done.wait(MIX_WAIT_SEC):
        return {"success": True, "session": s.sid, "mix": "loading"}
    return _similar_out(s, page, with_mix=True)


# --- like + rating --------------------------------------------------------------------------------

@api.post("/like")
def like(body: dict = Body(...), x_app_session: str = Header(default="")):
    seed_key, song_key = str(body.get("seed_key", "")), str(body.get("song_key", ""))
    if not _key_ok(seed_key) or not _key_ok(song_key):
        return _fail(400, "seed_key and song_key must be song_ids or yt:<video id>.")
    liked = bool(body.get("liked"))
    r = feedback.set_like(seed_key, similar.added_keys.get(song_key, song_key), liked,
                          str(body.get("seed_title", ""))[:200], str(body.get("song_title", ""))[:200])
    if not r["success"]:
        return _fail(400, r["error"])
    a = _act(x_app_session)
    if a is not None:
        a["likes"][(seed_key, song_key)] = liked
    return r


@api.post("/rating")
def rating(body: dict = Body(...), x_app_session: str = Header(default="")):
    key, value = str(body.get("song_key", "")), body.get("rating")
    if not _key_ok(key):
        return _fail(400, "song_key must be a song_id or yt:<video id>.")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10):
        return _fail(400, "rating must be a whole number 1-10, or null to clear.", field_errors={
            "my_rating": "1-10, or Clear."})
    row = _song(key)
    if not row:
        return {"success": True, "needs_add": True, "song_key": key,
                "adding": bool(jobs.running_for(key))}
    r = catalog.catalog_upsert({"song_id": row["song_id"], "my_rating": "" if value is None else value,
                                "rated_on": "" if value is None else date.today().isoformat()})
    if not r["success"]:
        return _fail(422, r["error"], field_errors=_field_errors(r.get("errors")))
    a = _act(x_app_session)
    if a is not None:
        a["ratings"][row["song_id"]] = value
    return {"success": True, "needs_add": False, "song_id": row["song_id"],
            "my_rating": r["row"]["my_rating"], "status": r["row"]["status"]}


# --- songs ----------------------------------------------------------------------------------------

@api.get("/song/{key}")
def get_song(key: str, title: str = Query("", max_length=200), film: str = Query("", max_length=200),
             karaoke_url: str = Query("", max_length=300)):
    if not _key_ok(key):
        return _fail(400, "Bad song key.")
    row = _song(key)
    if row:
        return {"success": True, "in_catalog": True, "row": row, "job": jobs.running_for(key)}
    if not YT_KEY_RE.match(key):
        return _fail(404, "No such song in the catalog.")
    draft = {c: "" for c in vocab.SONG_COLUMNS} | {"title": title.strip(), "film": film.strip(),
                                                   "karaoke_url": karaoke_url if is_youtube_url(karaoke_url) else ""}
    return {"success": True, "in_catalog": False, "row": draft, "job": jobs.running_for(key)}


@api.put("/song/{key}")
def put_song(key: str, body: dict = Body(...), x_app_session: str = Header(default="")):
    row = _song(key)
    if not row:
        return _fail(404, "Not in the catalog; use Add to catalog.")
    fields, errors = _clean_fields(body.get("fields"))
    if errors:
        return _fail(422, "Some fields need fixing.", field_errors=errors)
    if "my_rating" in fields and str(fields["my_rating"] or "") != row["my_rating"]:
        fields["rated_on"] = date.today().isoformat() if fields["my_rating"] not in ("", None) else ""
    r = catalog.catalog_upsert({"song_id": row["song_id"]} | fields)
    if not r["success"]:
        return _fail(422, r["error"], field_errors=_field_errors(r.get("errors")))
    a = _act(x_app_session)
    if a is not None and r["action"] == "update":
        a["edited"].add(row["song_id"])
    return {"success": True, "action": r["action"], "changed": r["changed"], "row": r["row"],
            "warnings": r["warnings"],
            "karaoke_changed": "karaoke_url" in r["changed"]}


@api.post("/song/add")
def add_song(body: dict = Body(...), x_app_session: str = Header(default="")):
    key = str(body.get("song_key", ""))
    if key and not YT_KEY_RE.match(key):
        return _fail(400, "song_key must be yt:<video id> (or empty).")
    if key and _song(key):
        return _fail(409, "Already in the catalog.", song_id=_song(key)["song_id"])
    if key and jobs.running_for(key):
        return {"success": True} | {"job_id": jobs.running_for(key)["job_id"]}
    fields, errors = _clean_fields(body.get("fields"))
    if not str(fields.get("title") or "").strip():
        errors["title"] = "required."
    rating_v = body.get("rating")
    if rating_v is not None and (isinstance(rating_v, bool) or not isinstance(rating_v, int)
                                 or not 1 <= rating_v <= 10):
        errors["my_rating"] = "1-10."
    if not errors:  # validate up front, so mistakes show in the sheet, not in a job
        _, verrs, _ = catalog.validate_row({c: "" for c in vocab.SONG_COLUMNS} | fields)
        errors = _field_errors(verrs)
    if errors:
        return _fail(422, "Some fields need fixing.", field_errors=errors)
    fields.pop("my_rating", None)
    job = jobs.start_add(key, fields, rating_v)
    a = _act(x_app_session)
    if a is not None:
        a["added"].add(key or job["job_id"])
    return {"success": True, "job_id": job["job_id"]}


@api.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id) if re.fullmatch(r"[0-9a-f]{12}", job_id) else None
    if not job:
        raise HTTPException(status_code=404, detail="No such job.")
    return {"success": True} | job


@api.post("/song/{key}/reanalyse")
def reanalyse(key: str):
    row = _song(key)
    if not row:
        return _fail(404, "Not in the catalog.")
    if not row["karaoke_url"]:
        return _fail(400, "Set a karaoke link first.", field_errors={"karaoke_url": "needed to re-analyse."})
    return {"success": True, "job_id": jobs.start_reanalyse(row["song_id"])["job_id"]}


@api.get("/karaoke/find")
def karaoke_find(title: str = Query(..., min_length=1, max_length=200), film: str = Query("", max_length=200)):
    r = youtube.karaoke_find(title, film, "", 5)
    if not r.get("success"):
        return _fail(502, r.get("error") or "Search failed.")
    return {"success": True, "candidates": [{k: c[k] for k in ("url", "title", "channel", "score", "source")}
                                            for c in r.get("candidates", [])]}


_embed_cache: dict[str, bool | None] = {}


@api.get("/embeddable")
def embeddable(url: str = Query(..., max_length=300)):
    """oEmbed: 401/403 means the uploader blocked embedding (show Open in YouTube)."""
    vid = youtube.video_id_from_url(url) if is_youtube_url(url) else ""
    if not vid:
        return _fail(400, "Not a YouTube video link.")
    if vid not in _embed_cache:
        try:
            r = requests.get(OEMBED, params={"url": youtube.watch_url(vid), "format": "json"}, timeout=8)
            _embed_cache[vid] = True if r.status_code == 200 else (False if r.status_code in (401, 403) else None)
        except requests.RequestException:
            return {"success": True, "video_id": vid, "embeddable": None}
    return {"success": True, "video_id": vid, "embeddable": _embed_cache[vid]}


_original_cache: dict[tuple[str, str], str] = {}


@api.get("/original")
def original(title: str = Query(..., min_length=1, max_length=200), film: str = Query("", max_length=200)):
    """The original (film) video of a song, for the player's Original tab."""
    k = (title.strip().lower(), film.strip().lower())
    if k not in _original_cache:
        info, err = youtube._find_seed(title.strip(), "", "original", film.strip())
        if err:
            return _fail(404, err.get("error") or "No original video found.")
        _original_cache[k] = youtube.watch_url(info["video_id"])
    return {"success": True, "url": _original_cache[k]}


# --- close ---------------------------------------------------------------------------------------

@api.post("/session/close")
def close(x_app_session: str = Header(default="")):
    """One summary line to the chat for what was saved since the last close.
    The client calls this on Done and, best effort, when the app is hidden, so a
    repeat call with nothing new sends nothing."""
    a = _act(x_app_session)
    if a is None:
        return _fail(400, "Missing X-App-Session.")
    with _activity_lock:
        line = summary(a)
        if line:
            for k in ("likes", "ratings", "added", "edited"):
                a[k] = type(a[k])()
    if not line:
        return {"success": True, "sent": False, "reason": "nothing new"}
    r = tg.send(line)
    return {"success": r["success"], "sent": r["success"], "text": line, "error": r.get("error", "")}


app = FastAPI(title="KaraokeGayak", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api)


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
