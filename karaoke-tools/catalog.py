"""catalog/songs.csv, catalog/review.csv, lyrics/, events/ and log.md.

Rules (from SCHEMA.md):
- UTF-8, header unquoted, every text field double-quoted; numbers bare.
- Multi-value fields joined with ';' (no spaces). Empty means unknown.
- songs.csv sorted by song_id; whole-file atomic writes (temp + os.replace).
- song_id is permanent: ascii slug of title + -<year>, + -2, -3 on collision.
- Every change appends a line to log.md.
"""

import csv
import os
import re
import tempfile
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path

from rapidfuzz import fuzz, process

import vocab
from common import PathError, SchemaError, fail, is_youtube_url, ok, resolve_inside, url_host, wiki_root

SONGS_REL = "catalog/songs.csv"
REVIEW_REL = "catalog/review.csv"
LOG_REL = "log.md"
README_REL = "README.md"

CHUNK_LIMIT = 3500  # Telegram allows 4096 per message; leave headroom.
MAX_LYRICS_CHARS = 100_000

SONG_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EMOTION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT = 2000

COMPACT_FIELDS = ["song_id", "title", "film", "year", "singers", "voice_type", "tempo_class",
                  "bpm", "energy", "mood", "karaoke_url", "status", "my_key_change"]
STATUS_RANK = {"ready": 0, "practicing": 1}

# One lock for every read-modify-write: MCP may run sync tools in threads.
_lock = threading.RLock()


# --- low-level file IO -----------------------------------------------------------

def wiki_path(rel: str) -> Path:
    """A path inside WIKI_ROOT. Rejects '..' and symlinks that escape."""
    root = wiki_root()
    return resolve_inside(rel, [root], base=root)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def render_csv(columns: list[str], rows: list[dict], numeric: set[str] = frozenset()) -> str:
    """Header bare; text fields always quoted; non-empty numeric fields bare."""
    lines = [",".join(columns)]
    for row in rows:
        cells = []
        for col in columns:
            v = str(row.get(col, "") or "")
            cells.append(v if (col in numeric and v) else _quote(v))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_csv(path: Path, columns: list[str]) -> list[dict]:
    if not path.exists():
        raise SchemaError(f"{path.name} not found in the wiki.")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != columns:
            raise SchemaError(f"{path.name} header does not match SCHEMA.md "
                              f"(expected {len(columns)} columns: {','.join(columns)}).")
        return [{c: (r.get(c) or "") for c in columns} for r in reader]


def load_songs() -> list[dict]:
    return read_csv(wiki_path(SONGS_REL), vocab.SONG_COLUMNS)


def save_songs(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: r["song_id"])
    atomic_write(wiki_path(SONGS_REL),
                 render_csv(vocab.SONG_COLUMNS, rows, vocab.NUMERIC_COLUMNS))


def load_review() -> list[dict]:
    return read_csv(wiki_path(REVIEW_REL), vocab.REVIEW_COLUMNS)


def save_review(rows: list[dict]) -> None:
    atomic_write(wiki_path(REVIEW_REL), render_csv(vocab.REVIEW_COLUMNS, rows))


def append_log(action: str, ref: str, details: str = "") -> str:
    """Append `YYYY-MM-DD HH:MM | action | ref | details` to log.md."""
    def clean(s):
        return re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()

    line = f"{datetime.now():%Y-%m-%d %H:%M} | {clean(action)} | {clean(ref)} | {clean(details)}"
    path = wiki_path(LOG_REL)
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Change log\n"
    sep = "" if existing.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(sep + line + "\n")
    return line


# --- ids ------------------------------------------------------------------------------

def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def make_song_id(title: str, year: str, existing: set[str]) -> str:
    base = slugify(title) or "song"
    if year:
        base = f"{base}-{year}"
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


# --- validation -------------------------------------------------------------------------

def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _split_multi(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else _as_text(value).split(";")
    out = []
    for item in items:
        item = _as_text(item)
        if item and item not in out:
            out.append(item)
    return out


def normalize_field(col: str, value, errors: list[str], warnings: list[str]) -> str:
    """Validate and normalize one field. Appends to errors on bad input."""
    if isinstance(value, (list, tuple)) and col not in vocab.MULTI_ENUM_FIELDS \
            and col not in vocab.MULTI_FREE_FIELDS:
        errors.append(f"{col}: expected a single value, got a list.")
        return ""
    if col in vocab.MULTI_ENUM_FIELDS or col in vocab.MULTI_FREE_FIELDS:
        items = [i for i in _split_multi(value) if i.lower() not in vocab.UNKNOWN_PLACEHOLDERS]
    else:
        text = _as_text(value)
        if text.lower() in vocab.UNKNOWN_PLACEHOLDERS:
            warnings.append(f"{col}: '{text}' stored as empty (empty means unknown).")
            return ""
        if not text:
            return ""

    if col in vocab.ENUM_FIELDS:
        v = text.lower()
        if v not in vocab.ENUM_FIELDS[col]:
            errors.append(f"{col}: '{text}' is not allowed. Allowed: {', '.join(vocab.ENUM_FIELDS[col])}.")
            return ""
        return v
    if col in vocab.MULTI_ENUM_FIELDS:
        allowed = vocab.MULTI_ENUM_FIELDS[col]
        vals = [i.lower() for i in items]
        bad = [v for v in vals if v not in allowed]
        if bad:
            errors.append(f"{col}: {', '.join(repr(b) for b in bad)} not allowed. "
                          f"Allowed: {', '.join(allowed)}.")
            return ""
        return ";".join(dict.fromkeys(vals))
    if col == "emotion":
        vals = [i.lower() for i in items]
        bad = [v for v in vals if not EMOTION_RE.match(v) or len(v) > 30]
        if bad:
            errors.append(f"emotion: {', '.join(repr(b) for b in bad)} must be short "
                          f"lowercase-hyphenated words, e.g. {', '.join(vocab.EMOTION_SUGGESTED[:5])}.")
            return ""
        new = [v for v in vals if v not in vocab.EMOTION_SUGGESTED]
        if new:
            warnings.append(f"emotion: {', '.join(new)} not in SCHEMA.md's examples (kept).")
        return ";".join(dict.fromkeys(vals))
    if col == "karaoke_alt_urls":
        bad = [u for u in items if not is_youtube_url(u)]
        if bad:
            errors.append(f"karaoke_alt_urls: not YouTube URLs: {', '.join(bad)}.")
            return ""
        if len(items) > 2:
            errors.append("karaoke_alt_urls: at most 2 backup links.")
            return ""
        return ";".join(items)
    if col in vocab.MULTI_FREE_FIELDS:
        too_long = [i for i in items if len(i) > 120]
        if too_long:
            errors.append(f"{col}: value too long.")
            return ""
        return ";".join(items)
    if col in vocab.INT_FIELDS:
        lo, hi = vocab.INT_FIELDS[col]
        if col == "year":
            hi = date.today().year + 1
        try:
            n = int(float(text)) if re.fullmatch(r"[+-]?\d+(\.0+)?", text) else None
        except ValueError:
            n = None
        if n is None:
            errors.append(f"{col}: '{text}' is not a whole number.")
            return ""
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            errors.append(f"{col}: {n} is out of range ({lo}..{hi if hi is not None else ''}).")
            return ""
        return str(n)
    if col in vocab.NUMBER_FIELDS:
        lo, hi = vocab.NUMBER_FIELDS[col]
        try:
            x = float(text)
        except ValueError:
            errors.append(f"{col}: '{text}' is not a number.")
            return ""
        if not lo <= x <= hi:
            errors.append(f"{col}: {x} is out of range ({lo}..{hi}).")
            return ""
        return f"{x:.1f}" if col == "bpm" else f"{round(x, 2):g}"
    if col in vocab.DATE_FIELDS:
        try:
            if not DATE_RE.match(text):
                raise ValueError
            date.fromisoformat(text)
        except ValueError:
            errors.append(f"{col}: '{text}' must be a YYYY-MM-DD date.")
            return ""
        return text
    if col in vocab.BOOL_FIELDS:
        v = text.lower()
        if v in ("true", "yes", "1"):
            return "true"
        if v in ("false", "no", "0"):
            return "false"
        errors.append(f"{col}: '{text}' must be true or false.")
        return ""
    if col == "karaoke_url":
        if not is_youtube_url(text):
            errors.append(f"karaoke_url: '{text}' is not a YouTube URL.")
            return ""
        return text
    if col == "lyrics_url":
        if not url_host(text):
            errors.append(f"lyrics_url: '{text}' is not an http(s) URL.")
            return ""
        return text
    if col == "song_id":
        if not SONG_ID_RE.match(text):
            errors.append(f"song_id: '{text}' must be a lowercase ascii slug like pal-pal-dil-ke-paas-1973.")
            return ""
        return text
    if col == "lyrics_file":
        return text  # checked against song_id in validate_row
    # free text
    if len(text) > MAX_TEXT:
        errors.append(f"{col}: longer than {MAX_TEXT} characters.")
        return ""
    if col != "notes" and "\n" in text:
        text = re.sub(r"\s*\n\s*", " ", text)
    return text


def validate_row(row: dict) -> tuple[dict, list[str], list[str]]:
    """Validate a full row (all columns present). Returns (row, errors, warnings)."""
    errors, warnings = [], []
    out = {c: normalize_field(c, row.get(c, ""), errors, warnings) for c in vocab.SONG_COLUMNS}
    if not out["title"]:
        errors.append("title: required.")
    if out["year"]:
        era = vocab.era_for_year(int(out["year"]))
        if era not in vocab.ERA:
            errors.append(f"year: {out['year']} has no era in SCHEMA.md.")
        elif out["era"] and out["era"] != era:
            warnings.append(f"era: '{out['era']}' does not match year {out['year']}; set to {era}.")
        out["era"] = era if era in vocab.ERA else out["era"]
    if out["lyrics_file"] and out["lyrics_file"] != f"lyrics/{out['song_id']}.txt":
        errors.append(f"lyrics_file: must be lyrics/{out['song_id']}.txt or empty.")
    return out, errors, warnings


# --- songs: upsert / get / search ------------------------------------------------------

def _to_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[;,]", value) if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _possible_duplicates(row: dict, rows: list[dict]) -> list[str]:
    t, f = slugify(row["title"]), slugify(row["film"])
    return [r["song_id"] for r in rows
            if slugify(r["title"]) == t and (not f or not r["film"] or slugify(r["film"]) == f)]


def catalog_upsert(row: dict) -> dict:
    if not isinstance(row, dict) or not row:
        return fail("invalid_input", "row must be a non-empty object of column: value.")
    unknown = [k for k in row if k not in vocab.SONG_COLUMNS]
    if unknown:
        return fail("unknown_column", f"Unknown columns: {', '.join(unknown)}. "
                    f"Columns: {', '.join(vocab.SONG_COLUMNS)}.")
    with _lock:
        rows = load_songs()
        by_id = {r["song_id"]: r for r in rows}
        given_id = _as_text(row.get("song_id")).lower()
        warnings: list[str] = []

        if given_id and given_id in by_id:
            action = "update"
            old = by_id[given_id]
            merged = old | {k: v for k, v in row.items() if k != "song_id"}
        else:
            action = "add"
            merged = {c: "" for c in vocab.SONG_COLUMNS} | row
            title = _as_text(row.get("title"))
            if not title:
                return fail("validation_failed", "title: required for a new song.",
                            errors=["title: required for a new song."])
            if given_id:
                if not SONG_ID_RE.match(given_id):
                    return fail("validation_failed", f"song_id '{given_id}' is not a valid slug.")
                merged["song_id"] = given_id
                warnings.append(f"song_id '{given_id}' did not exist; created a new song with it.")
            else:
                year_errs: list[str] = []
                year = normalize_field("year", row.get("year"), year_errs, [])
                merged["song_id"] = make_song_id(title, year, set(by_id))
            if not _as_text(merged.get("added_on")):
                merged["added_on"] = date.today().isoformat()
            if not _as_text(merged.get("status")):
                added_from = _as_text(merged.get("added_from")).lower()
                merged["status"] = "practicing" if added_from == "local-mp3" else "wishlist"

        new_row, errors, vwarn = validate_row(merged)
        warnings += vwarn
        if errors:
            return fail("validation_failed", "; ".join(errors)[:500], errors=errors,
                        warnings=warnings)

        if action == "update":
            changed = [c for c in vocab.SONG_COLUMNS if new_row[c] != old[c]]
            if not changed:
                return ok(action="unchanged", song_id=given_id, changed=[], row=new_row,
                          warnings=warnings)
            by_id[given_id] = new_row
            details = ", ".join(changed)
        else:
            dups = _possible_duplicates(new_row, rows)
            if dups:
                warnings.append(f"Possible duplicate of: {', '.join(dups)}. "
                                "Pass song_id to update an existing song instead.")
            changed = [c for c in vocab.SONG_COLUMNS if new_row[c]]
            by_id[new_row["song_id"]] = new_row
            details = f"{new_row['title']} ({new_row['film'] or '?'} {new_row['year']})".strip()

        save_songs(list(by_id.values()))
        append_log(action, new_row["song_id"], details)
        return ok(action=action, song_id=new_row["song_id"], changed=changed, row=new_row,
                  warnings=warnings)


def compact(row: dict) -> dict:
    return {k: row[k] for k in COMPACT_FIELDS}


def catalog_get(ref: str) -> dict:
    ref = _as_text(ref)
    if not ref:
        return fail("invalid_input", "ref must be a song_id or a title.")
    rows = load_songs()
    for r in rows:
        if r["song_id"] == ref.lower():
            return ok(found=True, row=r, matches=[])
    choices = {r["song_id"]: f"{r['title']} {r['film']}" for r in rows}
    hits = process.extract(ref, choices, scorer=fuzz.WRatio, limit=5,
                           processor=lambda s: s.lower())
    by_id = {r["song_id"]: r for r in rows}
    matches = [{"song_id": key, "title": by_id[key]["title"], "film": by_id[key]["film"],
                "year": by_id[key]["year"], "score": round(score, 1)}
               for _, score, key in hits]
    return ok(found=False, row=None, matches=matches)


def catalog_search(text="", status=None, voice_type=None, tempo_class=None, era=None,
                   genre=None, mood=None, theme=None, situation=None, singer="",
                   min_energy=1, max_energy=5, exclude_ids=None, limit=15,
                   full=False) -> dict:
    filters = {"status": status, "voice_type": voice_type, "tempo_class": tempo_class,
               "era": era, "genre": genre, "mood": mood, "theme": theme,
               "situation": situation}
    wanted: dict[str, set[str]] = {}
    for col, value in filters.items():
        vals = {v.lower() for v in _to_list(value)}
        allowed = vocab.ENUM_FIELDS.get(col) or vocab.MULTI_ENUM_FIELDS[col]
        bad = sorted(vals - set(allowed))
        if bad:
            return fail("invalid_filter", f"{col}: {', '.join(bad)} not allowed. "
                        f"Allowed: {', '.join(allowed)}.")
        if vals:
            wanted[col] = vals
    try:
        min_energy, max_energy = int(min_energy), int(max_energy)
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return fail("invalid_filter", "min_energy, max_energy and limit must be whole numbers.")
    exclude = set(_to_list(exclude_ids))
    text = _as_text(text).lower()
    singer = _as_text(singer).lower()
    energy_filtered = (min_energy, max_energy) != (1, 5)

    matches = []
    for r in load_songs():
        if r["song_id"] in exclude:
            continue
        if any(not (set(r[col].split(";")) & vals) for col, vals in wanted.items()):
            continue
        if energy_filtered:
            if not r["energy"] or not min_energy <= int(r["energy"]) <= max_energy:
                continue
        if singer and not any(singer in s.lower() or fuzz.partial_ratio(singer, s.lower()) >= 85
                              for s in r["singers"].split(";") if s):
            continue
        score = None
        if text:
            fields = [r["title"], r["film"], r["singers"].replace(";", " ")]
            score = max(fuzz.WRatio(text, f.lower()) for f in fields if f) if any(fields) else 0
            if score < 70:
                continue
        matches.append((r, score))

    def sort_key(item):
        r, score = item
        return (STATUS_RANK.get(r["status"], 2),
                -float(r["crowd_score"] or 0),
                -int(r["singalong"] or 0),
                -(score or 0),
                r["title"].lower())

    matches.sort(key=sort_key)
    out = []
    for r, score in matches[:limit]:
        item = dict(r) if full else compact(r)
        if score is not None:
            item["match_score"] = round(score, 1)
        out.append(item)
    return ok(total=len(matches), returned=len(out), rows=out)


# --- stats + README ----------------------------------------------------------------

QUICK_STATS_RE = re.compile(r"(## Quick stats\n)(.*?)(?=\n## |\Z)", re.S)


def _event_files() -> list[Path]:
    events_dir = wiki_path("events")
    if not events_dir.is_dir():
        return []
    return sorted(p for p in events_dir.glob("*.md")
                  if not p.name.startswith("_") and p.is_file() and not p.is_symlink())


def catalog_stats() -> dict:
    with _lock:
        rows = load_songs()
        reviews = load_review()

        def counts(col):
            c: dict[str, int] = {}
            for r in rows:
                c[r[col] or "(empty)"] = c.get(r[col] or "(empty)", 0) + 1
            return dict(sorted(c.items()))

        active = [r for r in rows if r["status"] != "retired"]
        missing_lyrics = [r["song_id"] for r in active if not r["lyrics_file"]]
        missing_links = [r["song_id"] for r in active if not r["karaoke_url"]]
        n_events = len(_event_files())
        by_status = counts("status")
        stats = {
            "songs": len(rows),
            "by_status": by_status,
            "by_voice_type": counts("voice_type"),
            "by_era": counts("era"),
            "by_tempo_class": counts("tempo_class"),
            "open_reviews": sum(1 for r in reviews if r["resolved"] != "true"),
            "events_logged": n_events,
            "missing_lyrics": len(missing_lyrics),
            "missing_lyrics_ids": missing_lyrics[:30],
            "missing_links": len(missing_links),
            "missing_links_ids": missing_links[:30],
        }

        readme_updated = False
        readme = wiki_path(README_REL)
        if readme.exists():
            text = readme.read_text(encoding="utf-8")
            body = (
                "\nKept up to date by the agent after bulk changes.\n\n"
                f"- Songs: {len(rows)} (ready: {by_status.get('ready', 0)}, "
                f"practicing: {by_status.get('practicing', 0)}, "
                f"wishlist: {by_status.get('wishlist', 0)})\n"
                f"- Events logged: {n_events}\n"
            )
            m = QUICK_STATS_RE.search(text)
            if m:
                old_body = re.sub(r"- Last updated: .*\n?", "", m.group(2))
                if old_body.rstrip("\n") != body.rstrip("\n"):
                    new_section = m.group(1) + body + f"- Last updated: {date.today().isoformat()}\n"
                    atomic_write(readme, text[:m.start()] + new_section + text[m.end():])
                    append_log("stats", "README.md", f"{len(rows)} songs, {n_events} events")
                    readme_updated = True
        return ok(**stats, readme_updated=readme_updated)


# --- review.csv -----------------------------------------------------------------------

def _set_needs_review(song_id: str, value: str) -> bool:
    rows = load_songs()
    for r in rows:
        if r["song_id"] == song_id:
            if r["needs_review"] == value:
                return False
            r["needs_review"] = value
            save_songs(rows)
            return True
    return False


def review_add(song_id: str, source_ref: str, problem: str, details: str = "") -> dict:
    problem = _as_text(problem).lower()
    if problem not in vocab.REVIEW_PROBLEM:
        return fail("validation_failed", f"problem '{problem}' not allowed. "
                    f"Allowed: {', '.join(vocab.REVIEW_PROBLEM)}.")
    song_id = _as_text(song_id).lower()
    if song_id and not SONG_ID_RE.match(song_id):
        return fail("validation_failed", f"song_id '{song_id}' is not a valid slug.")
    source_ref, details = _as_text(source_ref), re.sub(r"\s+", " ", _as_text(details))
    if not source_ref and not song_id:
        return fail("validation_failed", "Give a song_id or a source_ref.")
    with _lock:
        reviews = load_review()
        known = {r["song_id"] for r in load_songs()}
        row = {"logged_at": f"{datetime.now():%Y-%m-%d %H:%M}", "song_id": song_id,
               "source_ref": source_ref[:500], "problem": problem,
               "details": details[:MAX_TEXT], "resolved": "false"}
        reviews.append(row)
        save_review(reviews)
        flagged = _set_needs_review(song_id, "true") if song_id in known else False
        append_log("review-add", song_id or source_ref, f"{problem}: {details}"[:200])
        warnings = [] if not song_id or song_id in known else \
            [f"song_id '{song_id}' is not in the catalog yet."]
        return ok(row_number=len(reviews), row=row, song_flagged=flagged, warnings=warnings)


def review_list(open_only: bool = True) -> dict:
    reviews = load_review()
    items = [{"row_number": i} | r for i, r in enumerate(reviews, 1)
             if not open_only or r["resolved"] != "true"]
    return ok(count=len(items), items=items)


def review_resolve(row_number: int) -> dict:
    with _lock:
        reviews = load_review()
        try:
            idx = int(row_number) - 1
        except (TypeError, ValueError):
            return fail("invalid_input", "row_number must be a whole number.")
        if not 0 <= idx < len(reviews):
            return fail("not_found", f"No review row {row_number} (there are {len(reviews)}).")
        row = reviews[idx]
        if row["resolved"] == "true":
            return ok(row_number=idx + 1, row=row, note="already resolved", song_unflagged=False)
        row["resolved"] = "true"
        save_review(reviews)
        unflagged = False
        sid = row["song_id"]
        if sid and not any(r["song_id"] == sid and r["resolved"] != "true" for r in reviews):
            unflagged = _set_needs_review(sid, "false")
        append_log("review-resolve", sid or row["source_ref"], f"row {idx + 1}: {row['problem']}")
        return ok(row_number=idx + 1, row=row, song_unflagged=unflagged)


# --- lyrics files ------------------------------------------------------------------------

def _clean_lyrics(text: str) -> str:
    text = _as_text(text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and lines[0].startswith("# "):  # drop a header the caller pasted back in
        lines.pop(0)
    text = "\n".join(lines).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split at verse boundaries (blank lines) into chunks shorter than `limit`.
    A verse that is itself too long is split by lines, a line by characters."""
    pieces: list[str] = []
    for verse in re.split(r"\n\s*\n", text.strip()):
        if len(verse) < limit:
            pieces.append(verse)
            continue
        buf = ""
        for line in verse.split("\n"):
            while len(line) >= limit:
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(line[:limit - 1])
                line = line[limit - 1:]
            if buf and len(buf) + 1 + len(line) >= limit:
                pieces.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            pieces.append(buf)
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + 2 + len(piece) < limit:
            chunks[-1] += "\n\n" + piece
        else:
            chunks.append(piece)
    return [c for c in chunks if c.strip()]


def lyrics_save(song_id: str, text: str, source: str, url: str = "", chords: str = "") -> dict:
    song_id = _as_text(song_id).lower()
    source = _as_text(source).lower()
    if source not in vocab.LYRICS_SOURCE:
        return fail("validation_failed", f"source '{source}' not allowed. "
                    f"Allowed: {', '.join(vocab.LYRICS_SOURCE)}.")
    url = _as_text(url)
    if url and not url_host(url):
        return fail("validation_failed", f"url '{url}' is not an http(s) URL.")
    body = _clean_lyrics(text)
    chords = _as_text(chords).strip("\n")
    if not body:
        return fail("validation_failed", "Lyrics text is empty.")
    if len(body) + len(chords) > MAX_LYRICS_CHARS:
        return fail("validation_failed", f"Lyrics longer than {MAX_LYRICS_CHARS} characters.")
    with _lock:
        rows = load_songs()
        row = next((r for r in rows if r["song_id"] == song_id), None)
        if row is None:
            return fail("not_found", f"song_id '{song_id}' is not in the catalog.")
        rel = f"lyrics/{song_id}.txt"
        path = wiki_path(rel)
        film_year = f"{row['film']} ({row['year']})" if row["year"] else row["film"]
        meta = " | ".join(p for p in [film_year, ", ".join(row["singers"].split(";")) if row["singers"] else "",
                                       f"source: {source}", url] if p)
        content = f"# {row['title']}\n# {meta}\n\n{body}\n"
        if chords and "## Chords" not in body:
            content += f"\n## Chords\n\n{chords}\n"
        existed = path.exists()
        atomic_write(path, content)
        row.update(lyrics_file=rel, lyrics_source=source, lyrics_url=url)
        save_songs(rows)
        append_log("lyrics", song_id, f"{'replaced' if existed else 'saved'} from {source}")
        return ok(song_id=song_id, lyrics_file=rel, chars=len(content), replaced=existed)


def lyrics_get(song_id: str) -> dict:
    song_id = _as_text(song_id).lower()
    if not SONG_ID_RE.match(song_id):
        return fail("invalid_input", f"song_id '{song_id}' is not a valid slug.")
    path = wiki_path(f"lyrics/{song_id}.txt")
    if not path.exists():
        return fail("not_found", f"No lyrics file for {song_id}.")
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n")
    header = [ln[2:] for ln in lines[:2] if ln.startswith("# ")]
    body = "\n".join(lines[len(header):]).strip("\n")
    chords = ""
    m = re.search(r"^## Chords[^\n]*\n?", body, re.M)
    if m:
        body, chords = body[:m.start()].strip("\n"), body[m.end():].strip("\n")
    title = header[0] if header else ""
    shown = f"{title}\n\n{body}" if title else body
    return ok(song_id=song_id, title=title, meta=header[1] if len(header) > 1 else "",
              text=body, chords=chords, chunks=chunk_text(shown))


# --- events ------------------------------------------------------------------------------

def _parse_front_matter(text: str) -> dict:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    meta = {}
    if m:
        for line in m.group(1).split("\n"):
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.split("#", 1)[0].strip()
    return meta


def parse_event(path: Path) -> dict:
    """Return {date, rows: [{song_id, title, outcome}], warnings} for one event file."""
    text = path.read_text(encoding="utf-8")
    meta = _parse_front_matter(text)
    warnings = []
    event_date = meta.get("date", "")
    if not (DATE_RE.match(event_date or "") and _valid_date(event_date)):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        event_date = m.group(1) if m and _valid_date(m.group(1)) else ""
        if not event_date:
            warnings.append(f"{path.name}: no valid date in front matter or file name.")

    section = re.search(r"^## Set list\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    rows = []
    if not section:
        warnings.append(f"{path.name}: no '## Set list' section.")
        return {"date": event_date, "rows": rows, "warnings": warnings}
    table = [ln.strip() for ln in section.group(1).split("\n") if ln.strip().startswith("|")]
    if len(table) < 2:
        return {"date": event_date, "rows": rows, "warnings": warnings}

    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = [h.lower() for h in cells(table[0])]
    col = {name: header.index(name) for name in ("song_id", "title", "outcome") if name in header}
    if "song_id" not in col:
        warnings.append(f"{path.name}: set-list table has no song_id column.")
        return {"date": event_date, "rows": rows, "warnings": warnings}
    for line in table[1:]:
        if re.fullmatch(r"\|?[\s:|-]+\|?", line):  # separator row
            continue
        c = cells(line)
        get = lambda k: c[col[k]] if k in col and col[k] < len(c) else ""  # noqa: E731
        sid, title, outcome = get("song_id").strip("` ").lower(), get("title"), get("outcome").lower()
        if not sid and not title:
            continue
        if outcome and outcome not in vocab.OUTCOME_SCORES:
            if "/" not in outcome:  # the template placeholder "standout / good / weak"
                warnings.append(f"{path.name}: outcome '{outcome}' for {sid or title} is not "
                                f"standout, good or weak; ignored.")
            outcome = ""
        rows.append({"song_id": sid, "title": title, "outcome": outcome})
    return {"date": event_date, "rows": rows, "warnings": warnings}


def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def event_apply(event_file: str) -> dict:
    name = _as_text(event_file)
    if name.startswith("events/"):
        name = name[len("events/"):]
    if not name.endswith(".md") or "/" in name or name.startswith("_"):
        return fail("invalid_input", "event_file must be a file name like 2026-11-01-diwali.md "
                    "in events/ (not the template).")
    try:
        path = wiki_path(f"events/{name}")
    except PathError as e:
        return fail("path_not_allowed", str(e))
    if not path.is_file():
        return fail("not_found", f"events/{name} not found.")

    with _lock:
        target = parse_event(path)
        warnings = list(target["warnings"])
        rows = load_songs()
        by_id = {r["song_id"]: r for r in rows}
        unmatched = [{"song_id": r["song_id"], "title": r["title"]}
                     for r in target["rows"] if r["song_id"] not in by_id]
        target_ids = {r["song_id"] for r in target["rows"] if r["song_id"] in by_id}

        # Recompute from every event so re-running is idempotent.
        agg: dict[str, dict] = {sid: {"times": 0, "scores": [], "last": ""} for sid in target_ids}
        for ev_path in _event_files():
            ev = target if ev_path == path else parse_event(ev_path)
            for r in ev["rows"]:
                a = agg.get(r["song_id"])
                if a is None:
                    continue
                a["times"] += 1
                if r["outcome"]:
                    a["scores"].append(vocab.OUTCOME_SCORES[r["outcome"]])
                if ev["date"] and ev["date"] > a["last"]:
                    a["last"] = ev["date"]

        changes = []
        for sid in sorted(target_ids):
            a, row = agg[sid], by_id[sid]
            new = {
                "times_performed": str(a["times"]),
                "crowd_score": f"{round(sum(a['scores']) / len(a['scores']), 2):g}" if a["scores"] else "",
                "last_performed": a["last"],
            }
            diff = {k: {"from": row[k], "to": v} for k, v in new.items() if row[k] != v}
            if diff:
                row.update(new)
                changes.append({"song_id": sid, "title": row["title"], "changes": diff})
        if changes:
            save_songs(rows)
            append_log("event", f"events/{name}", f"updated {len(changes)} songs")
        return ok(event_file=f"events/{name}", event_date=target["date"],
                  set_list_rows=len(target["rows"]), updated=changes,
                  unmatched=unmatched, warnings=warnings)
