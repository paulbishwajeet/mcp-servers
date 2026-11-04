"""Feedback on suggestions: 👍/👎 relations, ⭐ ratings, reply-keyboard buttons.

- Relations (feedback/relations.csv in the wiki, append-only) say whether song
  Y fits after/with reference X. They are about the pair, never about Y alone.
- Ratings (my_rating 1-10 on songs.csv) are about the song.
- Session state (STATE_ROOT/last_suggestions.json, outside the wiki) maps the
  numbers in the last suggestion lists to songs, so a tap like "3 👍" can be
  resolved. The last 5 requests are kept; numbers refer to the most recent one.
"""

import csv
import json
import re
import threading
from datetime import date, datetime

import catalog
import tg
import vocab
import youtube
from common import fail, log, ok, state_root

RELATIONS_REL = "feedback/relations.csv"
STATE_FILE = "last_suggestions.json"
KEEP_REQUESTS = 5
MAIN_TEXT = "Rate these: 👍 fits · 👎 doesn't fit · ⭐ rate song"
_lock = threading.RLock()


# --- session state --------------------------------------------------------------------

def _state_path():
    return state_root() / STATE_FILE


def load_state() -> dict:
    p = _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError) as e:
        log(f"[feedback] state unreadable, starting fresh: {e}")
        data = {}
    data.setdefault("requests", [])
    data.setdefault("pending_ratings", {})
    return data


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    catalog.atomic_write(p, json.dumps(state, ensure_ascii=False, indent=1))


def push_request(req: dict) -> None:
    with _lock:
        st = load_state()
        st["requests"] = (st["requests"] + [req])[-KEEP_REQUESTS:]
        save_state(st)


def save_request(req: dict) -> None:
    with _lock:
        st = load_state()
        st["requests"] = [req if r["request_id"] == req["request_id"] else r for r in st["requests"]]
        save_state(st)


def latest_request() -> dict | None:
    reqs = load_state()["requests"]
    return reqs[-1] if reqs else None


def get_request(request_id: str = "") -> dict | None:
    reqs = load_state()["requests"]
    if not request_id:
        return reqs[-1] if reqs else None
    return next((r for r in reqs if r["request_id"] == request_id), None)


# --- relations.csv ---------------------------------------------------------------------

def load_relations() -> list[dict]:
    path = catalog.wiki_path(RELATIONS_REL)
    if not path.exists():
        return []
    return catalog.read_csv(path, vocab.RELATION_COLUMNS)


def _save_relations(rows: list[dict]) -> None:
    catalog.atomic_write(catalog.wiki_path(RELATIONS_REL),
                         catalog.render_csv(vocab.RELATION_COLUMNS, rows))


def _append_relation(row: dict) -> None:
    path = catalog.wiki_path(RELATIONS_REL)
    if not path.exists():
        _save_relations([row])
        return
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(catalog.render_csv(vocab.RELATION_COLUMNS, [row]).split("\n", 1)[1])


def relation_score(ref_keys: list[str] | str, song_key: str, rows: list[dict], cfg: dict,
                   today: date | None = None) -> tuple[float, int, int]:
    """Σ per_up/per_down × 0.5^(age_days/half_life_days) over the rows for
    (reference, song), capped at ±cap. Returns (score, ups, downs)."""
    keys = {ref_keys} if isinstance(ref_keys, str) else set(ref_keys)
    today = today or date.today()
    total, ups, downs = 0.0, 0, 0
    half = float(cfg.get("half_life_days") or 0)
    for r in rows:
        if r["reference_key"] not in keys or r["song_key"] != song_key:
            continue
        try:
            age = (today - datetime.fromisoformat(r["logged_at"].replace(" ", "T")).date()).days
        except ValueError:
            age = 0
        decay = 0.5 ** (max(0, age) / half) if half > 0 else 1.0
        if r["verdict"] == "up":
            total += cfg["per_up"] * decay
            ups += 1
        elif r["verdict"] == "down":
            total += cfg["per_down"] * decay
            downs += 1
    cap = float(cfg.get("cap") or 0)
    if cap:
        total = max(-cap, min(cap, total))
    return round(total, 2), ups, downs


def record_relation(req: dict, n: int, verdict: str) -> str:
    """👍/👎 for item n of req. A second tap on the same item in the same request
    replaces the first. Returns the reply line."""
    it = req["numbers"][str(n)]
    refs = [(k, t) for k, t in zip(req["reference_keys"], req["reference_title"].split("; "))] \
        if req["mode"] == "plan" else [(req["reference_key"], req["reference_title"])]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        prev = req["taps"].get(str(n))
        if prev:  # replace: drop the rows the earlier tap wrote
            old = {(p["logged_at"], p["reference_key"]) for p in prev}
            rows = [r for r in load_relations()
                    if not ((r["logged_at"], r["reference_key"]) in old and r["song_key"] == it["song_key"])]
            _save_relations(rows)
        written = []
        for key, title in refs:
            row = {"logged_at": now, "reference_key": key, "reference_title": title,
                   "song_key": it["song_key"], "song_title": it["title"], "verdict": verdict,
                   "request_type": req["mode"], "request_text": req["request_text"]}
            _append_relation(row)
            written.append({"logged_at": now, "reference_key": key})
        req["taps"][str(n)] = written
        save_request(req)
    word = {"like": "with", "after": "after", "next": "for", "plan": "in"}[req["mode"]]
    ref = req["reference_title"] if req["mode"] != "next" else f"'{req['reference_title']}'"
    return f"{'👍' if verdict == 'up' else '👎'} noted: {it['title']} {word} {ref}"


# --- ratings ----------------------------------------------------------------------------

def _rate_row(song_id: str, rating: int) -> dict:
    return catalog.catalog_upsert({"song_id": song_id, "my_rating": rating,
                                   "rated_on": date.today().isoformat()})


def set_rating(ref: str, rating) -> dict:
    """Typed "rate <song> 9": fuzzy title or song_id."""
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return fail("invalid_input", "rating must be a whole number 1-10.")
    if not 1 <= rating <= 10:
        return fail("invalid_input", "rating must be 1-10.")
    g = catalog.catalog_get(ref)
    if g.get("found"):
        row = g["row"]
    else:
        best = (g.get("matches") or [None])[0]
        if not best or best["score"] < 90:
            return fail("not_found", f"No catalog song matches '{ref}'.", matches=g.get("matches", []))
        row = catalog.catalog_get(best["song_id"])["row"]
    r = _rate_row(row["song_id"], rating)
    if not r["success"]:
        return r
    return ok(song_id=row["song_id"], title=row["title"], my_rating=rating,
              reply=f"⭐{rating} saved for {row['title']}")


# --- keyboards ---------------------------------------------------------------------------

def main_rows(numbers: list[int]) -> list[list[str]]:
    return [[f"{n} 👍", f"{n} 👎", f"{n} ⭐"] for n in numbers]


FOOTER = ["more", "done"]


def rating_pad(n: int) -> list[list[str]]:
    return [[f"{n} ⭐{i}" for i in range(1, 6)], [f"{n} ⭐{i}" for i in range(6, 11)], ["back"]]


def send_main_keyboard(req: dict) -> dict:
    """The 👍/👎/⭐ keyboard for the request's current page."""
    numbers = req.get("page_numbers") or []
    if not numbers:
        return fail("nothing_to_rate", "The last list has no items.")
    keyboards = tg.split_keyboard(main_rows(numbers), FOOTER)
    result = None
    for kb in keyboards:
        nums = [int(row[0].split()[0]) for row in kb[:-1]]
        text = MAIN_TEXT if len(keyboards) == 1 else f"{MAIN_TEXT} (#{nums[0]}–#{nums[-1]})"
        result = tg.send(text, keyboard=kb)
        if not result["success"]:
            return result
    return result


def send_buttons(request_id: str = "") -> dict:
    req = get_request(request_id)
    if not req:
        return fail("no_request", "No suggestion list to add buttons to.")
    return send_main_keyboard(req)


# --- taps --------------------------------------------------------------------------------

_EMOJI_NOISE = re.compile("[︎️‍\U0001f3fb-\U0001f3ff]")
_TAP = re.compile(r"^#?(\d{1,3})\s*(👍|👎|⭐)\s*(\d{1,2})?$")
_RATE_NUM = re.compile(r"^rate\s+#?(\d{1,3})\s+(\d{1,2})$", re.I)
_RATE_TITLE = re.compile(r"^rate\s+(.+?)\s+(\d{1,2})$", re.I)
_BATCH_ITEM = re.compile(r"#?(\d{1,3})\s*(👍|👎|⭐\s*\d{1,2})")


def _clean(text: str) -> str:
    t = _EMOJI_NOISE.sub("", text or "")
    t = t.replace("★", "⭐").replace("🌟", "⭐")
    return re.sub(r"\s+", " ", t).strip()


def _apply(req: dict, n: int, op: str, rating: int | None, state: dict) -> dict:
    """One tap on item n. Returns {reply, needs_add?, keyboard: main|pad|none}."""
    it = req["numbers"].get(str(n))
    if not it:
        return {"reply": f"#{n} isn't in the last list", "keyboard": "none"}
    if op in ("👍", "👎"):
        return {"reply": record_relation(req, n, "up" if op == "👍" else "down"), "keyboard": "none"}
    if rating is None:  # "3 ⭐" -> rating pad
        return {"reply": "", "keyboard": "pad", "pad": n, "title": it["title"]}
    if not 1 <= rating <= 10:
        return {"reply": "Ratings are 1-10", "keyboard": "none"}
    if it["group"] == "catalog":
        r = _rate_row(it["song_key"], rating)
        if not r["success"]:
            return {"reply": f"Couldn't save the rating: {r['error']}", "keyboard": "none"}
        it["my_rating"] = str(rating)
        return {"reply": f"⭐{rating} saved for {it['title']}", "keyboard": "main"}
    state["pending_ratings"][it["song_key"]] = {"rating": rating, "title": it["title"],
                                               "url": it.get("karaoke_url") or it.get("video_url", ""),
                                               "logged_at": datetime.now().isoformat(timespec="seconds")}
    return {"reply": f"⭐{rating} for {it['title']} kept: it isn't in the catalog yet, add it to save "
                     f"the rating", "keyboard": "main", "needs_add": True,
            "title": it["title"], "url": it.get("karaoke_url") or it.get("video_url", "")}


def feedback_tap(text: str) -> dict:
    t = _clean(text)
    low = t.lower()
    req = latest_request()
    if low in ("done", "more", "back"):
        if not req:
            return ok(handled=True, reply="There's no suggestion list right now.")
        if low == "done":
            r = tg.send("👍 thanks", remove_keyboard=True)
            return ok(handled=True, reply="", sent=r["success"],
                      warnings=[] if r["success"] else [r["error"]])
        if low == "back":
            r = send_main_keyboard(req)
            return ok(handled=True, reply="", sent=r["success"],
                      warnings=[] if r["success"] else [r["error"]])
        import suggest
        r = suggest.suggest_more()
        return r | {"handled": True, "reply": r.get("note", "")}

    ops = []
    m = _TAP.match(t)
    if m:
        ops.append((int(m.group(1)), m.group(2), int(m.group(3)) if m.group(3) else None))
    elif _RATE_NUM.match(t):
        m = _RATE_NUM.match(t)
        ops.append((int(m.group(1)), "⭐", int(m.group(2))))
    elif _RATE_TITLE.match(t):  # "rate Pal Pal Dil Ke Paas 9"
        m = _RATE_TITLE.match(t)
        r = set_rating(m.group(1), m.group(2))
        return ok(handled=True, reply=r.get("reply") or r.get("error", ""), result=r)
    elif low.startswith("fb "):
        for n, op in _BATCH_ITEM.findall(t[3:]):
            if op.startswith("⭐"):
                ops.append((int(n), "⭐", int(op[1:].strip())))
            else:
                ops.append((int(n), op, None))
        if not ops:
            return ok(handled=True, reply="Couldn't read that: try fb 1👍 3👎 5⭐8")
    else:
        return ok(handled=False, reply="")
    if not req:
        return ok(handled=True, reply="There's no suggestion list right now.")

    with _lock:
        state = load_state()
        replies, needs_add, kb, pad = [], [], "none", None
        for n, op, rating in ops:
            res = _apply(req, n, op, rating, state)
            if res["reply"]:
                replies.append(res["reply"])
            if res.get("needs_add"):
                needs_add.append({"n": n, "title": res["title"], "url": res["url"]})
            if res["keyboard"] == "pad" and len(ops) == 1:
                kb, pad = "pad", res
            elif res["keyboard"] == "main" and kb == "none":
                kb = "main"
        # record_relation already saved its taps into req; state holds the
        # pending ratings. Write both back together.
        state["requests"] = [req if r["request_id"] == req["request_id"] else r for r in state["requests"]]
        for n, op, rating in ops:  # keep ratings shown in the cached list current
            if op == "⭐" and rating and str(n) in req["numbers"] and \
                    req["numbers"][str(n)]["group"] == "catalog":
                req["numbers"][str(n)]["my_rating"] = str(rating)
        save_state(state)
    warnings = []
    if kb == "pad":
        r = tg.send(f"Rate {pad['title']}:", keyboard=rating_pad(pad["pad"]))
        if not r["success"]:
            warnings.append(r["error"])
    elif kb == "main":
        r = send_main_keyboard(req)
        if not r["success"]:
            warnings.append(r["error"])
    out = ok(handled=True, reply=" · ".join(replies), warnings=warnings)
    if needs_add:
        out |= {"needs_add": True, "add": needs_add}
    return out


# --- rekey + lookups -----------------------------------------------------------------------

def relations_rekey(old_key: str, new_key: str) -> dict:
    old_key, new_key = (old_key or "").strip(), (new_key or "").strip()
    if not old_key or not new_key:
        return fail("invalid_input", "old_key and new_key are required.")
    with _lock:
        rows = load_relations()
        n = 0
        for r in rows:
            for col in ("reference_key", "song_key"):
                if r[col] == old_key:
                    r[col] = new_key
                    n += 1
        if n:
            _save_relations(rows)
            catalog.append_log("relations-rekey", new_key, f"{old_key} -> {new_key} ({n} keys)")
        return ok(old_key=old_key, new_key=new_key, rewritten=n)


def on_song_added(row: dict) -> dict:
    """Called by catalog_upsert for a newly added song: if one of its YouTube
    links was suggested as an outside song, move its relations (yt:<id>) and any
    pending ⭐ rating over to the new song_id."""
    urls = [row.get("karaoke_url") or ""] + [u for u in (row.get("karaoke_alt_urls") or "").split(";")]
    keys = {"yt:" + youtube.video_id_from_url(u) for u in urls if u and youtube.video_id_from_url(u)}
    if not keys:
        return {"rekeyed": 0, "rating": None}
    rekeyed, rating = 0, None
    present = {r[c] for r in load_relations() for c in ("reference_key", "song_key")}
    for k in keys & present:
        rekeyed += relations_rekey(k, row["song_id"])["rewritten"]
    with _lock:
        state = load_state()
        for k in keys:
            p = state["pending_ratings"].pop(k, None)
            if p:
                rating = p["rating"]
        if rating is not None:
            save_state(state)
    if rating is not None:
        _rate_row(row["song_id"], rating)
    return {"rekeyed": rekeyed, "rating": rating}


def relations_for(ref: str, limit: int = 10) -> dict:
    """A song's strongest 👍/👎 relations both ways."""
    g = catalog.catalog_get(ref)
    if g.get("found"):
        key, title = g["row"]["song_id"], g["row"]["title"]
    elif ref.startswith(("yt:", "query:")):
        key, title = ref, ref
    else:
        best = (g.get("matches") or [None])[0]
        if not best or best["score"] < 90:
            return fail("not_found", f"No catalog song matches '{ref}'.", matches=g.get("matches", []))
        key, title = best["song_id"], best["title"]
    import suggest
    cfg = suggest.load_weights()[0]["relation"]
    rows = load_relations()
    pairs = {}
    for r in rows:
        if r["reference_key"] == key:
            pairs.setdefault(("after", r["song_key"]), r["song_title"])
        if r["song_key"] == key:
            pairs.setdefault(("before", r["reference_key"]), r["reference_title"])
    out = {"after": [], "before": []}
    for (direction, other), other_title in pairs.items():
        ref_k, song_k = (key, other) if direction == "after" else (other, key)
        score, ups, downs = relation_score(ref_k, song_k, rows, cfg)
        out[direction].append({"song_key": other, "title": other_title, "score": score,
                               "ups": ups, "downs": downs})
    for d in out:
        out[d].sort(key=lambda x: -abs(x["score"]))
        out[d] = out[d][:limit]
    return ok(song_key=key, title=title, goes_after_it=out["after"], it_goes_after=out["before"])
