"""Background jobs: adding an outside song to the catalog, and re-analysing a
song's karaoke track. Same deterministic pipeline the agent uses (catalog_upsert,
youtube_fetch_audio -> analyze_audio -> cleanup_job, lyrics_search ->
lyrics_save), minus the LLM parts: genre / mood / theme / situation /
tempo_class are left empty and the song is flagged in review.csv for the agent
to tag. Jobs live in memory; they are short (about a minute).
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import audio
import cache
import catalog
import feedback
import lookup
import lyrics
import youtube
from common import is_youtube_url, log

from . import similar

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jobs")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
MAX_JOBS = 100
TAGS_PENDING = "Added from the Mini App: genre, mood, theme, situation and tempo_class not tagged yet."


def _new(kind: str, song_key: str) -> dict:
    job = {"job_id": uuid.uuid4().hex[:12], "kind": kind, "song_key": song_key, "status": "running",
           "steps": [], "song_id": "", "error": "", "started": time.time()}
    with _lock:
        _jobs[job["job_id"]] = job
        for old in sorted(_jobs.values(), key=lambda j: j["started"])[:-MAX_JOBS]:
            _jobs.pop(old["job_id"], None)
    return job


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job, steps=list(job["steps"])) if job else None


def running_for(song_key: str) -> dict | None:
    with _lock:
        return next((dict(j) for j in _jobs.values()
                     if j["song_key"] == song_key and j["status"] == "running"), None)


def _step(job: dict, name: str, ok_: bool, note: str = "") -> None:
    job["steps"].append({"step": name, "ok": ok_, "note": note[:200]})


def _run(job: dict, fn, *args) -> None:
    try:
        fn(job, *args)
        job["status"] = "done"
    except Exception as e:  # report, never crash the server
        log(f"[jobs] {job['kind']} {job['song_key']}: {e!r}")
        job["status"], job["error"] = "error", str(e)[:300]


# --- analysis ---------------------------------------------------------------------------

def analyse(job: dict, song_id: str, url: str) -> None:
    """bpm, musical_key, energy, duration_sec from the karaoke track."""
    fetched = youtube.youtube_fetch_audio(url)
    if not fetched["success"]:
        _step(job, "download", False, fetched["error"])
        return
    try:
        a = audio.analyze_audio(fetched["audio_path"])
    finally:
        youtube.cleanup_job(fetched["job_id"])
    if not a["success"]:
        _step(job, "analyse", False, a["error"])
        return
    fields = {"song_id": song_id, "bpm": a.get("bpm"), "musical_key": a.get("musical_key"),
              "energy": a.get("energy"), "duration_sec": a.get("duration_sec")}
    r = catalog.catalog_upsert({k: v for k, v in fields.items() if v not in (None, "")})
    _step(job, "analyse", r["success"], r.get("error") or
          f"{a.get('bpm')} BPM · {a.get('musical_key')} · energy {a.get('energy')}")


def _reanalyse(job: dict, song_id: str) -> None:
    row = catalog.catalog_get(song_id)["row"]
    if not row["karaoke_url"]:
        raise ValueError("The song has no karaoke link to analyse.")
    job["song_id"] = song_id
    analyse(job, song_id, row["karaoke_url"])
    if job["steps"] and not job["steps"][-1]["ok"]:
        raise ValueError(job["steps"][-1]["note"] or "analysis failed")


def start_reanalyse(song_id: str) -> dict:
    job = _new("reanalyse", song_id)
    _pool.submit(_run, job, _reanalyse, song_id)
    return job


# --- add a song --------------------------------------------------------------------------

def _add(job: dict, fields: dict, rating: int | None) -> None:
    title, film = fields["title"], fields.get("film", "")
    if not fields.get("year") or not fields.get("music_director"):
        try:
            lk = lookup.song_lookup(title, film)
            if lk.get("success") and lk.get("found"):
                year = str(lk.get("release_year") or "")
                if year and not fields.get("year"):
                    fields["year"] = year
                md = (lk.get("infobox") or {}).get("music_by", "")
                if md and not fields.get("music_director"):
                    fields["music_director"] = md.replace(",", ";").replace(" and ", ";")
            _step(job, "lookup", True, lk.get("page_title", "") or "no Wikipedia article")
        except Exception as e:
            _step(job, "lookup", False, str(e))
    if not fields.get("karaoke_url"):
        track = cache.karaoke_many([(title, film)])[0]
        if track:
            fields["karaoke_url"], fields["karaoke_channel"] = track["url"], track["channel"]
        _step(job, "karaoke", bool(track), track["title"] if track else "no karaoke track found")
    row = dict(fields, added_from="youtube-link" if fields.get("karaoke_url") else "song-name",
               confidence="medium")
    if rating is not None:
        row |= {"my_rating": rating, "rated_on": date.today().isoformat()}
    r = catalog.catalog_upsert(row)
    if not r["success"]:
        job["field_errors"] = r.get("errors", [])
        raise ValueError(r["error"])
    song_id = job["song_id"] = r["song_id"]
    _step(job, "catalog", True, song_id)
    key = job["song_key"]
    if key.startswith("yt:"):
        similar.added_keys[key] = song_id
        feedback.relations_rekey(key, song_id)
    catalog.review_add(song_id, "mini-app", "unsure-match", TAGS_PENDING)
    if row.get("karaoke_url"):
        analyse(job, song_id, row["karaoke_url"])
    try:
        found = lyrics.lyrics_search(title, film, fields.get("singers", ""))
        best = found.get("best") if found.get("success") and found.get("found") else None
        if best and best.get("schema_source"):
            s = catalog.lyrics_save(song_id, best["text"], best["schema_source"], best.get("url", ""),
                                    best.get("chords", ""))
            _step(job, "lyrics", s["success"], s.get("error") or best["schema_source"])
        else:
            _step(job, "lyrics", False, "no lyrics found")
    except Exception as e:
        _step(job, "lyrics", False, str(e))


def start_add(song_key: str, fields: dict, rating: int | None) -> dict:
    """fields: catalog columns from the edit sheet (title required)."""
    job = _new("add", song_key)
    _pool.submit(_run, job, _add, dict(fields), rating)
    return job


def youtube_ok(url: str) -> bool:
    return not url or is_youtube_url(url)
