"""YouTube via yt-dlp (no login, no API key): channel index, karaoke search,
metadata, audio download, job cleanup, dead-link checks.

yt-dlp patterns follow media-download: a stderr-only logger (stdout is the
MCP channel), ffmpeg_location from shutil.which(FFMPEG_PATH) because Hermes
runs under launchd without /usr/local/bin on PATH, and the same job_id
format + cleanup rules.
"""

import contextlib
import csv
import hashlib
import json
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from rapidfuzz import fuzz

import catalog
import vocab
from common import (fail, ffmpeg_path, is_youtube_url, js_runtime, log, max_duration_sec,
                    media_root, ok, preferred_channel_url)

INDEX_REL = "index/anilchauhan.csv"
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# karaoke_find: an index hit at or above this score is "strong" and we skip
# the YouTube search. Scores are 0-100: 85% token_set_ratio (query words all
# present in the video title -> 100) + 15% plain ratio (penalises long titles
# that merely contain the query). "Pal Pal Dil Ke Paas" vs "Pal Pal Dil Ke
# Paas Tum Rehti Ho" scores ~96; unrelated songs sharing a word score < 70.
STRONG_INDEX_SCORE = 85
MIN_CANDIDATE_SCORE = 60
PENALTY_WORDS = ("cover", "unplugged", "female version", "female", "lofi", "lo-fi",
                 "remix", "reprise", "slowed", "reverb", "instrumental", "mashup", "medley")
PENALTY = 25
# Shorter edits of the same song: still the right song, so only nudged down
# (the channel often has both a Full and an Abridged upload).
SOFT_PENALTY_WORDS = ("abridged", "short version", "short ver")
SOFT_PENALTY = 5

# Words karaoke uploaders add to titles; stripped before matching.
NOISE_PHRASES = [
    "full karaoke", "karaoke track", "karaoke song", "karaoke version", "karaoke",
    "with scrolling lyrics", "scrolling lyrics", "with lyrics", "lyrics", "scrolling",
    "english", "eng", "hindi", "hq", "hd", "4k", "original", "clean", "track",
    "improved version", "improved ver", "renewed", "recreated", "video", "official",
    "full song", "with",
]
_NOISE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in
                                         sorted(NOISE_PHRASES, key=len, reverse=True)) + r")\b")

ERROR_STATUS = {"private": "private", "not_found": "removed", "unavailable": "unavailable",
                "login_required": "login_required"}


# --- yt-dlp plumbing ------------------------------------------------------------------

class _StderrLogger:
    """yt-dlp must never write to stdout: stdout is the MCP protocol channel."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log(f"[yt-dlp] {msg}")

    def error(self, msg):
        log(f"[yt-dlp] {msg}")


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": True,
        "logger": _StderrLogger(),
        "socket_timeout": 30,
        "retries": 3,
    }
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    runtime = js_runtime()
    if runtime:  # YouTube's signature/n challenges need a JS runtime (yt-dlp EJS)
        name, path = runtime
        opts["js_runtimes"] = {name: {"path": path}}
    return opts


def _extract(url: str, opts: dict, download: bool = False) -> dict:
    import yt_dlp
    with contextlib.redirect_stdout(sys.stderr), yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download)
        return ydl.sanitize_info(info)


def classify_error(msg: str) -> str:
    m = (msg or "").lower()
    if "private video" in m or "is private" in m:
        return "private"
    if "sign in to confirm your age" in m or "age-restricted" in m or "inappropriate" in m:
        return "login_required"
    if "sign in to confirm you" in m and "bot" in m:
        return "rate_limited"
    if "429" in m or "too many requests" in m or re.search(r"\brate[- ]?limit", m):
        return "rate_limited"
    if ("removed" in m or "terminated" in m or "no longer available" in m or "404" in m
            or "does not exist" in m or "deleted" in m):
        return "not_found"
    if "video unavailable" in m or "not available" in m or "unavailable" in m:
        return "unavailable"
    if "login" in m or "log in" in m or "members-only" in m or "join this channel" in m:
        return "login_required"
    if "unsupported url" in m:
        return "unsupported_url"
    return "download_failed"


def video_id_from_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    else:
        vid = (parse_qs(p.query).get("v") or [""])[0]
        if not vid:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", p.path)
            vid = m.group(1) if m else ""
    return vid if VIDEO_ID_RE.match(vid) else ""


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _check_video_url(url: str) -> tuple[str, dict | None]:
    """Return (canonical watch URL, None) or ("", fail-result)."""
    url = (url or "").strip()
    if not is_youtube_url(url):
        return "", fail("domain_not_allowed", "Only youtube.com, www/m/music.youtube.com and "
                        "youtu.be URLs are accepted.")
    vid = video_id_from_url(url)
    if not vid:
        return "", fail("invalid_url", "Not a single-video YouTube URL (need watch?v=, youtu.be/, "
                        "shorts/ or embed/).")
    return watch_url(vid), None


# --- title normalisation + matching ----------------------------------------------------

def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", t)  # (Male), [HQ], {Improved}
    t = re.sub(r"[\u0900-\u097f]+", " ", t)  # Devanagari ("हिंदी") is never the match key
    t = re.sub(r"[^\w\s]", " ", t)  # punctuation, including &, ｜ and -
    t = _NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_score(query: str, video_title: str) -> float:
    q, v = normalize_title(query), normalize_title(video_title)
    if not q or not v:
        return 0.0
    return 0.85 * fuzz.token_set_ratio(q, v) + 0.15 * fuzz.ratio(q, v)


def _penalty(video_title: str, asked: str) -> int:
    # NFKC: the channel writes "𝔸𝕓𝕣𝕚𝕕𝕘𝕖𝕕" in math alphanumerics.
    t = unicodedata.normalize("NFKC", video_title or "").lower()
    a = unicodedata.normalize("NFKC", asked or "").lower()
    hard = PENALTY if any(w in t and w not in a for w in PENALTY_WORDS) else 0
    soft = SOFT_PENALTY if any(w in t and w not in a for w in SOFT_PENALTY_WORDS) else 0
    return hard + soft


def load_index() -> list[dict]:
    path = catalog.wiki_path(INDEX_REL)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != vocab.INDEX_COLUMNS:
            return []
        return list(reader)


def match_index(rows: list[dict], title: str, film: str = "", singers: str = "",
                limit: int = 5) -> list[dict]:
    asked = " ".join([title, film, singers])
    scored = []
    for r in rows:
        s = title_score(title, r["title"]) - _penalty(r["title"], asked)
        if s >= MIN_CANDIDATE_SCORE:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [{"url": r["url"], "title": r["title"], "channel": "ANIL CHAUHAN KARAOKE",
             "duration_sec": int(r["duration_sec"]) if r["duration_sec"].isdigit() else None,
             "score": round(s, 1), "source": "index"} for s, r in scored[:limit]]


def _channel_key(url_or_handle: str) -> str:
    """'@anilchauhankaraoke' from a channel URL or uploader_id, lowercased."""
    m = re.search(r"(@[\w.-]+)", url_or_handle or "")
    return m.group(1).lower() if m else (url_or_handle or "").lower().rstrip("/")


def rank_search_results(entries: list[dict], title: str, film: str = "", singers: str = "",
                        limit: int = 5) -> list[dict]:
    preferred = _channel_key(preferred_channel_url())
    asked = " ".join([title, film, singers])
    ranked = []
    for e in entries:
        vid = e.get("id") or ""
        if not VIDEO_ID_RE.match(vid):
            continue
        vtitle = e.get("title") or ""
        sim = title_score(title, vtitle)
        is_pref = preferred in (_channel_key(e.get("uploader_id") or ""),
                                _channel_key(e.get("channel_url") or ""),
                                _channel_key(e.get("uploader_url") or ""))
        has_karaoke = "karaoke" in vtitle.lower()
        pen = _penalty(vtitle, asked)
        score = sim - pen + (10 if has_karaoke else 0) + (15 if is_pref else 0)
        # Preferred channel first, but only when it's plausibly the same song.
        key = (is_pref and sim >= 70 and not pen, sim - pen, has_karaoke)
        ranked.append((key, {
            "url": watch_url(vid), "title": vtitle,
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration_sec": int(e["duration"]) if e.get("duration") else None,
            "score": round(min(score, 100.0), 1), "source": "search",
        }))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in ranked[:limit]]


# --- tools ----------------------------------------------------------------------------

def channel_index_refresh(channel_url: str = "") -> dict:
    channel_url = (channel_url or preferred_channel_url()).strip().rstrip("/")
    if not is_youtube_url(channel_url):
        return fail("domain_not_allowed", "channel_url must be a YouTube URL.")
    path_part = urlparse(channel_url).path
    if not re.match(r"^/(@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)", path_part):
        return fail("invalid_url", "channel_url must be a channel URL like "
                    "https://www.youtube.com/@AnilChauhankaraoke.")
    if not re.search(r"/(videos|streams|shorts)$", channel_url):
        channel_url += "/videos"
    opts = _base_opts() | {"extract_flat": "in_playlist", "noplaylist": False,
                           "lazy_playlist": False}
    try:
        info = _extract(channel_url, opts)
    except Exception as e:
        return fail(classify_error(str(e)), str(e))
    today = date.today().isoformat()
    rows, seen = [], set()
    for e in info.get("entries") or []:
        vid = (e or {}).get("id") or ""
        if not VIDEO_ID_RE.match(vid) or vid in seen:
            continue
        seen.add(vid)
        rows.append({"video_id": vid, "title": re.sub(r"\s+", " ", e.get("title") or "").strip(),
                     "url": watch_url(vid),
                     "duration_sec": str(int(e["duration"])) if e.get("duration") else "",
                     "indexed_on": today})
    if not rows:
        return fail("empty_result", "The channel listing returned no videos; the existing "
                    "index was left unchanged.")
    with catalog._lock:
        path = catalog.wiki_path(INDEX_REL)
        previous = len(load_index())
        catalog.atomic_write(path, catalog.render_csv(vocab.INDEX_COLUMNS, rows, {"duration_sec"}))
        catalog.append_log("index-refresh", INDEX_REL, f"{len(rows)} videos (was {previous})")
    return ok(channel=info.get("channel") or info.get("uploader") or "", channel_url=channel_url,
              rows=len(rows), previous_rows=previous, index_file=INDEX_REL)


def karaoke_find(title: str, film: str = "", singers: str = "", max_results: int = 5) -> dict:
    title = (title or "").strip()
    if not title:
        return fail("invalid_input", "title is required.")
    try:
        max_results = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        max_results = 5
    notes = []
    index = load_index()
    if not index:
        notes.append("Channel index is empty or missing; run channel_index_refresh.")
    candidates = match_index(index, title, film, singers, max_results)
    if candidates and candidates[0]["score"] >= STRONG_INDEX_SCORE:
        return ok(candidates=candidates, used_search=False, notes=notes)

    query = " ".join(p for p in (title, film, "karaoke") if p)
    try:
        info = _extract(f"ytsearch10:{query}", _base_opts() | {"extract_flat": True,
                                                               "noplaylist": False})
    except Exception as e:
        if candidates:
            notes.append(f"YouTube search failed ({classify_error(str(e))}); index matches only.")
            return ok(candidates=candidates, used_search=False, notes=notes)
        return fail(classify_error(str(e)), str(e), candidates=[])
    found = rank_search_results(info.get("entries") or [], title, film, singers, max_results)
    seen = {c["url"] for c in found}
    merged = found + [c for c in candidates if c["url"] not in seen]
    merged.sort(key=lambda c: (c["source"] == "index" and c["score"] >= STRONG_INDEX_SCORE,
                               c["score"]), reverse=True)
    return ok(candidates=merged[:max_results], used_search=True, query=query, notes=notes)


def _info_fields(info: dict, url: str) -> dict:
    return {
        "url": url,
        "video_id": info.get("id") or "",
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_url": info.get("channel_url") or "",
        "duration_sec": int(info["duration"]) if info.get("duration") else None,
        "upload_date": info.get("upload_date") or "",
        "availability": info.get("availability") or "",
        "description": (info.get("description") or "")[:1500],
    }


def youtube_info(url: str) -> dict:
    clean, err = _check_video_url(url)
    if err:
        return err
    try:
        info = _extract(clean, _base_opts() | {"skip_download": True})
    except Exception as e:
        code = classify_error(str(e))
        return fail(code, str(e), url=clean, availability=ERROR_STATUS.get(code, ""))
    if info.get("_type") == "playlist":
        return fail("invalid_url", "URL points to a playlist, not a video.", url=clean)
    return ok(**_info_fields(info, clean))


def youtube_fetch_audio(url: str) -> dict:
    clean, err = _check_video_url(url)
    if err:
        return err | {"job_id": "", "audio_path": ""}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return fail("ffmpeg_not_found", "ffmpeg not found; set FFMPEG_PATH=/usr/local/bin/ffmpeg.",
                    job_id="", audio_path="")
    root = media_root()
    now = datetime.now().astimezone()
    job_id = now.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(clean.encode()).hexdigest()[:8]
    job_dir = root / job_id

    def bail(code, message, **known):
        shutil.rmtree(job_dir, ignore_errors=True)
        return fail(code, message, job_id="", audio_path="", **known)

    known: dict = {"url": clean}
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = _extract(clean, _base_opts() | {"skip_download": True})
        except Exception as e:
            return bail(classify_error(str(e)), str(e), **known)
        known = _info_fields(info, clean)
        duration = info.get("duration") or 0
        if duration > max_duration_sec():
            return bail("too_long", f"Video is {int(duration)}s; limit is {max_duration_sec()}s.",
                        **known)
        opts = _base_opts() | {
            "format": "bestaudio/best",
            "outtmpl": str(job_dir / "audio.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": "192"}],
        }
        try:
            _extract(clean, opts, download=True)
        except Exception as e:
            return bail(classify_error(str(e)), str(e), **known)
        audio_path = job_dir / "audio.mp3"
        if not audio_path.is_file() or audio_path.stat().st_size < 1000:
            return bail("download_failed", "yt-dlp finished but no audio.mp3 was written.", **known)
        for leftover in job_dir.iterdir():  # source .webm/.m4a if the postprocessor kept it
            if leftover.name != "audio.mp3":
                leftover.unlink(missing_ok=True)
        result = ok(job_id=job_id, audio_path=str(audio_path), **known)
        (job_dir / "metadata.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except Exception as e:  # last-resort guard; must never raise through MCP
        log(f"[youtube_fetch_audio] unexpected error: {e!r}")
        return bail("download_failed", f"Unexpected error: {e}", **known)


def cleanup_job(job_id: str) -> dict:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        return fail("invalid_input", "Invalid job_id format.")
    root = media_root()
    job_dir = (root / job_id).resolve()
    if job_dir.parent != root:
        return fail("path_not_allowed", "job_id resolves outside MEDIA_ROOT.")
    if not job_dir.exists():
        return ok(note="already gone")
    if not job_dir.is_dir():
        return fail("invalid_input", "job path is not a folder.")
    try:
        shutil.rmtree(job_dir)
    except OSError as e:
        return fail("cleanup_failed", f"Could not delete job folder: {e}")
    return ok()


def check_links(limit: int = 50, offset: int = 0) -> dict:
    try:
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    except (TypeError, ValueError):
        return fail("invalid_input", "limit and offset must be whole numbers.")
    rows = [r for r in catalog.load_songs() if r["karaoke_url"]]
    page = rows[offset:offset + limit]
    dead, errors = [], []
    for r in page:
        res = youtube_info(r["karaoke_url"])
        status = res.get("availability", "")
        if res["success"] and status in ("public", "unlisted", ""):
            continue
        item = {"song_id": r["song_id"], "title": r["title"], "url": r["karaoke_url"]}
        if res["success"]:  # reachable but e.g. needs_auth / premium_only / subscriber_only
            dead.append(item | {"status": status, "error": ""})
        elif res["error_code"] in ERROR_STATUS or res["error_code"] in ("invalid_url",
                                                                         "domain_not_allowed"):
            dead.append(item | {"status": ERROR_STATUS.get(res["error_code"], "invalid"),
                                "error": res["error"][:200]})
        else:  # network / rate limit: not evidence the link is dead
            errors.append(item | {"error_code": res["error_code"], "error": res["error"][:200]})
    next_offset = offset + limit if offset + limit < len(rows) else None
    return ok(checked=len(page), total_with_links=len(rows), dead=dead, errors=errors,
              next_offset=next_offset)
