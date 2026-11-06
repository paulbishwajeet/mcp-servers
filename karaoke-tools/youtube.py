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
# the YouTube search. Scores are 0-100: 45% token_set_ratio (query words all
# present in the video title -> 100) + 40% partial_ratio (query present as a
# phrase) + 15% plain ratio (penalises long titles that merely contain the
# query). "Pal Pal Dil Ke Paas" vs "Pal Pal Dil Ke Paas Tum Rehti Ho" scores
# ~96; "Deewangi Deewangi" vs "Aisi Deewangi Dekhi Nahin Kahin" ~83.
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
    # token_set alone dedups words, so "deewangi deewangi" fully "matches"
    # "aisi deewangi dekhi nahin kahin"; partial_ratio requires the query to
    # appear as a phrase.
    return (0.45 * fuzz.token_set_ratio(q, v) + 0.40 * fuzz.partial_ratio(q, v)
            + 0.15 * fuzz.ratio(q, v))


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


# --- playlists ----------------------------------------------------------------------

PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
MEDLEY_WORDS = ("medley", "mashup", "mash-up", "jukebox", "non-stop", "nonstop")
VERSION_TAGS = {  # catalog title suffix -> words that mark that version in a video title
    "unplugged": ("unplugged", "acoustic"),
    "remix": ("remix",),
    "piano": ("piano",),
    "reprise": ("reprise", "reprised"),
}
# playlist_triage: a catalog title scoring at or above MATCH_SCORE is "existing";
# MAYBE_SCORE..MATCH_SCORE is reported as a possible match for the agent to decide.
# Score: 100 for an exact song name, 95 when a 2+ word catalog title starts the
# song name parsed from the video title, else the mean of token_set_ratio and
# token_sort_ratio, so a short catalog title ("Pal") doesn't match every video
# that contains the word but spelling variants (Diwana/Deewana) still do.
MATCH_SCORE = 85
MAYBE_SCORE = 75
_SEGMENT_SPLIT = re.compile(r"\s*(?:\|\||\||\s[-–—:]+\s|__|\(|\[)\s*")


def _check_playlist_url(url: str) -> tuple[str, dict | None]:
    """Return (canonical playlist URL, None) or ("", fail-result)."""
    url = (url or "").strip()
    if not is_youtube_url(url):
        return "", fail("domain_not_allowed", "Only youtube.com, www/m/music.youtube.com and "
                        "youtu.be URLs are accepted.")
    pid = (parse_qs(urlparse(url).query).get("list") or [""])[0]
    if not PLAYLIST_ID_RE.match(pid):
        return "", fail("invalid_url", "Not a playlist URL (need a list= parameter, e.g. "
                        "https://www.youtube.com/playlist?list=PL...).")
    return f"https://www.youtube.com/playlist?list={pid}", None


def _playlist_entries(url: str) -> tuple[dict, list[dict], dict | None]:
    """Flat-list a playlist (ids and titles only; nothing is downloaded)."""
    clean, err = _check_playlist_url(url)
    if err:
        return {}, [], err
    opts = _base_opts() | {"extract_flat": "in_playlist", "noplaylist": False,
                           "lazy_playlist": False}
    try:
        info = _extract(clean, opts)
    except Exception as e:
        return {}, [], fail(classify_error(str(e)), str(e))
    videos, seen = [], set()
    for e in info.get("entries") or []:
        vid = (e or {}).get("id") or ""
        if not VIDEO_ID_RE.match(vid) or vid in seen:
            continue
        seen.add(vid)
        title = re.sub(r"\s+", " ", e.get("title") or "").strip()
        unavailable = not title or title.lower() in UNAVAILABLE_TITLES or \
            (e.get("availability") or "") in ("private", "needs_auth", "subscriber_only")
        videos.append({"video_id": vid, "url": watch_url(vid), "title": title,
                       "channel": e.get("channel") or e.get("uploader") or "",
                       "duration_sec": int(e["duration"]) if e.get("duration") else None,
                       "unavailable": unavailable})
    meta = {"playlist_url": clean, "playlist_id": info.get("id") or "",
            "playlist_title": info.get("title") or ""}
    return meta, videos, None


def _page(offset, limit, default=50, cap=200):
    try:
        return max(0, int(offset)), max(1, min(int(limit), cap)), None
    except (TypeError, ValueError):
        return 0, default, fail("invalid_input", "offset and limit must be whole numbers.")


def playlist_list(url: str, offset: int = 0, limit: int = 50) -> dict:
    offset, limit, err = _page(offset, limit)
    if err:
        return err
    meta, videos, err = _playlist_entries(url)
    if err:
        return err
    page = videos[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(videos) else None
    return ok(**meta, total=len(videos), offset=offset, limit=limit, next_offset=next_offset,
              videos=page)


# Version / arrangement words: kept out of the song name (video_version records them).
_VERSION_NOISE = re.compile(
    r"\b(unplugged|acoustic|remix|reprised?|piano|cover|instrumental|version|male|female|"
    r"(lower|higher|low|high)\s+(key|scale)|key|scale|duet|solo|full|short|clean)\b")


def _song_words(text: str) -> str:
    t = re.sub(r"\b(?:https?://|www\.)\S+|\b\S+\.(?:com|in|net|org)\b", " ", text or "",
               flags=re.I)
    t = _VERSION_NOISE.sub(" ", normalize_title(t))
    return re.sub(r"\s+", " ", t).strip()


def song_part(video_title: str) -> str:
    """The song-name part of a karaoke video title: the first segment (split on
    | || - : __ ( [ ) that still has words after karaoke, version and URL noise
    is removed."""
    for seg in _SEGMENT_SPLIT.split(unicodedata.normalize("NFKC", video_title or "")):
        if _song_words(seg):
            return _song_words(seg)
    return _song_words(video_title)


def video_version(video_title: str) -> str:
    t = unicodedata.normalize("NFKC", video_title or "").lower()
    return next((tag for tag, words in VERSION_TAGS.items() if any(w in t for w in words)), "")


def _row_version(title: str) -> str:
    m = re.search(r"\((unplugged|remix|piano|reprise)\)", (title or "").lower())
    return m.group(1) if m else ""


def match_score(catalog_title: str, song_text: str) -> float:
    a = normalize_title(re.sub(r"\(.*?\)", " ", catalog_title or ""))
    if not a or not song_text:
        return 0.0
    if song_text == a:
        return 100.0
    # Video titles often carry the whole opening line ("Bole Chudiyan Bole
    # Kangna"): a catalog title of 2+ words that starts the song text is a match.
    if len(a.split()) >= 2 and (song_text + " ").startswith(a + " "):
        extra = len(song_text.split()) - len(a.split())
        # A short title followed by several more words may be a different song
        # ("Tum Hi Ho" vs "Tum Hi Ho Bandhu Sakha Tum Hi"): ask, don't assume.
        return 80.0 if len(a.split()) <= 3 and extra >= 3 else 95.0
    score = 0.5 * fuzz.token_set_ratio(a, song_text) + 0.5 * fuzz.token_sort_ratio(a, song_text)
    # A one-word catalog title ("Safar") only matches exactly, not "Lo Safar".
    return min(score, 70.0) if len(a.split()) == 1 else score


def playlist_triage(url: str, offset: int = 0, limit: int = 50) -> dict:
    offset, limit, err = _page(offset, limit)
    if err:
        return err
    meta, videos, err = _playlist_entries(url)
    if err:
        return err
    songs = catalog.load_songs()
    groups = {"existing": [], "new_version": [], "maybe": [], "new": [], "medley": [],
              "unavailable": []}
    for v in videos[offset:offset + limit]:
        item = {"video_id": v["video_id"], "url": v["url"], "title": v["title"][:100],
                "duration_sec": v["duration_sec"]}
        low = unicodedata.normalize("NFKC", v["title"]).lower()
        if v["unavailable"]:
            groups["unavailable"].append(item)
            continue
        if any(w in low for w in MEDLEY_WORDS):
            groups["medley"].append(item)
            continue
        text, version = song_part(v["title"]), video_version(v["title"])
        item |= {"song_text": text, "version": version}
        if low.count("||") >= 3:
            item["note"] = "Several titles separated by ||: may be a medley."
        scored = sorted(((match_score(s["title"], text), s) for s in songs),
                        key=lambda x: -x[0])
        same_version = [(sc, s) for sc, s in scored if _row_version(s["title"]) == version]
        best = same_version[0] if same_version else (0.0, None)
        any_best = (scored or [(0.0, None)])[0]
        if best[1] is not None and best[0] >= MATCH_SCORE:
            s = best[1]
            alts = s["karaoke_alt_urls"].split(";") if s["karaoke_alt_urls"] else []
            link = ("same" if video_id_from_url(s["karaoke_url"]) == v["video_id"] else
                    "alt" if v["video_id"] in [video_id_from_url(a) for a in alts] else
                    "none" if not s["karaoke_url"] else "different")
            groups["existing"].append(item | {"song_id": s["song_id"], "catalog_title": s["title"],
                                              "score": round(best[0], 1), "link": link})
        elif any_best[1] is not None and any_best[0] >= MATCH_SCORE:
            groups["new_version"].append(item | {"song_id": any_best[1]["song_id"],
                                                 "catalog_title": any_best[1]["title"],
                                                 "score": round(any_best[0], 1)})
        elif any_best[1] is not None and any_best[0] >= MAYBE_SCORE:
            groups["maybe"].append(item | {"song_id": any_best[1]["song_id"],
                                           "catalog_title": any_best[1]["title"],
                                           "score": round(any_best[0], 1)})
        else:
            groups["new"].append(item)
    next_offset = offset + limit if offset + limit < len(videos) else None
    return ok(**meta, total=len(videos), offset=offset, limit=limit, next_offset=next_offset,
              counts={k: len(v) for k, v in groups.items()}, **groups)


# --- mixes ("similar songs") ------------------------------------------------------------

MIX_SOURCES = ("youtube", "youtube-music", "both")
MIX_SEEDS = ("original", "karaoke")
SEED_SKIP_WORDS = ("karaoke", "cover", "instrumental", "unplugged", "reprise", "slowed",
                   "lofi", "lo-fi")
# Official label channels, preferred for seed="original" when they upload the song.
LABEL_CHANNELS = ("saregama", "t-series", "tseries", "sony music india", "zee music",
                  "tips official", "tips music", "tips films", "yrf", "shemaroo",
                  "universal music india", "eros now music", "venus")
# Seed/duplicate threshold: a mix item whose song matches the seed's song (or
# another item's) at this score is the same song.
SAME_SONG_SCORE = 90

# Noise in Indian label/lyric-channel titles: removed from song and film parts.
_TITLE_NOISE = re.compile(
    r"\b(with\s+(?:scrolling\s+)?lyrics?|with\s+(?:[a-z]+\s+){0,2}vocals?|"
    r"(?:guide|singer|female|male)\s+vocals?|sing\s+along|(?:lower|higher|low|high)\s+(?:key|scale|pitch|version)|"
    r"lower|higher|scale|"
    r"full\s+(?:video|song|audio|version|hd\s+video)|official\s+(?:music\s+)?(?:video|audio|"
    r"lyric(?:al)?\s+video)|lyrical(?:\s+video)?|lyrics?(?:\s+video)?|with\s+lyrics|"
    r"scrolling\s+lyrics|music\s+video|video\s+song|audio\s+song|full\s+lyrics|"
    r"karaoke(?:\s+(?:track|version|song))?|hd|4k|8k|1080p|720p|remastered|"
    r"title\s+(?:song|track)|bonus\s+track|(?:fe)?male(?:\s+version)?|version|reprised?|"
    r"unplugged|remix|recreated|cover|"
    r"video|audio|song|eng(?:lish)?|hindi)\b", re.I)
_PREFIX_NOISE = re.compile(r"^\s*(?:full\s+(?:video|song|audio)|lyrical|song|video|audio)\s*[:\-–]\s*",
                           re.I)
_FROM_FILM = re.compile(r"""\(?\bfrom\s+["“']([^"”']+)["”']\)?""", re.I)
_GENERIC_SEGMENT = re.compile(
    r"\b(songs?|hits|hindi|bollywood|old|romantic|classic|evergreen|superhit|best|status|"
    r"jukebox|playlist|official|music|records?|movie|film|retro|new|latest|lyrics?)\b", re.I)
_MIX_SPLIT = re.compile(r"\s*(\|\||\||\s[-–—]\s?|\s~\s|__|//|:\s)\s*")

KIND_RULES = [  # first match wins
    ("jukebox", ("jukebox", "non stop", "non-stop", "nonstop", "medley", "mashup", "mash-up",
                 "back to back", "best of", "collection", "top 10", "top 20", "top 50")),
    ("karaoke", ("karaoke", "minus one", "minus track")),
    ("other", ("reaction", "interview", "behind the scenes", "making of", "trailer", "teaser",
               "dialogue", "#shorts", "whatsapp status", "status video")),
    ("cover", ("cover", "unplugged", "reprise", "slowed", "lofi", "lo-fi", "reverb",
               "female version", "male version", "piano", "guitar", "flute", "violin",
               "saxophone", "instrumental", "acoustic", "remix", "recreated", "sung by")),
]
KIND_ORDER = {"original": 0, "karaoke": 1, "cover": 2, "other": 3, "jukebox": 4}


def classify_kind(title: str, duration_sec: int | None = None) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    for kind, words in KIND_RULES:
        if any(w in t for w in words):
            return kind
    if duration_sec and duration_sec >= 15 * 60:  # a 15-minute "song" is a compilation
        return "jukebox"
    return "original"


def _known_names() -> set[str]:
    """Singer/composer/lyricist names from the catalog, used to drop name lists
    from video titles. Read-only; empty if the catalog can't be read."""
    try:
        rows = catalog.load_songs()
    except Exception:
        return set()
    names = set()
    for r in rows:
        for col in ("singers", "music_director", "lyricist"):
            names.update(n.strip().lower() for n in r[col].split(";") if len(n.strip()) > 3)
    return names


def _known_films() -> set[str]:
    try:
        return {normalize_title(r["film"]) for r in catalog.load_songs() if r["film"]}
    except Exception:
        return set()


def _clean_segment(seg: str, names: set[str]) -> str:
    seg = re.sub(r"[ऀ-ॿ]+", " ", seg)  # Devanagari repeats of the title
    seg = re.sub(r"\((?:[^)]*\b(?:19|20)\d\d\b[^)]*)\)", " ", seg)  # "(1973)"
    seg = re.sub(r"[\(\[][^)\]]*[\)\]]", lambda m: " " if _TITLE_NOISE.search(m.group(0))
                 or len(m.group(0)) > 25 else m.group(0), seg)
    seg = _TITLE_NOISE.sub(" ", seg)
    seg = re.sub(r"#\S+", " ", seg)
    seg = re.sub(r"(?:\s*[-–—:,.!|()\[\]~&+/])+\s*$", "", re.sub(r"\s+", " ", seg))
    seg = seg.strip(" -–—:,.!|()[]~&+/")
    low = seg.lower()
    for name in sorted(names, key=len, reverse=True):  # trailing "… Kishore Kumar"
        if low.endswith(" " + name) and len(low) > len(name) + 3:
            seg, low = seg[: -len(name)].rstrip(" -–—:,&"), low[: -len(name)].rstrip(" -–—:,&")
    return seg.strip()


def _is_people(seg: str, names: set[str]) -> bool:
    low = seg.lower()
    if "," in seg or " & " in seg or " ft." in low or " feat" in low:
        return True
    return low in names or any(low.startswith(n) and len(low) <= len(n) + 3 for n in names)


def parse_song_title(video_title: str, names: set[str] | None = None,
                     films: set[str] | None = None) -> tuple[str, str]:
    """Best-guess (song_title, film) from an Indian label/lyric/karaoke video
    title. A heuristic: rules for the common patterns, no LLM."""
    names = names if names is not None else set()
    films = films if films is not None else set()
    title = unicodedata.normalize("NFKC", video_title or "")
    film = ""
    m = _FROM_FILM.search(title)
    if m:
        film, title = m.group(1).strip(), title[:m.start()] + " " + title[m.end():]
    title = _PREFIX_NOISE.sub("", title)
    # Emoji and other symbols separate parts ("Meri Bheegi Bheegi Si ❤ Anamika").
    title = "".join(" | " if unicodedata.category(c) == "So" else c for c in title)
    # Brackets hold tags or credits, never the song: treat them as separators.
    title = re.sub(r"[\(\[][^)\]]*[\)\]]?", " | ", title)
    pieces = _MIX_SPLIT.split(title)
    parts = []  # (text, is_people, separator before it) for every non-empty segment
    sep = ""
    for k, piece in enumerate(pieces):
        if k % 2:  # captured separator
            sep = piece.strip()
            continue
        seg = _clean_segment(piece, names)
        if len(seg) >= 2:
            parts.append((seg, _is_people(seg, names), sep))
        sep = sep if len(seg) < 2 else ""
    content = [i for i, (seg, people, _) in enumerate(parts)
               if not people and not (i and _GENERIC_SEGMENT.search(seg))]
    if not content:
        return _clean_segment(video_title, names), film
    is_film = lambda t: normalize_title(t) in films  # noqa: E731
    song = parts[content[0]][0]
    if len(content) > 1:
        a, (b, _, sep_b), j = song, parts[content[1]], content[1]
        if is_film(a) and not is_film(b) and sep_b in ("-", "–", "—", ":"):
            song, film = b, film or a  # "Blackmail - Pal Pal Dil Ke Paas"
        elif is_film(b):
            film = film or b
        elif sep_b in ("-", "–", "—", ":") and j == content[0] + 1 and \
                (sep_b == ":" or (len(a.split()) <= 2 and len(b.split()) >= len(a.split()) + 3)):
            # "Blackmail - Pal Pal Dil Ke Paas Tum Rehti Ho", "Luka Chuppi: Duniyaa"
            song, film = b, film or a
        elif not film:
            # "Song | Film | Actor, Actor": only trust the second segment as the
            # film when a list of people follows it.
            people_next = j + 1 < len(parts) and parts[j + 1][1]
            if people_next and len(b.split()) <= 6:
                film = b
    if not film:  # any later segment that is a known film
        film = next((parts[i][0] for i in content[1:] if is_film(parts[i][0])), "")
    song = re.sub(r"\s*\.{2,}\s*$", "", song)  # "Chura Liya Hai Tumne Jo..."
    if song.isupper() or song.islower():
        song = song.title()
    if film.isupper():
        film = film.title()
    return song, film


def _same_song(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    return max(match_score(a, nb), match_score(b, na)) >= SAME_SONG_SCORE


def _find_seed(song: str, url: str, seed: str, film: str = "") -> tuple[dict | None, dict | None]:
    """Return (seed dict, None) or (None, fail-result)."""
    if url:
        clean, err = _check_video_url(url)
        if err:
            return None, err
        vid = video_id_from_url(clean)
        return {"video_id": vid, "title": "", "channel": "", "url": clean}, None
    if seed == "karaoke":
        # A plain "<song> karaoke" search finds the right song more reliably than
        # karaoke_find's index match on short titles ("Tum Hi Ho"), and a karaoke
        # seed gives a mix of karaoke tracks.
        try:
            info = _extract(f"ytsearch5:{' '.join(p for p in (song, film) if p)} karaoke",
                            _base_opts() | {"extract_flat": True,
                                                                        "noplaylist": False})
        except Exception as e:
            return None, fail(classify_error(str(e)), str(e))
        preferred = _channel_key(preferred_channel_url())
        pool = []
        for i, e in enumerate(info.get("entries") or []):
            vid, title = (e or {}).get("id") or "", (e or {}).get("title") or ""
            if not VIDEO_ID_RE.match(vid) or "karaoke" not in title.lower():
                continue
            if title_score(song, title) < 60 or _penalty(title, song):  # other song / version
                continue
            is_pref = preferred in (_channel_key(e.get("uploader_id") or ""),
                                    _channel_key(e.get("channel_url") or ""))
            # Closeness first: "Tum Hi Ho, Hum Tere Bin…" on the preferred channel
            # must not beat an exact "Tum Hi Ho" elsewhere.
            text = song_part(title)
            close = match_score(song, text) >= MATCH_SCORE
            extra = abs(len(text.split()) - len(normalize_title(song).split()))
            pool.append(((not close, not is_pref, extra), i, {"video_id": vid, "title": title,
                                          "channel": e.get("channel") or e.get("uploader") or "",
                                          "url": watch_url(vid)}))
        if not pool:
            return None, fail("no_seed", f"No karaoke video of '{song}' found in the top 5 results.")
        pool.sort(key=lambda x: (x[0], x[1]))
        return pool[0][2], None
    try:
        # The film disambiguates songs that share a title (1973 vs 2019 "Pal Pal").
        info = _extract(f"ytsearch5:{' '.join(p for p in (song, film) if p)} song",
                        _base_opts() | {"extract_flat": True,
                                                                  "noplaylist": False})
    except Exception as e:
        return None, fail(classify_error(str(e)), str(e))
    pool = []
    for i, e in enumerate(info.get("entries") or []):
        vid, title = (e or {}).get("id") or "", (e or {}).get("title") or ""
        low = unicodedata.normalize("NFKC", title).lower()
        if not VIDEO_ID_RE.match(vid) or any(w in low for w in SEED_SKIP_WORDS):
            continue
        if title_score(song, title) < 60:  # a different song entirely
            continue
        channel = e.get("channel") or e.get("uploader") or ""
        is_label = any(l in channel.lower() for l in LABEL_CHANNELS)
        pool.append((not is_label, i, {"video_id": vid, "title": title, "channel": channel,
                                       "url": watch_url(vid)}))
    if not pool:
        return None, fail("no_seed", f"No original upload of '{song}' found in the top 5 results.")
    pool.sort(key=lambda x: (x[0], x[1]))
    return pool[0][2], None


def _fetch_mix(video_id: str, which: str, cap: int) -> tuple[list[dict], str]:
    """Flat-list one auto-generated mix. Returns (entries, error message)."""
    url = (f"https://music.youtube.com/watch?v={video_id}&list=RDAMVM{video_id}"
           if which == "youtube-music" else
           f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}")
    opts = _base_opts() | {"extract_flat": "in_playlist", "noplaylist": False,
                           "playlistend": cap, "lazy_playlist": True}
    try:
        info = _extract(url, opts)
    except Exception as e:
        return [], str(e)
    entries = [e for e in (info.get("entries") or []) if e and VIDEO_ID_RE.match(e.get("id") or "")]
    return entries[:cap], ("" if entries else "Mix returned no videos.")


def youtube_mix(song: str = "", url: str = "", seed: str = "karaoke", source: str = "youtube",
                limit: int = 25, film: str = "") -> dict:
    song, url = (song or "").strip(), (url or "").strip()
    seed, source = (seed or "karaoke").strip().lower(), (source or "youtube").strip().lower()
    if not song and not url:
        return fail("invalid_input", "Give a song name or a YouTube video url.")
    if seed not in MIX_SEEDS:
        return fail("invalid_input", f"seed must be one of {', '.join(MIX_SEEDS)}.")
    if source not in MIX_SOURCES:
        return fail("invalid_input", f"source must be one of {', '.join(MIX_SOURCES)}.")
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return fail("invalid_input", "limit must be a whole number.")

    seed_info, err = _find_seed(song, url, seed, (film or "").strip())
    if err:
        return err
    sid = seed_info["video_id"]

    wanted = ["youtube", "youtube-music"] if source == "both" else [source]
    lists, errors = {}, {}
    for which in wanted:
        entries, msg = _fetch_mix(sid, which, limit + 5)
        if entries:
            lists[which] = entries
        else:
            errors[which] = msg[:200]
    if not lists:
        return fail("mix_unavailable", "; ".join(f"{k}: {v}" for k, v in errors.items())
                    or "No mix for this video.", seed=seed_info)

    # Merge sources by video id; rank = best position in any source (YouTube first on ties).
    videos: dict[str, dict] = {}
    for which in wanted:
        for pos, e in enumerate(lists.get(which, [])):
            vid = e["id"]
            if vid == sid:
                if not seed_info["title"]:
                    seed_info["title"] = e.get("title") or ""
                    seed_info["channel"] = e.get("channel") or e.get("uploader") or ""
                continue
            v = videos.setdefault(vid, {"video_id": vid, "url": watch_url(vid),
                                        "title": re.sub(r"\s+", " ", e.get("title") or "").strip(),
                                        "channel": e.get("channel") or e.get("uploader") or "",
                                        "duration_sec": int(e["duration"]) if e.get("duration") else None,
                                        "sources": [], "pos": (pos, wanted.index(which))})
            if which not in v["sources"]:
                v["sources"].append(which)
            v["pos"] = min(v["pos"], (pos, wanted.index(which)))

    names, films = _known_names(), _known_films()
    seed_song, _ = parse_song_title(seed_info["title"], names, films) if seed_info["title"] else (song, "")
    seed_info["song_title"] = seed_song

    entries: list[dict] = []
    for v in sorted(videos.values(), key=lambda v: v["pos"]):
        kind = classify_kind(v["title"], v["duration_sec"])
        song_title, film = parse_song_title(v["title"], names, films)
        item = {"url": v["url"], "title": v["title"], "channel": v["channel"],
                "duration_sec": v["duration_sec"], "kind": kind, "sources": v["sources"]}
        if kind != "jukebox" and seed_song and _same_song(seed_song, song_title):
            continue  # another upload of the seed song
        target = None
        if kind != "jukebox":
            target = next((e for e in entries if e["kind"] != "jukebox"
                           and _same_song(e["song_title"], song_title)), None)
        if target:
            target["videos"].append(item)
            target["sources"] = sorted(set(target["sources"]) | set(v["sources"]))
            target["film"] = target["film"] or film
            if KIND_ORDER[kind] < KIND_ORDER[target["kind"]]:
                target["kind"] = kind
            continue
        entries.append({"rank": 0, "song_title": song_title, "film": film, "kind": kind,
                        "sources": list(v["sources"]), "videos": [item]})

    # Catalog matches (read-only).
    notes = [f"{k} mix unavailable: {v}" for k, v in errors.items()]
    try:
        songs = catalog.load_songs()
    except Exception as e:
        songs = []
        notes.append(f"Catalog not read: {e}")
    for rank, e in enumerate(entries[:limit], 1):
        e["rank"] = rank
        e["in_catalog"], e["status"], e["match_score"] = "", "", 0.0
        if e["kind"] == "jukebox" or not songs:
            continue
        text = normalize_title(e["song_title"])
        want_version = "unplugged" if e["kind"] == "cover" and "unplugged" in \
            " ".join(x["title"].lower() for x in e["videos"]) else ""
        def film_adj(row):
            # Same film +10. A different film only -5: films parsed from video
            # titles can be wrong, and must not veto a strong title match.
            if not e["film"] or not row["film"]:
                return 0
            return 10 if fuzz.ratio(normalize_title(e["film"]), normalize_title(row["film"])) >= 85 else -5
        scored = sorted(((match_score(s["title"], text) + film_adj(s)
                          + (1 if _row_version(s["title"]) == want_version else 0), s) for s in songs),
                        key=lambda x: -x[0])
        if scored and scored[0][0] >= MATCH_SCORE:
            sc, s = scored[0]
            e["in_catalog"], e["status"] = s["song_id"], s["status"]
            e["match_score"] = round(min(sc, 100.0), 1)
        elif scored:
            e["match_score"] = round(scored[0][0], 1)
    out = entries[:limit]
    return ok(seed=seed_info, source=source, fetched=len(videos) + 1, returned=len(out),
              in_catalog=sum(1 for e in out if e["in_catalog"]), entries=out, notes=notes)
