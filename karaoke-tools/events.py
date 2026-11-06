"""Event files (events/<date>-<slug>.md) for the Mini App's Event Planner.

The files keep the wiki template's shape, so the chat agent and event_apply
read them as before. The app owns and rewrites only:
- the front matter (template keys plus time, song_count, theme, instructions,
  parsed_filters, hidden, status);
- the table in "## Set list" (text above and below the table is kept);
- the bullet lists in "## Audience requests" and "## What worked / what I'd change".
Every other section (Backups, Songs sung tonight, ...) is kept verbatim, and
set-list rows keep their original cells (Title, Link used, Notes) unless the
app changes them.

Status lifecycle: draft (being built, autosaved) -> planned (Create) ->
performed (feedback finished; event_apply has run). A file without a status
is an older agent-written event.

All writes go through the shared lock (locks.py) and are logged in log.md.
"""

import json
import re
from datetime import date, datetime

import catalog
import feedback
import locks
import suggest
import vocab
import youtube
from common import fail, ok

EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
STATUSES = ("draft", "planned", "performed")
EVENT_TYPES = ["festival", "wedding", "sangeet", "birthday", "house-party", "community",
               "open-mic", "paid-gig", "other"]
OUTCOMES = tuple(vocab.OUTCOME_SCORES)  # standout, good, weak
OUTCOME_PLACEHOLDER = "standout / good / weak"
FRONT_KEYS = ["event", "date", "time", "venue", "event_type", "audience", "duration_min",
              "song_count", "co_singers", "theme", "instructions", "parsed_filters", "hidden",
              "status"]
JSON_KEYS = {"parsed_filters"}
SET_COLUMNS = ["#", "song_id", "Title", "Key change", "Link used", "Outcome", "Skipped",
               "Comment", "Notes"]
SET_LIST = "Set list"
REQUESTS = "Audience requests"
NOTES = "What worked / what I'd change"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
YT_KEY_RE = re.compile(r"^yt:[A-Za-z0-9_-]{11}$")
CHANGEOVER_SEC = 45     # between songs, for the time estimate
DEFAULT_SONG_SEC = 270  # songs with no duration_sec yet
MAX_TEXT = 1000
_lock = locks.LOCK


# --- files ---------------------------------------------------------------------------------

def path_for(event_id: str):
    if not EVENT_ID_RE.match(event_id or "") or event_id.startswith("_"):
        raise ValueError("bad event id")
    return catalog.wiki_path(f"events/{event_id}.md")


def event_ids() -> list[str]:
    return [p.stem for p in catalog._event_files() if EVENT_ID_RE.match(p.stem)]


def _clean(text, limit: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _cell(text) -> str:
    return _clean(text).replace("|", "/")


# --- parsing --------------------------------------------------------------------------------

def parse_front(text: str) -> tuple[dict, str]:
    """(front matter, rest). Values may be JSON (quoted strings, objects);
    otherwise a ' # comment' is dropped, like the template's event_type line."""
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).split("\n"):
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if value[:1] in ('"', "{", "["):
            try:
                meta[key.strip()] = json.loads(value)
                continue
            except ValueError:
                pass
        meta[key.strip()] = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return meta, text[m.end():]


def split_sections(body: str) -> tuple[str, list[list[str]]]:
    """(text before the first '## ', [[heading, text], ...])."""
    parts = re.split(r"^## (.*)\n?", body, flags=re.M)
    pre, sections = parts[0], []
    for i in range(1, len(parts), 2):
        sections.append([parts[i].strip(), parts[i + 1]])
    return pre, sections


def _table_block(text: str) -> tuple[str, list[str], str]:
    """(text before the table, table lines, text after) in a section."""
    lines = text.split("\n")
    idx = [i for i, ln in enumerate(lines) if ln.strip().startswith("|")]
    if not idx:
        return text, [], ""
    start = idx[0]
    end = start
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("|"):
        end += 1
    return "\n".join(lines[:start]), lines[start:end + 1], "\n".join(lines[end + 1:])


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _plain_title(cell: str) -> str:
    """'Pal Pal Dil Ke Paas (Blackmail, 1973, Kishore Kumar) · 4:10' -> the title."""
    t = re.split(r"\s+·\s+", cell)[0]
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    return t.strip()


def _song_key(sid: str, link: str, title: str) -> str:
    if sid:
        return sid
    vid = youtube.video_id_from_url(link) if link else ""
    if vid:
        return "yt:" + vid
    return "t:" + catalog.slugify(title)[:60]


def parse_set_list(text: str) -> list[dict]:
    _, table, _ = _table_block(text)
    if len(table) < 2:
        return []
    header = [h.lower() for h in _cells(table[0])]
    songs = []
    for line in table[1:]:
        if re.fullmatch(r"\|?[\s:|-]+\|?", line.strip()):
            continue
        c = _cells(line)
        cells = {header[i]: c[i] if i < len(c) else "" for i in range(len(header))}
        raw_sid = cells.get("song_id", "").strip("` ")
        sid = raw_sid if raw_sid.startswith("yt:") else raw_sid.lower()
        title_cell = cells.get("title", "")
        if not sid and not title_cell:
            continue
        outcome = cells.get("outcome", "").lower()
        link = cells.get("link used", "")
        songs.append({
            "key": _song_key(sid, link, title_cell),
            "title": _plain_title(title_cell) or sid,
            "title_cell": title_cell,
            "key_change": cells.get("key change", ""),
            "link": link,
            "outcome": outcome if outcome in OUTCOMES else "",
            "skipped": cells.get("skipped", "").lower() in ("yes", "true", "x", "✓", "skipped"),
            "comment": cells.get("comment", ""),
            "notes": cells.get("notes", ""),
        })
    return songs


def _bullets(text: str) -> list[str]:
    out = []
    for ln in text.split("\n"):
        m = re.match(r"\s*[-*]\s*(.*)$", ln)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def load(event_id: str) -> dict:
    """The event as a dict. Raises FileNotFoundError / ValueError."""
    p = path_for(event_id)
    text = p.read_text(encoding="utf-8")
    meta, body = parse_front(text)
    pre, sections = split_sections(body)
    sec = {h: t for h, t in sections}
    filters = meta.get("parsed_filters")
    return {
        "id": event_id, "meta": meta, "pre": pre, "sections": sections,
        "songs": parse_set_list(sec.get(SET_LIST, "")),
        "requests": _bullets(sec.get(REQUESTS, "")),
        "notes": _bullets(sec.get(NOTES, "")),
        "filters": filters if isinstance(filters, dict) else {},
        "hidden": [h for h in str(meta.get("hidden", "")).split(";") if h],
        "status": meta.get("status") if meta.get("status") in STATUSES else "",
    }


# --- rendering ------------------------------------------------------------------------------

def _front_value(key: str, value) -> str:
    if key in JSON_KEYS:
        return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
    v = _clean(value)
    if key == "event_type" and v:
        return f"{v:<15} # {' | '.join(EVENT_TYPES)}"
    if key in ("instructions", "theme") or "#" in v or v[:1] in ('"', "{", "["):
        return json.dumps(v, ensure_ascii=False) if v else ""
    return v


def render(ev: dict) -> str:
    meta = dict(ev["meta"])
    # Only write the app's keys once they have something in them.
    for key, value in (("parsed_filters", ev.get("filters") or {}), ("hidden", ";".join(ev.get("hidden") or []))):
        if value or key in meta:
            meta[key] = value
    if ev.get("status"):
        meta["status"] = ev["status"]
    keys = [k for k in FRONT_KEYS if k in meta] + [k for k in meta if k not in FRONT_KEYS]
    front = "\n".join(f"{k}: {_front_value(k, meta[k])}".rstrip() for k in keys)

    table = ["| " + " | ".join(SET_COLUMNS) + " |",
             "|" + "|".join("---" for _ in SET_COLUMNS) + "|"]
    for i, s in enumerate(ev["songs"], 1):
        sid = "" if s["key"].startswith("t:") else s["key"]
        table.append("|" + "|".join(f" {c} " if c else " " for c in [
            str(i), sid, _cell(s["title_cell"] or s["title"]), _cell(s["key_change"]), _cell(s["link"]),
            s["outcome"] or OUTCOME_PLACEHOLDER, "yes" if s["skipped"] else "", _cell(s["comment"]),
            _cell(s["notes"])]) + "|")

    sections = [list(x) for x in ev["sections"]]
    names = [h for h, _ in sections]
    for name in (SET_LIST, REQUESTS, NOTES):
        if name not in names:
            sections.append([name, "\n"])
            names.append(name)
    out = []
    for heading, text in sections:
        if heading == SET_LIST:
            before, _, after = _table_block(text)
            before = before.rstrip("\n")
            text = (before + "\n\n" if before.strip() else "\n") + "\n".join(table) + "\n" + \
                (after if after.strip() else "\n")
        elif heading in (REQUESTS, NOTES):
            items = ev["requests"] if heading == REQUESTS else ev["notes"]
            text = "\n" + "\n".join(f"- {_clean(x, 300)}" for x in items or [""]) + "\n\n"
        out.append(f"## {heading}\n{text}")
    pre = ev["pre"] if ev["pre"].strip() else f"\n# {_clean(meta.get('event', ev['id']))}\n\n"
    return f"---\n{front}\n---\n{pre}" + "".join(out).rstrip("\n") + "\n"


def save(ev: dict, what: str) -> None:
    with _lock:
        catalog.atomic_write(path_for(ev["id"]), render(ev))
        catalog.append_log("event-edit", f"events/{ev['id']}.md", what)


# --- create ----------------------------------------------------------------------------------

def validate_meta(fields: dict) -> tuple[dict, dict]:
    """Clean the editable front-matter fields. Returns (values, field_errors)."""
    out, errs = {}, {}
    if "name" in fields:
        name = _clean(fields["name"], 120)
        if not name:
            errs["name"] = "required."
        out["event"] = name
    if "date" in fields:
        d = _clean(fields["date"], 10)
        if not catalog.DATE_RE.match(d) or not catalog._valid_date(d):
            errs["date"] = "use YYYY-MM-DD."
        out["date"] = d
    if "time" in fields:
        t = _clean(fields["time"], 5)
        if t and not TIME_RE.match(t):
            errs["time"] = "use HH:MM (24 h)."
        out["time"] = t
    for key, lo, hi in (("duration_min", 5, 600), ("song_count", 1, 60)):
        if key in fields:
            v = str(fields[key] if fields[key] is not None else "").strip()
            if v and (not v.isdigit() or not lo <= int(v) <= hi):
                errs[key] = f"{lo}-{hi}."
            out[key] = v
    if "event_type" in fields:
        v = _clean(fields["event_type"], 20).lower()
        if v and v not in EVENT_TYPES:
            errs["event_type"] = "one of " + ", ".join(EVENT_TYPES) + "."
        out["event_type"] = v
    if "theme" in fields:
        vals = fields["theme"] if isinstance(fields["theme"], list) else str(fields["theme"]).split(";")
        out["theme"] = ";".join(dict.fromkeys(_clean(v, 40).lower() for v in vals if _clean(v)))
    for key in ("venue", "audience", "co_singers"):
        if key in fields:
            out[key] = _clean(fields[key], 200)
    if "instructions" in fields:
        out["instructions"] = _clean(fields["instructions"], MAX_TEXT)
    return out, errs


def create(fields: dict) -> dict:
    """A new draft event file. fields: name, date (required), time, theme, ..."""
    values, errs = validate_meta(fields)
    for req in ("name", "date"):
        if not values.get("event" if req == "name" else req):
            errs.setdefault(req, "required.")
    if errs:
        return fail("validation_failed", "Some fields need fixing.", field_errors=errs)
    slug = catalog.slugify(values["event"])[:50] or "event"
    with _lock:
        base = f"{values['date']}-{slug}"
        event_id, n = base, 2
        while path_for(event_id).exists():
            event_id, n = f"{base}-{n}", n + 1
        meta = {k: "" for k in ("event", "date", "time", "venue", "event_type", "audience",
                                "duration_min", "song_count", "co_singers", "theme", "instructions")}
        meta.update(values)
        ev = {"id": event_id, "meta": meta, "pre": f"\n# {values['event']}\n\n", "sections": [],
              "songs": [], "requests": [], "notes": [], "filters": {}, "hidden": [], "status": "draft"}
        path_for(event_id).parent.mkdir(parents=True, exist_ok=True)
        catalog.atomic_write(path_for(event_id), render(ev))
        catalog.append_log("event-new", f"events/{event_id}.md", values["event"])
    return ok(event_id=event_id)


# --- view helpers -------------------------------------------------------------------------

def is_past(ev: dict, today: date | None = None) -> bool:
    d = ev["meta"].get("date", "")
    return bool(d) and d < (today or date.today()).isoformat()


def display_status(ev: dict) -> str:
    if ev["status"]:
        return ev["status"]
    return "performed" if any(s["outcome"] for s in ev["songs"]) else "planned"


def editable(ev: dict) -> bool:
    """Performed events only take feedback changes, so history stays honest."""
    return display_status(ev) != "performed"


def _seconds(row: dict | None) -> tuple[int, bool]:
    try:
        return int(row["duration_sec"]), False
    except (TypeError, ValueError, KeyError):
        return DEFAULT_SONG_SEC, True


def summary(ev: dict, by_id: dict) -> dict:
    sung = [s for s in ev["songs"] if not s["skipped"]]
    secs, estimated, energy, duets = 0, False, [], 0
    for s in sung:
        row = by_id.get(s["key"])
        sec, est = _seconds(row)
        secs, estimated = secs + sec, estimated or est
        energy.append(int(row["energy"]) if row and row["energy"].isdigit() else None)
        if row and row["voice_type"].startswith("duet"):
            duets += 1
    total = secs + CHANGEOVER_SEC * len(sung)
    target = ev["meta"].get("duration_min") or (ev["filters"] or {}).get("target_min") or ""
    return {"songs": len(sung), "singing_sec": secs, "total_sec": total, "estimated": estimated,
            "target_min": int(target) if str(target).isdigit() else None,
            "song_count": int(ev["meta"]["song_count"]) if str(ev["meta"].get("song_count", "")).isdigit()
            else (ev["filters"] or {}).get("song_count"),
            "duets": duets, "duets_wanted": (ev["filters"] or {}).get("duets"), "energy": energy}


def past_performances(exclude_id: str = "") -> list[dict]:
    """[{id, name, date, event_type, theme, keys}] for events that happened."""
    out = []
    today = date.today().isoformat()
    for eid in event_ids():
        if eid == exclude_id:
            continue
        try:
            ev = load(eid)
        except (OSError, ValueError):
            continue
        d = ev["meta"].get("date", "")
        if ev["status"] in ("draft", "planned") or not d or d >= today:
            continue
        out.append({"id": eid, "name": ev["meta"].get("event", eid), "date": d,
                    "event_type": ev["meta"].get("event_type", ""),
                    "theme": set(filter(None, str(ev["meta"].get("theme", "")).split(";"))),
                    "keys": {s["key"] for s in ev["songs"] if not s["skipped"]}})
    return out


def _similar_event(ev: dict, past: dict) -> bool:
    et = ev["meta"].get("event_type", "")
    theme = set(filter(None, str(ev["meta"].get("theme", "")).split(";")))
    return bool(et and et == past["event_type"]) or bool(theme & past["theme"])


def song_warnings(ev: dict, s: dict, row: dict | None, past: list[dict], recent_days: int) -> list[str]:
    w = []
    if not row:
        w.append("outside catalog")
    elif row["status"] in ("practicing", "wishlist"):
        w.append(f"{row['status']} (unrehearsed)" if row["status"] == "wishlist" else "practicing")
    d = ev["meta"].get("date") or date.today().isoformat()
    for p in past:
        try:
            age = (date.fromisoformat(d) - date.fromisoformat(p["date"])).days
        except ValueError:
            continue
        if 0 <= age <= recent_days and s["key"] in p["keys"] and _similar_event(ev, p):
            w.append(f"sung at {p['name']} ({p['date']})")
            break
    return w


def list_events() -> dict:
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    upcoming, past = [], []
    for eid in event_ids():
        try:
            ev = load(eid)
        except (OSError, ValueError):
            continue
        sm = summary(ev, by_id)
        item = {"id": eid, "name": ev["meta"].get("event") or eid, "date": ev["meta"].get("date", ""),
                "time": ev["meta"].get("time", ""), "theme": ev["meta"].get("theme", ""),
                "songs": sm["songs"], "total_sec": sm["total_sec"], "estimated": sm["estimated"],
                "status": display_status(ev),
                "needs_feedback": is_past(ev) and display_status(ev) != "performed"}
        (past if is_past(ev) else upcoming).append(item)
    upcoming.sort(key=lambda e: (e["date"] or "9999", e["time"]))
    past.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
    return ok(upcoming=upcoming, past=past)


def latest_past() -> str:
    past = list_events()["past"]
    return past[0]["id"] if past else ""


# --- playlist edits -------------------------------------------------------------------------

def find(ev: dict, key: str) -> int:
    return next((i for i, s in enumerate(ev["songs"]) if s["key"] == key), -1)


def new_entry(key: str, row: dict | None, title: str = "", film: str = "", link: str = "") -> dict:
    if row:
        bits = ", ".join(x for x in (row["film"], row["year"], row["singers"].replace(";", " & ")) if x)
        return {"key": row["song_id"], "title": row["title"],
                "title_cell": f"{row['title']} ({bits})" if bits else row["title"],
                "key_change": row["my_key_change"], "link": row["karaoke_url"], "outcome": "",
                "skipped": False, "comment": "", "notes": ""}
    title = _clean(title, 150)
    return {"key": key, "title": title, "title_cell": f"{title} ({_clean(film, 80)})" if film else title,
            "key_change": "", "link": link, "outcome": "", "skipped": False, "comment": "",
            "notes": "outside catalog"}


def rekey_all(old_key: str, new_key: str) -> int:
    """An outside song was added to the catalog: point unfinished events at it."""
    n = 0
    row = catalog.catalog_get(new_key).get("row")
    with _lock:
        for eid in event_ids():
            ev = load(eid)
            i = find(ev, old_key)
            changed = False
            if i >= 0 and display_status(ev) != "performed" and find(ev, new_key) < 0:
                old = ev["songs"][i]
                ev["songs"][i] = new_entry(new_key, row) | {
                    k: old[k] for k in ("outcome", "skipped", "comment")} | {
                    "link": old["link"] or (row or {}).get("karaoke_url", "")}
                changed = True
            if old_key in ev["hidden"]:
                ev["hidden"] = [new_key if h == old_key else h for h in ev["hidden"]]
                changed = True
            if changed:
                save(ev, f"{old_key} -> {new_key}")
                n += 1
    return n


# --- suggestions --------------------------------------------------------------------------

EVENT_DEFAULTS = {"situation_match": 3, "theme_match": 2, "mood_match": 1, "era_match": 2,
                  "era_miss": -1, "voice_match": 2, "tempo_match": 1, "recent_similar_event": -3,
                  "recent_days": 365, "must_include": 20}


def weights() -> dict:
    W, _ = suggest.load_weights()
    return W | {"event": EVENT_DEFAULTS | (W.get("event") or {})}


def _set(row: dict, col: str) -> set:
    return set(filter(None, row[col].split(";")))


def wanted(ev: dict) -> dict:
    f = ev["filters"] or {}
    theme = set(filter(None, str(ev["meta"].get("theme", "")).split(";")))
    return {"situation": (theme & set(vocab.SITUATION)) | set(f.get("situations") or []),
            "theme": (theme & set(vocab.THEME)) | set(f.get("themes") or []),
            "mood": set(f.get("moods") or []), "genre": set(f.get("genres") or []),
            "era": set(f.get("eras") or []), "tempo": set(f.get("tempo") or []),
            "voice": set(f.get("voice_types") or []), "duets": f.get("duets") or 0,
            "avoid": [a.lower() for a in f.get("avoid") or []],
            "must": [m for m in f.get("must_include") or []]}


def _avoided(row: dict, avoid: list[str]) -> bool:
    if not avoid:
        return False
    words = {row["title"].lower(), *(_set(row, "genre")), *(_set(row, "mood")),
             *(s.lower() for s in _set(row, "singers"))}
    return any(a in words or (len(a) > 3 and a in row["title"].lower()) for a in avoid)


def score_for_event(row: dict, ev: dict, want: dict, W: dict, past: list[dict],
                    duets_in_list: int, must_ids: set) -> tuple[float, list[str]]:
    E = W["event"]
    pts, why = 0.0, []
    if row["song_id"] in must_ids:
        pts += E["must_include"]
        why.append("asked for")
    st = row["status"] or "wishlist"
    pts += W["status"].get(st, 0)
    why.append(st)
    if row["my_rating"]:
        pts += suggest.rating_points(row["my_rating"], W["rating"])
        why.append(f"⭐{row['my_rating']}")
    try:
        crowd = float(row["crowd_score"] or 0)
    except ValueError:
        crowd = 0.0
    if crowd >= 2.5:
        pts += W["crowd_score"]["gte_2_5"]
        why.append(f"crowd {crowd:g}")
    elif crowd >= 2.0:
        pts += W["crowd_score"]["gte_2_0"]
        why.append(f"crowd {crowd:g}")
    hit = _set(row, "situation") & want["situation"]
    if hit:
        pts += E["situation_match"]
        why.append(", ".join(sorted(hit)))
    hit = _set(row, "theme") & want["theme"]
    if hit:
        pts += E["theme_match"]
        why.append(", ".join(sorted(hit)))
    hit = (_set(row, "mood") & want["mood"]) | (_set(row, "genre") & want["genre"])
    if hit:
        pts += E["mood_match"]
        why.append(", ".join(sorted(hit)))
    if want["era"]:
        if row["era"] in want["era"]:
            pts += E["era_match"]
            why.append(row["era"])
        else:
            pts += E["era_miss"]
    if want["tempo"] and row["tempo_class"] in want["tempo"]:
        pts += E["tempo_match"]
    if (want["voice"] and row["voice_type"] in want["voice"]) or \
            (want["duets"] and duets_in_list < want["duets"] and row["voice_type"].startswith("duet")):
        pts += E["voice_match"]
        why.append("duet" if row["voice_type"].startswith("duet") else row["voice_type"])
    d = ev["meta"].get("date") or date.today().isoformat()
    for p in past:
        try:
            age = (date.fromisoformat(d) - date.fromisoformat(p["date"])).days
        except ValueError:
            continue
        if 0 <= age <= E["recent_days"] and row["song_id"] in p["keys"] and _similar_event(ev, p):
            pts += E["recent_similar_event"]
            why.append(f"sung at {p['name']}")
            break
    return round(pts, 2), why


def suggestions(ev: dict) -> list[dict]:
    """Catalog songs for this event, best first (playlist and hidden excluded)."""
    W = weights()
    want = wanted(ev)
    songs = catalog.load_songs()
    taken = {s["key"] for s in ev["songs"]} | set(ev["hidden"])
    past = past_performances(exclude_id=ev["id"])
    by_id = {r["song_id"]: r for r in songs}
    duets = sum(1 for s in ev["songs"] if by_id.get(s["key"], {}).get("voice_type", "").startswith("duet"))
    must_ids = set()
    for m in want["must"]:
        r = suggest._resolve(m)
        if r["row"]:
            must_ids.add(r["row"]["song_id"])
    floor = W["exclude_rating_at_or_below_in_plans"]
    out = []
    for row in songs:
        if row["song_id"] in taken or row["status"] == "retired":
            continue
        if row["my_rating"] and int(row["my_rating"]) <= floor:
            continue
        if _avoided(row, want["avoid"]) and row["song_id"] not in must_ids:
            continue
        score, why = score_for_event(row, ev, want, W, past, duets, must_ids)
        out.append((score, row["title"].lower(), row, why))
    out.sort(key=lambda x: (-x[0], x[1]))
    return [{"key": r["song_id"], "score": s, "reasons": " · ".join(w)} for s, _, r, w in out]


# --- auto-order -------------------------------------------------------------------------------

def auto_order(ev: dict, by_id: dict) -> list[str]:
    """Running order by the energy arc: a strong opener, harder songs early, no
    two slow or two very fast songs in a row, the closer the instructions ask
    for (singalong by default). Skipped songs go last."""
    f = ev["filters"] or {}
    songs = [s for s in ev["songs"] if not s["skipped"]]
    skipped = [s["key"] for s in ev["songs"] if s["skipped"]]
    if len(songs) < 3:
        return [s["key"] for s in songs] + skipped

    def info(s):
        r = by_id.get(s["key"]) or {}
        e = int(r["energy"]) if str(r.get("energy", "")).isdigit() else 3
        sing = int(r["singalong"]) if str(r.get("singalong", "")).isdigit() else 0
        hard = (r.get("status") in ("practicing", "wishlist")) + bool(s["key_change"].strip("0+ "))
        return {"key": s["key"], "energy": e, "tempo": r.get("tempo_class", ""), "sing": sing,
                "hard": hard, "rating": int(r["my_rating"]) if str(r.get("my_rating", "")).isdigit() else 0}

    pool = [info(s) for s in songs]
    end = f.get("arc_end") or "singalong"
    closer = max(pool, key=lambda x: (x["sing"], x["energy"], x["rating"])) if end == "singalong" else \
        min(pool, key=lambda x: (x["energy"], -x["rating"])) if end == "calm" else \
        max(pool, key=lambda x: (x["energy"], x["rating"]))
    pool.remove(closer)
    start = f.get("arc_start") or "high"
    opener = min(pool, key=lambda x: (x["energy"], -x["rating"])) if start == "calm" else \
        max(pool, key=lambda x: (x["energy"] if start == "high" else -abs(x["energy"] - 3), x["rating"]))
    pool.remove(opener)
    # Harder songs early, then alternate energy so the set breathes.
    pool.sort(key=lambda x: (-x["hard"], -x["energy"]))
    order = [opener]
    while pool:
        prev = order[-1]

        def clash(x):
            return (x["tempo"] == "slow" and prev["tempo"] == "slow") or \
                (x["energy"] >= 5 and prev["energy"] >= 5)
        pick = next((x for x in pool if not clash(x)), pool[0])
        pool.remove(pick)
        order.append(pick)
    order.append(closer)
    return [x["key"] for x in order] + skipped


# --- finish (feedback) --------------------------------------------------------------------------

def fits_after_pairs(ev: dict) -> list[tuple[dict, dict]]:
    """Consecutive sung songs that both came out standout or good."""
    sung = [s for s in ev["songs"] if not s["skipped"]]
    return [(a, b) for a, b in zip(sung, sung[1:])
            if a["outcome"] in ("standout", "good") and b["outcome"] in ("standout", "good")
            and not a["key"].startswith("t:") and not b["key"].startswith("t:")]


def sync_fits_after(ev: dict) -> dict:
    """Like rows (request_type after) for the event's good consecutive pairs;
    unlike rows for pairs this event linked earlier that no longer qualify.
    Safe to re-run."""
    tag = f"event:{ev['id']}"
    rows = feedback.load_relations()
    linked = {}
    for r in rows:
        if r["request_text"] == tag and r["verdict"] in ("like", "unlike"):
            linked[(r["reference_key"], r["song_key"])] = r["verdict"] == "like"
    want = {(a["key"], b["key"]): (a["title"], b["title"]) for a, b in fits_after_pairs(ev)}
    added = removed = 0
    for (ka, kb), (ta, tb) in want.items():
        if not linked.get((ka, kb)):
            feedback.set_like(ka, kb, True, ta, tb, request_type="after", request_text=tag)
            added += 1
    for (ka, kb), on in linked.items():
        if on and (ka, kb) not in want:
            feedback.set_like(ka, kb, False, request_type="after", request_text=tag)
            removed += 1
    return {"added": added, "removed": removed}


def add_requests_to_wishlist(ev: dict) -> list[str]:
    """Audience requests not in the catalog yet become wishlist songs (flagged
    for the agent to identify and tag). Requests already there are skipped."""
    added = []
    for text in ev["requests"]:
        title = _clean(re.sub(r"\s*\(.*?\)\s*$", "", text), 150)
        if not title or len(title) < 2:
            continue
        g = catalog.catalog_get(title)
        if g.get("found") or any(m["score"] >= 90 for m in g.get("matches") or []):
            continue
        r = catalog.catalog_upsert({"title": title, "status": "wishlist", "added_from": "song-name",
                                    "notes": f"Audience request at {ev['meta'].get('event', ev['id'])} "
                                             f"({ev['meta'].get('date', '')})"})
        if r["success"]:
            catalog.review_add(r["song_id"], f"events/{ev['id']}.md", "unsure-match",
                               "Audience request from the Mini App: identify film, year, singers; tag it.")
            added.append(r["song_id"])
    return added


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
