"""Deterministic song suggestions: "like X", "after X", "next <words>", plans.

Candidates come from YouTube Mixes (youtube_mix) and the catalog, and are
scored with the weights in the wiki's ranking.yaml, so the agent never does
arithmetic over 40 candidates. Results are numbered continuously across groups
and pages (catalog first, then outside), and the numbering is saved in the
session state so feedback taps ("3 👍") can be resolved later.
"""

import copy
import re
import uuid
from datetime import date, datetime
from pathlib import Path

import catalog
import feedback
import vocab
import youtube
from common import fail, log, ok

RANKING_REL = "ranking.yaml"

DEFAULT_WEIGHTS = {
    "mix_position": {"top5": 3, "top15": 2, "top40": 1},
    "both_sources": 2,
    "multiple_mixes_per_extra": 2,
    "status": {"ready": 3, "practicing": 2, "wishlist": 1, "outside": 0},
    "crowd_score": {"gte_2_5": 2, "gte_2_0": 1},
    "same_era": 1,
    "tempo_or_energy_within_1": 1,
    "same_mood": 1,
    "relation": {"per_up": 2, "per_down": -2, "cap": 6, "half_life_days": 180},
    "rating": {"9-10": 3, "7-8": 2, "5-6": 0, "3-4": -1, "1-2": -3},
    "max_per_singer": 3,
    "exclude_rating_at_or_below_in_plans": 2,
}
MODES = ("like", "after", "next", "plan")
# Per-mode page sizes (catalog, outside). like uses the caller's limits.
MODE_LIMITS = {"after": (5, 3), "next": (3, 0)}
MIX_LIMIT = 40
STOPWORDS = {"a", "an", "the", "and", "or", "of", "for", "to", "me", "my", "some", "song",
             "songs", "next", "give", "please", "pls", "any", "something", "like", "after",
             "with", "in", "on", "one", "few", "suggest", "play", "sing"}

# "next <words>": words that map to catalog filters.
ERA_WORDS = {"old": {"pre-1960", "1960s", "1970s", "1980s"}, "classic": {"pre-1960", "1960s", "1970s"},
             "retro": {"1960s", "1970s", "1980s"}, "new": {"2010s", "2020s"},
             "latest": {"2020s"}, "recent": {"2010s", "2020s"},
             "50s": {"pre-1960"}, "60s": {"1960s"}, "70s": {"1970s"}, "80s": {"1980s"},
             "90s": {"1990s"}, "2000s": {"2000s"}}
TEMPO_WORDS = {"slow": "slow", "medium": "medium", "fast": "fast", "upbeat": "fast",
               "dance": "fast"}
VOICE_WORDS = {"duet": {"duet-mf", "duet-mm", "duet-ff"}, "female": {"solo-female", "duet-ff"},
               "male": {"solo-male", "duet-mm"}, "solo": {"solo-male", "solo-female"},
               "group": {"group"}}


# --- weights ---------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_weights() -> tuple[dict, list[str]]:
    """ranking.yaml merged over the defaults. Returns (weights, warnings)."""
    import yaml
    warnings = []
    try:
        path = catalog.wiki_path(RANKING_REL)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not path.exists():
            warnings.append("ranking.yaml not found; using default weights.")
    except Exception as e:  # bad YAML must not break suggestions
        warnings.append(f"ranking.yaml unreadable ({e}); using default weights.")
        data = {}
    return _merge(DEFAULT_WEIGHTS, data if isinstance(data, dict) else {}), warnings


def rating_points(rating: str | int | None, bands: dict) -> int:
    try:
        r = int(rating)
    except (TypeError, ValueError):
        return 0
    for band, pts in bands.items():
        lo, _, hi = str(band).partition("-")
        try:
            if int(lo) <= r <= int(hi or lo):
                return pts
        except ValueError:
            continue
    return 0


# --- references and queries ---------------------------------------------------------------

def query_key(text: str) -> str:
    words = sorted(set(re.findall(r"[a-z0-9]+", (text or "").lower())) - STOPWORDS)
    return "query:" + " ".join(words)


def query_filters(text: str) -> dict:
    words = set(re.findall(r"[a-z0-9-]+", (text or "").lower()))
    f = {"era": set(), "tempo": set(), "voice": set(), "mood": set(), "genre": set(),
         "theme": set(), "situation": set()}
    for w in words:
        f["era"] |= ERA_WORDS.get(w, set())
        if w in TEMPO_WORDS:
            f["tempo"].add(TEMPO_WORDS[w])
        f["voice"] |= VOICE_WORDS.get(w, set())
        for col, allowed in (("mood", vocab.MOOD), ("genre", vocab.GENRE), ("theme", vocab.THEME),
                             ("situation", vocab.SITUATION)):
            f[col] |= {v for v in allowed if v == w or v.split("-")[0] == w}
    return f


def _resolve(ref: str) -> dict:
    """A reference by song_id or title -> {key, title, row}. row is None when the
    song isn't in the catalog (the key is filled in from the mix seed later)."""
    ref = (ref or "").strip()
    g = catalog.catalog_get(ref)
    if g.get("found"):
        return {"key": g["row"]["song_id"], "title": g["row"]["title"], "row": g["row"]}
    matches = [m for m in g.get("matches") or [] if m["score"] >= 90]
    if matches:
        # Several rows can match equally (a duplicate, an unplugged version):
        # prefer the plain version, better status, then a row with film + link.
        # catalog_get scores title + film, so a duplicate without a film can
        # outscore the real row: group rows whose title equals the reference.
        same = [m for m in matches if _norm(m["title"]) == _norm(ref)]
        top = max(m["score"] for m in matches)
        rows = [catalog.catalog_get(m["song_id"])["row"]
                for m in (same or [m for m in matches if m["score"] == top])]
        status_rank = {"ready": 0, "practicing": 1, "wishlist": 2}
        row = min(rows, key=lambda r: (bool(youtube._row_version(r["title"])),
                                       status_rank.get(r["status"], 3), not r["film"],
                                       not r["karaoke_url"], r["song_id"]))
        return {"key": row["song_id"], "title": row["title"], "row": row}
    return {"key": "", "title": ref, "row": None}


# --- candidates -----------------------------------------------------------------------------

def _karaoke_video(entry: dict) -> str:
    return next((v["url"] for v in entry.get("videos", []) if v["kind"] == "karaoke"), "")


def _collect_mix(ref: dict, url: str, cands: dict, mix_index: int) -> tuple[str, str]:
    """Run youtube_mix for one reference and fold its entries into cands.
    Returns (seed key, warning)."""
    row = ref["row"]
    seed_url = url or (row["karaoke_url"] if row and row["karaoke_url"] else "")
    mix = youtube.youtube_mix(song=ref["title"] if not seed_url else "", url=seed_url,
                              source="both", limit=MIX_LIMIT)
    if not mix.get("success"):
        return "", "YouTube Mix unavailable"
    seed_key = "yt:" + mix["seed"]["video_id"]
    if not ref["title"] or ref["title"] == url:
        ref["title"] = mix["seed"].get("song_title") or mix["seed"].get("title") or ref["title"]
    for e in mix["entries"]:
        if e["kind"] in ("jukebox", "other"):
            continue
        if e["in_catalog"]:
            key, group = e["in_catalog"], "catalog"
        else:
            key, group = "yt:" + youtube.video_id_from_url(e["videos"][0]["url"]), "outside"
        c = cands.setdefault(key, {"key": key, "group": group, "title": e["song_title"],
                                   "film": e["film"], "mix_rank": None, "sources": set(),
                                   "mixes": set(), "kind": e["kind"],
                                   "video_url": e["videos"][0]["url"],
                                   "karaoke_url": _karaoke_video(e)})
        c["mix_rank"] = e["rank"] if c["mix_rank"] is None else min(c["mix_rank"], e["rank"])
        c["sources"] |= set(e["sources"])
        c["mixes"].add(mix_index)
        if not c["karaoke_url"]:
            c["karaoke_url"] = _karaoke_video(e)
    return seed_key, ""


def _similar(row: dict, ref_rows: list[dict], qf: dict | None) -> dict:
    """Which similarity weights apply: same era, tempo/energy within 1, mood."""
    hit = {"era": False, "tempo": False, "mood": False}
    for ref in ref_rows:
        if row["era"] and row["era"] == ref["era"]:
            hit["era"] = True
        if (row["tempo_class"] and row["tempo_class"] == ref["tempo_class"]) or \
                (row["energy"] and ref["energy"] and abs(int(row["energy"]) - int(ref["energy"])) <= 1):
            hit["tempo"] = True
        if set(filter(None, row["mood"].split(";"))) & set(filter(None, ref["mood"].split(";"))):
            hit["mood"] = True
    if qf:
        hit["era"] |= bool(qf["era"]) and row["era"] in qf["era"]
        hit["tempo"] |= bool(qf["tempo"]) and row["tempo_class"] in qf["tempo"]
        hit["mood"] |= bool(qf["mood"]) and bool(set(row["mood"].split(";")) & qf["mood"])
    return hit


def _query_match(row: dict, qf: dict) -> bool:
    """For "next": a catalog song must fit every filter the words named."""
    def has(col, wanted):
        return not wanted or bool(set(filter(None, row[col].split(";"))) & wanted)
    return ((not qf["era"] or row["era"] in qf["era"])
            and (not qf["tempo"] or row["tempo_class"] in qf["tempo"])
            and (not qf["voice"] or row["voice_type"] in qf["voice"])
            and has("mood", qf["mood"]) and has("genre", qf["genre"])
            and has("theme", qf["theme"]) and has("situation", qf["situation"]))


# --- scoring --------------------------------------------------------------------------------

RELATION_WORD = {"like": "with this", "after": "after this", "next": "for this ask",
                 "plan": "in your plans"}


def score_candidate(c: dict, row: dict | None, W: dict, ref_keys: list[str], ref_rows: list[dict],
                    qf: dict | None, rel_rows: list[dict], request_type: str,
                    today: date | None = None) -> tuple[float, list[str]]:
    pts, why = 0.0, []
    if c.get("mix_rank"):
        r = c["mix_rank"]
        mp = W["mix_position"]
        p = mp["top5"] if r <= 5 else mp["top15"] if r <= 15 else mp["top40"] if r <= 40 else 0
        pts += p
        why.append(f"mix #{r}")
        if {"youtube", "youtube-music"} <= c["sources"]:
            pts += W["both_sources"]
            why.append("both")
        if len(c["mixes"]) > 1:
            pts += W["multiple_mixes_per_extra"] * (len(c["mixes"]) - 1)
            why.append(f"{len(c['mixes'])} mixes")
    if row:
        st = row["status"] or "wishlist"
        pts += W["status"].get(st, 0)
        why.append(st)
        try:
            crowd = float(row["crowd_score"]) if row["crowd_score"] else 0.0
        except ValueError:
            crowd = 0.0
        if crowd >= 2.5:
            pts += W["crowd_score"]["gte_2_5"]
            why.append(f"crowd {crowd:g}")
        elif crowd >= 2.0:
            pts += W["crowd_score"]["gte_2_0"]
            why.append(f"crowd {crowd:g}")
        sim = _similar(row, ref_rows, qf)
        if sim["era"]:
            pts += W["same_era"]
        if sim["tempo"]:
            pts += W["tempo_or_energy_within_1"]
        if sim["mood"]:
            pts += W["same_mood"]
        rp = rating_points(row["my_rating"], W["rating"])
        if row["my_rating"]:
            pts += rp
            why.append(f"⭐{row['my_rating']}")
    else:
        pts += W["status"].get("outside", 0)
        why.append("outside")
    rel, ups, downs = feedback.relation_score(ref_keys, c["key"], rel_rows, W["relation"], today)
    if ups or downs:
        pts += rel
        word = RELATION_WORD.get(request_type, "")
        if ups:
            why.append(f"👍×{ups} {word}")
        if downs:
            why.append(f"👎×{downs} {word}")
    return round(pts, 2), why


# --- suggest ----------------------------------------------------------------------------------

def _item(c: dict, row: dict | None, score: float, why: list[str]) -> dict:
    if row:
        return {"group": "catalog", "song_key": row["song_id"], "title": row["title"],
                "film": row["film"], "year": row["year"], "singers": row["singers"],
                "tempo_class": row["tempo_class"], "energy": row["energy"], "status": row["status"],
                "my_rating": row["my_rating"], "my_key_change": row["my_key_change"],
                "karaoke_url": row["karaoke_url"], "score": score, "reasons": " · ".join(why)}
    return {"group": "outside", "song_key": c["key"], "title": c["title"], "film": c["film"],
            "year": "", "singers": "", "tempo_class": "", "energy": "", "status": "",
            "my_rating": "", "my_key_change": "", "karaoke_url": c.get("karaoke_url", ""),
            "video_url": c.get("video_url", ""), "score": score, "reasons": " · ".join(why)}


def _norm(t: str) -> str:
    return youtube.normalize_title(re.sub(r"\(.*?\)", " ", t or ""))


def rank(mode: str, refs: list[dict], urls: list[str], query: str, exclude: list[str],
         rel_rows: list[dict], W: dict) -> dict:
    """Build and score candidates. Returns {catalog, outside, ref_keys, warnings}."""
    songs = catalog.load_songs()
    by_id = {r["song_id"]: r for r in songs}
    cands: dict[str, dict] = {}
    warnings, ref_keys = [], []
    if mode != "next":
        for i, ref in enumerate(refs):
            seed_key, warn = _collect_mix(ref, urls[i] if i < len(urls) else "", cands, i)
            if warn and warn not in warnings:
                warnings.append(warn)
            if not ref["key"]:
                ref["key"] = seed_key
            if ref["key"]:
                ref_keys.append(ref["key"])
    ref_rows = [r["row"] for r in refs if r["row"]]
    qf = query_filters(query) if (mode == "next" or not ref_rows) and query else None
    if mode == "next":
        ref_keys = [query_key(query)]
    # Catalog fill: songs like the reference(s) or fitting the request words.
    for row in songs:
        if row["song_id"] in cands or row["status"] == "retired":
            continue
        if mode == "next":
            if qf and _query_match(row, qf):
                cands[row["song_id"]] = {"key": row["song_id"], "group": "catalog",
                                         "title": row["title"], "sources": set(), "mixes": set()}
        elif ref_rows and any(_similar(row, [r], None)["mood"] and _similar(row, [r], None)["era"]
                              for r in ref_rows):
            cands[row["song_id"]] = {"key": row["song_id"], "group": "catalog",
                                     "title": row["title"], "sources": set(), "mixes": set()}

    excl_keys = {e.strip() for e in exclude if e and e.strip()}
    excl_titles = {_norm(e) for e in exclude if e} | {_norm(r["title"]) for r in refs}
    out = {"catalog": [], "outside": []}
    for key, c in cands.items():
        row = by_id.get(key) if c["group"] == "catalog" else None
        if c["group"] == "catalog" and (row is None or row["status"] == "retired"):
            continue
        title = row["title"] if row else c["title"]
        if key in excl_keys or key in ref_keys or _norm(title) in excl_titles:
            continue
        if mode == "plan" and row and row["my_rating"] and \
                int(row["my_rating"]) <= W["exclude_rating_at_or_below_in_plans"]:
            continue
        score, why = score_candidate(c, row, W, ref_keys, ref_rows, qf, rel_rows,
                                     "next" if mode == "next" else mode)
        out[c["group"]].append((score, c.get("mix_rank") or 999, title.lower(), c, row, why))
    for g in out:
        out[g].sort(key=lambda x: (-x[0], x[1], x[2]))
    # At most max_per_singer catalog songs per singer.
    cap, per_singer, kept = W["max_per_singer"], {}, []
    for t in out["catalog"]:
        singers = [s.strip().lower() for s in t[4]["singers"].split(";") if s.strip()]
        if cap and any(per_singer.get(s, 0) >= cap for s in singers):
            continue
        for s in singers:
            per_singer[s] = per_singer.get(s, 0) + 1
        kept.append(t)
    out["catalog"] = kept
    return {"catalog": [_item(t[3], t[4], t[0], t[5]) for t in out["catalog"]],
            "outside": [_item(t[3], None, t[0], t[5]) for t in out["outside"]],
            "ref_keys": ref_keys, "warnings": warnings}


def _fill_karaoke(items: list[dict]) -> None:
    """Outside items on the page: a karaoke link via karaoke_find when the mix
    didn't already offer one."""
    for it in items:
        if it["group"] != "outside" or it["karaoke_url"]:
            continue
        r = youtube.karaoke_find(it["title"], it["film"])
        best = (r.get("candidates") or [None])[0] if r.get("success") else None
        it["karaoke_url"] = best["url"] if best and best["score"] >= youtube.STRONG_INDEX_SCORE \
            else "no karaoke track found"


def page(req: dict) -> list[dict]:
    """Take the next page from a request's ranked groups, numbering continuously."""
    lc, lo = req["limits"]
    items = []
    for group, limit in (("catalog", lc), ("outside", lo)):
        start = req["cursor"][group]
        chunk = req["ranked"][group][start:start + limit]
        req["cursor"][group] = start + len(chunk)
        items.extend(copy.deepcopy(chunk))
    _fill_karaoke(items)
    for it in items:
        it["n"] = req["next_n"]
        req["numbers"][str(it["n"])] = it
        req["next_n"] += 1
    req["page_numbers"] = [it["n"] for it in items]
    return items


def suggest(mode: str, reference: str = "", url: str = "", references: list | str | None = None,
            query: str = "", limit_catalog: int = 10, limit_outside: int = 10,
            exclude: list | str | None = None, send_buttons: bool = True) -> dict:
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return fail("invalid_input", f"mode must be one of {', '.join(MODES)}.")
    refs_in = references if isinstance(references, list) else \
        [r for r in re.split(r"[;\n]", references or "") if r.strip()]
    exclude = exclude if isinstance(exclude, list) else \
        [e for e in re.split(r"[;\n]", exclude or "") if e.strip()]
    try:
        lc, lo = max(0, min(int(limit_catalog), 30)), max(0, min(int(limit_outside), 30))
    except (TypeError, ValueError):
        return fail("invalid_input", "limit_catalog and limit_outside must be whole numbers.")
    lc, lo = MODE_LIMITS.get(mode, (lc, lo))
    if mode in ("like", "after") and not (reference or url):
        return fail("invalid_input", f"mode={mode} needs a reference song or url.")
    if mode == "plan" and not refs_in:
        return fail("invalid_input", "mode=plan needs references (the songs in the plan).")
    if mode == "next" and not query:
        return fail("invalid_input", "mode=next needs query (e.g. 'peppy old').")

    W, warnings = load_weights()
    refs = [_resolve(r) for r in refs_in] if mode == "plan" else \
        ([_resolve(reference)] if reference else [{"key": "", "title": url, "row": None}]) \
        if mode != "next" else []
    urls = [url] if url and mode != "plan" else []
    rel_rows = feedback.load_relations()
    ranked = rank(mode, refs, urls, query, exclude, rel_rows, W)
    warnings += ranked["warnings"]
    if mode == "next":
        ref_key, ref_title = ranked["ref_keys"][0], query.strip()
    elif mode == "plan":
        ref_key, ref_title = ";".join(ranked["ref_keys"]), "; ".join(r["title"] for r in refs)
    else:
        ref_key, ref_title = (ranked["ref_keys"] or [""])[0], refs[0]["title"]
    req = {"request_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
           "created_at": datetime.now().isoformat(timespec="seconds"), "mode": mode,
           "request_text": query or reference or url or "; ".join(refs_in),
           "reference_key": ref_key, "reference_title": ref_title,
           "reference_keys": ranked["ref_keys"],
           "ranked": {"catalog": ranked["catalog"], "outside": ranked["outside"]},
           "cursor": {"catalog": 0, "outside": 0}, "limits": [lc, lo], "next_n": 1,
           "numbers": {}, "page_numbers": [], "taps": {}}
    items = page(req)
    feedback.push_request(req)
    sent = feedback.send_main_keyboard(req) if send_buttons and items else None
    if sent and not sent.get("success"):
        warnings.append(f"Buttons not sent: {sent.get('error')}")
    return ok(request_id=req["request_id"], mode=mode, reference_key=ref_key,
              reference_title=ref_title, items=items,
              remaining={g: len(req["ranked"][g]) - req["cursor"][g] for g in ("catalog", "outside")},
              buttons_sent=bool(sent and sent.get("success")), warnings=warnings)


def suggest_more(send_buttons: bool = True) -> dict:
    req = feedback.latest_request()
    if not req:
        return fail("no_request", "No suggestion list yet: ask for suggestions first.")
    items = page(req)
    feedback.save_request(req)
    if not items:
        return ok(request_id=req["request_id"], items=[], note="That's the whole list.",
                  buttons_sent=False)
    sent = feedback.send_main_keyboard(req) if send_buttons else None
    return ok(request_id=req["request_id"], mode=req["mode"], reference_title=req["reference_title"],
              items=items, buttons_sent=bool(sent and sent.get("success")),
              remaining={g: len(req["ranked"][g]) - req["cursor"][g] for g in ("catalog", "outside")},
              warnings=[] if not sent or sent.get("success") else [f"Buttons not sent: {sent.get('error')}"])
