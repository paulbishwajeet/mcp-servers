"""Event Planner API (spec §5): events list, new/edit, playlist building,
suggestions, YouTube ideas, auto-order, Create, and feedback after the event.

Everything is read from and written to the event's markdown file (events.py)
on every call, so drafts are always saved and the chat agent sees the same
file. Performed events only accept feedback changes.
"""

import copy
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

import catalog
import events
import feedback
import locks
import suggest
import tg
from common import is_youtube_url

from . import instructions, similar

router = APIRouter(prefix="/events")
PAGE = 10
IDEAS_WAIT_SEC = 20
MAX_IDEA_SEEDS = 8
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ideas")
_pool_mix = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ideas-mix")
_ideas: dict[str, dict] = {}
_ideas_lock = threading.Lock()


def _fail(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": message} | extra)


def _load(event_id: str) -> dict:
    try:
        return events.load(event_id)
    except (ValueError, FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="No such event.") from None


def _require_editable(ev: dict):
    if not events.editable(ev):
        raise HTTPException(status_code=409, detail="This event is performed: only feedback can change.")


def _item(s: dict, row: dict | None, warnings: list[str]) -> dict:
    base = {"key": s["key"], "catalog_id": row["song_id"] if row else "", "title": row["title"] if row else s["title"],
            "outcome": s["outcome"], "comment": s["comment"], "skipped": s["skipped"],
            "key_change": s["key_change"], "notes": s["notes"], "warnings": warnings,
            "karaoke_url": (row["karaoke_url"] if row else "") or (s["link"] if is_youtube_url(s["link"]) else ""),
            "video_url": "", "group": "catalog" if row else "outside"}
    if row:
        base |= {k: row[k] for k in ("film", "year", "singers", "status", "energy", "tempo_class",
                                     "my_rating", "voice_type", "duration_sec")}
    else:
        base |= {"film": "", "year": "", "singers": "", "status": "", "energy": "", "tempo_class": "",
                 "my_rating": "", "voice_type": "", "duration_sec": ""}
    return base


def view(ev: dict) -> dict:
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    past = events.past_performances(exclude_id=ev["id"])
    recent = events.weights()["event"]["recent_days"]
    items = []
    for s in ev["songs"]:
        row = by_id.get(s["key"]) or by_id.get(similar.added_keys.get(s["key"], ""))
        items.append(_item(s, row, events.song_warnings(ev, s, row, past, recent)))
    m = ev["meta"]
    return {"success": True, "id": ev["id"],
            "meta": {"name": m.get("event") or ev["id"], "date": m.get("date", ""), "time": m.get("time", ""),
                     "theme": m.get("theme", ""), "instructions": m.get("instructions", ""),
                     "duration_min": m.get("duration_min", ""), "song_count": m.get("song_count", ""),
                     "event_type": m.get("event_type", ""), "venue": m.get("venue", ""),
                     "audience": m.get("audience", "")},
            "status": events.display_status(ev), "editable": events.editable(ev), "past": events.is_past(ev),
            "filters": ev["filters"], "chips": instructions.chips(ev["filters"]),
            "playlist": items, "hidden": ev["hidden"], "summary": events.summary(ev, by_id),
            "requests": ev["requests"], "notes": ev["notes"]}


def _parse_into(ev: dict) -> str:
    filters, source = instructions.parse(ev["meta"].get("instructions", ""))
    ev["filters"] = filters
    return source


# --- list / create / edit -----------------------------------------------------------------------

@router.get("")
def list_all():
    return events.list_events()


@router.get("/latest-past")
def latest_past():
    return {"success": True, "id": events.latest_past()}


@router.get("/vocab")
def event_vocab():
    import vocab
    return {"success": True, "event_types": events.EVENT_TYPES, "situations": vocab.SITUATION,
            "themes": vocab.THEME}


@router.post("")
def create(body: dict = Body(...)):
    r = events.create(body)
    if not r["success"]:
        return _fail(422, r["error"], field_errors=r.get("field_errors", {}))
    with locks.LOCK:
        ev = events.load(r["event_id"])
        source = _parse_into(ev)
        if ev["filters"]:
            events.save(ev, "instructions read")
    return view(ev) | {"filters_source": source}


@router.get("/{event_id}")
def get_event(event_id: str):
    return view(_load(event_id))


@router.put("/{event_id}")
def put_event(event_id: str, body: dict = Body(...)):
    fields = body.get("fields") or {}
    values, errs = events.validate_meta(fields)
    if errs:
        return _fail(422, "Some fields need fixing.", field_errors=errs)
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        old_instr = ev["meta"].get("instructions", "")
        if values.get("event") and values["event"] != ev["meta"].get("event"):
            ev["pre"] = re.sub(r"(?m)^# .*$", f"# {values['event']}", ev["pre"], count=1) \
                if re.search(r"(?m)^# ", ev["pre"]) else f"\n# {values['event']}\n\n"
        ev["meta"].update(values)
        source = ""
        if "instructions" in values and values["instructions"] != old_instr:
            source = _parse_into(ev)
        events.save(ev, "details: " + ", ".join(sorted(values)))
    return view(ev) | {"filters_source": source}


@router.post("/{event_id}/parse")
def reparse(event_id: str):
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        source = _parse_into(ev)
        events.save(ev, "instructions re-read")
    return view(ev) | {"filters_source": source}


@router.post("/{event_id}/chips/remove")
def remove_chip(event_id: str, body: dict = Body(...)):
    chip = str(body.get("chip", ""))[:120]
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        ev["filters"] = instructions.remove_chip(ev["filters"], chip)
        events.save(ev, f"filter removed: {chip}")
    return view(ev)


# --- playlist -------------------------------------------------------------------------------

@router.post("/{event_id}/songs")
def add_song(event_id: str, body: dict = Body(...)):
    key = str(body.get("song_key", "")).strip()
    catalog_key = similar.added_keys.get(key, key)
    row = catalog.catalog_get(catalog_key).get("row") if catalog.SONG_ID_RE.match(catalog_key) else None
    if not row and not events.YT_KEY_RE.match(key):
        return _fail(400, "song_key must be a catalog song_id or yt:<video id>.")
    link = str(body.get("karaoke_url") or body.get("video_url") or "")
    if link and not is_youtube_url(link):
        return _fail(400, "Links must be YouTube links.")
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        k = row["song_id"] if row else key
        if events.find(ev, k) >= 0:
            return _fail(409, "Already in the playlist.")
        entry = events.new_entry(k, row, str(body.get("title", "")), str(body.get("film", "")), link)
        pos = body.get("position")
        ev["songs"].insert(pos if isinstance(pos, int) and 0 <= pos <= len(ev["songs"]) else len(ev["songs"]), entry)
        ev["hidden"] = [h for h in ev["hidden"] if h != k]
        events.save(ev, f"added {k}")
    return view(ev)


@router.delete("/{event_id}/songs/{key}")
def remove_song(event_id: str, key: str):
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        i = events.find(ev, key)
        if i < 0:
            return _fail(404, "Not in the playlist.")
        ev["songs"].pop(i)
        events.save(ev, f"removed {key}")
    return view(ev)


@router.put("/{event_id}/order")
def set_order(event_id: str, body: dict = Body(...)):
    keys = body.get("keys")
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        current = [s["key"] for s in ev["songs"]]
        if not isinstance(keys, list) or sorted(map(str, keys)) != sorted(current):
            return _fail(409, "The playlist changed; reload it.")
        by_key = {s["key"]: s for s in ev["songs"]}
        ev["songs"] = [by_key[k] for k in keys]
        events.save(ev, "reordered")
    return view(ev)


@router.post("/{event_id}/autoorder")
def autoorder(event_id: str):
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        by_id = {r["song_id"]: r for r in catalog.load_songs()}
        order = events.auto_order(ev, by_id)
        by_key = {s["key"]: s for s in ev["songs"]}
        ev["songs"] = [by_key[k] for k in order]
        events.save(ev, "auto-ordered")
    return view(ev)


@router.post("/{event_id}/hide")
def hide(event_id: str, body: dict = Body(...)):
    """✕ on a suggestion: not for this event (only this event; no global dislike)."""
    key = str(body.get("song_key", "")).strip()
    if not (catalog.SONG_ID_RE.match(key) or events.YT_KEY_RE.match(key)):
        return _fail(400, "Bad song key.")
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        if key not in ev["hidden"]:
            ev["hidden"].append(key)
            events.save(ev, f"hid {key}")
    return {"success": True, "hidden": ev["hidden"]}


# --- suggestions ----------------------------------------------------------------------------------

@router.get("/{event_id}/suggestions")
def get_suggestions(event_id: str, page: int = Query(0, ge=0, le=50)):
    ev = _load(event_id)
    ranked = events.suggestions(ev)
    chunk = ranked[page * PAGE:(page + 1) * PAGE]
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    past = events.past_performances(exclude_id=ev["id"])
    recent = events.weights()["event"]["recent_days"]
    items = []
    for c in chunk:
        row = by_id[c["key"]]
        s = events.new_entry(c["key"], row)
        items.append(_item(s, row, events.song_warnings(ev, s, row, past, recent)) |
                     {"reasons": c["reasons"], "song_key": c["key"], "score": c["score"]})
    return {"success": True, "page": page, "items": items, "end": (page + 1) * PAGE >= len(ranked)}


def _ideas_key(ev: dict) -> tuple[str, list[dict]]:
    by_id = {r["song_id"]: r for r in catalog.load_songs()}
    refs = [{"key": s["key"], "title": by_id[s["key"]]["title"], "row": by_id[s["key"]]}
            for s in ev["songs"] if s["key"] in by_id and not s["skipped"]][:MAX_IDEA_SEEDS]
    sig = hashlib.sha1(("|".join(r["key"] for r in refs) + "#" + ev["id"]).encode()).hexdigest()[:12]
    return sig, refs


def _run_ideas(state: dict, ev: dict, refs: list[dict]) -> None:
    try:
        # One mix per playlist song, 4 at a time (cached 24 h per seed).
        mixes = [f.result() for f in [_pool_mix.submit(suggest.fetch_mix, r) for r in refs]]
        W, _ = suggest.load_weights()
        ranked = suggest.rank("plan", copy.deepcopy(refs), [], "", [s["key"] for s in ev["songs"]],
                              feedback.load_relations(), W, mixes=mixes)
        hidden = set(ev["hidden"]) | {s["key"] for s in ev["songs"]}
        state["items"] = [it for it in ranked["outside"] if it["song_key"] not in hidden]
        state["warnings"] = ranked["warnings"]
        state["status"] = "done"
    except Exception as e:  # never break the event page
        state["items"], state["status"], state["warnings"] = [], "error", [str(e)[:200]]
    finally:
        state["done"].set()


@router.get("/{event_id}/ideas")
def get_ideas(event_id: str, page: int = Query(0, ge=0, le=20)):
    """YouTube ideas: songs outside the catalog from the playlist songs' mixes;
    songs several playlist songs point to rank higher. Waits up to 20 s."""
    ev = _load(event_id)
    sig, refs = _ideas_key(ev)
    if not refs:
        return {"success": True, "status": "done", "items": [], "end": True,
                "note": "Add catalog songs to the playlist first."}
    with _ideas_lock:
        state = _ideas.get(sig)
        if not state or time.time() - state["at"] > 24 * 3600:
            state = {"at": time.time(), "status": "loading", "items": [], "warnings": [],
                     "done": threading.Event(), "lock": threading.Lock()}
            _ideas[sig] = state
            _pool.submit(_run_ideas, state, ev, refs)
    if not state["done"].wait(IDEAS_WAIT_SEC):
        return {"success": True, "status": "loading"}
    hidden = set(ev["hidden"]) | {s["key"] for s in ev["songs"]}
    visible = [it for it in state["items"] if it["song_key"] not in hidden]
    chunk = visible[page * PAGE:(page + 1) * PAGE]
    with state["lock"]:
        todo = [it for it in chunk if "karaoke" not in it]
        if todo:
            suggest._fill_karaoke(todo)
    return {"success": True, "status": state["status"], "page": page, "warnings": state["warnings"],
            "items": [{k: it.get(k, "") for k in ("song_key", "title", "film", "karaoke_url", "video_url",
                                                  "karaoke", "reasons", "group")} | {"catalog_id": ""}
                      for it in chunk],
            "end": (page + 1) * PAGE >= len(visible)}


# --- create (lock in) ---------------------------------------------------------------------------

def _fmt_min(sec: int) -> str:
    return f"{round(sec / 60)} min"


@router.post("/{event_id}/create")
def lock_in(event_id: str):
    with locks.LOCK:
        ev = _load(event_id)
        _require_editable(ev)
        if not ev["songs"]:
            return _fail(400, "Add songs to the playlist first.")
        by_id = {r["song_id"]: r for r in catalog.load_songs()}
        ev["status"] = "planned"
        events.save(ev, "planned")
    sm = events.summary(ev, by_id)
    m = ev["meta"]
    outside = sum(1 for s in ev["songs"] if s["key"] not in by_id)
    line = f"Planned: {m.get('event') or ev['id']} · {m.get('date', '')} {m.get('time', '')}".rstrip() + \
        f" · {sm['songs']} songs · ≈ {_fmt_min(sm['total_sec'])}" + \
        (f" · {outside} outside the catalog" if outside else "")
    r = tg.send(line)
    return view(ev) | {"sent": r["success"], "text": line}


# --- feedback --------------------------------------------------------------------------------------

@router.put("/{event_id}/feedback")
def put_feedback(event_id: str, body: dict = Body(...)):
    """Autosave: {song_key, outcome?, comment?, skipped?} or {requests: [...]} or {notes: [...]}."""
    with locks.LOCK:
        ev = _load(event_id)
        what = []
        if "song_key" in body:
            i = events.find(ev, str(body["song_key"]))
            if i < 0:
                return _fail(404, "Not in the playlist.")
            s = ev["songs"][i]
            if "outcome" in body:
                o = str(body["outcome"] or "").lower()
                if o and o not in events.OUTCOMES:
                    return _fail(422, "outcome must be standout, good or weak.",
                                 field_errors={"outcome": "standout, good or weak."})
                s["outcome"] = o
            if "comment" in body:
                s["comment"] = events._clean(body["comment"], 300)
            if "skipped" in body:
                s["skipped"] = bool(body["skipped"])
            what.append(f"feedback {s['key']}")
        for key in ("requests", "notes"):
            if key in body:
                vals = body[key] if isinstance(body[key], list) else str(body[key]).split("\n")
                ev[key] = [events._clean(v, 300) for v in vals if events._clean(v)][:50]
                what.append(key)
        if not what:
            return _fail(400, "Nothing to save.")
        events.save(ev, ", ".join(what))
    return view(ev)


@router.post("/{event_id}/finish")
def finish(event_id: str):
    """Status performed, event_apply (times_performed, crowd_score,
    last_performed), audience requests to the wishlist, fits-after links, and
    one summary line in the chat. Safe to run again after editing feedback."""
    with locks.LOCK:
        ev = _load(event_id)
        ev["status"] = "performed"
        events.save(ev, "performed")
        applied = catalog.event_apply(f"{event_id}.md")
        links = events.sync_fits_after(ev)
        wish = events.add_requests_to_wishlist(ev)
    counts = {o: sum(1 for s in ev["songs"] if s["outcome"] == o and not s["skipped"]) for o in events.OUTCOMES}
    skipped = sum(1 for s in ev["songs"] if s["skipped"])
    bits = [f"{n} {o}" for o, n in counts.items() if n] + ([f"{skipped} skipped"] if skipped else [])
    line = f"Feedback saved: {ev['meta'].get('event') or event_id} · " + (", ".join(bits) or "no outcomes") + \
        (f" · {len(applied.get('updated', []))} songs updated" if applied.get("success") else "") + \
        (f" · {len(wish)} requests added to wishlist" if wish else "")
    r = tg.send(line)
    return view(ev) | {"sent": r["success"], "text": line, "applied": applied.get("success", False),
                       "links": links, "wishlist_added": wish, "warnings": applied.get("warnings", [])}
