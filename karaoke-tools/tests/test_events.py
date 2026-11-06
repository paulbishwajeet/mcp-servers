"""Event Planner (spec §5), offline: event files (round-trip, new fields),
instructions -> filters (rules and a mocked Haiku), playlist edits,
suggestions, auto-order, Create, feedback + finish (event_apply, skipped songs,
fits-after links, wishlist from audience requests), and the API guard."""

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import appstart
import catalog
import events
import feedback
import youtube
from app import instructions, main, similar
from test_app import HEADERS, entry

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=10)).isoformat()
PAST = (TODAY - timedelta(days=3)).isoformat()


@pytest.fixture(autouse=True)
def no_haiku(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("APP_URL", "https://kg.example.ts.net")
    similar.added_keys.clear()


@pytest.fixture
def client(wiki, telegram):
    return TestClient(main.app)


def add(**row):
    r = catalog.catalog_upsert(row)
    assert r["success"], r
    return r["song_id"]


@pytest.fixture
def songs(wiki):
    return {
        "opener": add(title="Aaj Ki Raat", film="Don", year=1978, voice_type="solo-female", tempo_class="fast",
                      energy=5, status="ready", situation="house-party;diwali", duration_sec=240),
        "duet": add(title="Gazab Ka Hai Yeh Din", film="QSQT", year=1988, voice_type="duet-mf",
                    tempo_class="medium", energy=3, status="ready", situation="diwali", duration_sec=280),
        "slow1": add(title="Pal Pal Dil Ke Paas", film="Blackmail", year=1973, voice_type="solo-male",
                     tempo_class="slow", energy=2, status="ready", duration_sec=250, singalong=2),
        "slow2": add(title="O Mere Dil Ke Chain", film="Mere Jeevan Saathi", year=1972, voice_type="solo-male",
                     tempo_class="slow", energy=2, status="practicing", duration_sec=270),
        "sing": add(title="Chhookar Mere Man Ko", film="Yaarana", year=1981, voice_type="solo-male",
                    tempo_class="medium", energy=3, status="ready", singalong=5, duration_sec=284),
        "low": add(title="Bad Song", year=1990, status="ready", my_rating=2, rated_on=TODAY.isoformat()),
        "new": add(title="Kesariya", film="Brahmastra", year=2022, voice_type="solo-male", status="ready",
                   tempo_class="slow", energy=2),
    }


def new_event(client, **fields):
    body = {"name": "Diwali Party", "date": FUTURE, "time": "19:30", "event_type": "festival",
            "theme": "diwali;house-party"} | fields
    r = client.post("/api/events", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- files ----------------------------------------------------------------------------------------

LEGACY = """---
event: Old Party
date: 2026-10-30
venue: friend's place
event_type: house-party        # festival | wedding | sangeet | birthday | house-party | community | open-mic | paid-gig | other
audience: friends
duration_min: 25
co_singers:
---

# Old Party

## Set list

Proposed order, not final.

| # | song_id | Title | Key change | Link used | Outcome | Notes |
|---|---------|-------|-----------|-----------|---------|-------|
| 1 | {a} | Pal Pal Dil Ke Paas (Blackmail, 1973, Kishore Kumar) · 4:10 | -2 | https://www.youtube.com/watch?v=pw6r-izZArA | good | Slow |
| 2 | {b} | Aaj Ki Raat (Don, 1978) | | | standout / good / weak | |

Estimated time: about 10 min.

## Backups

- kesariya-2022: Kesariya

## Audience requests

-

## What worked / what I'd change

-
"""


def test_round_trip_keeps_agent_text(wiki, songs):
    p = wiki / "events/2026-10-30-old-party.md"
    p.write_text(LEGACY.format(a=songs["slow1"], b=songs["opener"]))
    ev = events.load("2026-10-30-old-party")
    assert [s["key"] for s in ev["songs"]] == [songs["slow1"], songs["opener"]]
    assert ev["songs"][0]["title"] == "Pal Pal Dil Ke Paas" and ev["songs"][0]["outcome"] == "good"
    assert ev["songs"][1]["outcome"] == "" and ev["meta"]["event_type"] == "house-party"
    ev["songs"][1]["comment"] = "Crowd sang | along"
    ev["requests"] = ["Yeh Dosti"]
    events.save(ev, "test")
    text = p.read_text()
    for kept in ("Proposed order, not final.", "Estimated time: about 10 min.", "## Backups",
                 "- kesariya-2022: Kesariya", "Pal Pal Dil Ke Paas (Blackmail, 1973, Kishore Kumar) · 4:10",
                 "| -2 |", "venue: friend's place", "- Yeh Dosti"):
        assert kept in text, kept
    assert "Crowd sang / along" in text  # a pipe can't break the table
    assert "parsed_filters" not in text and "hidden" not in text  # nothing new until used
    again = events.load("2026-10-30-old-party")
    ev["songs"][1]["comment"] = "Crowd sang / along"
    assert again["songs"] == ev["songs"] and again["requests"] == ["Yeh Dosti"]
    # The agent's tool still reads it.
    assert catalog.parse_event(p)["rows"][0]["song_id"] == songs["slow1"]


def test_create_validates_and_writes_a_draft(client, songs):
    r = client.post("/api/events", headers=HEADERS, json={"name": "", "date": "2026-13-01", "time": "25:00"})
    assert r.status_code == 422
    assert set(r.json()["field_errors"]) == {"name", "date", "time"}
    ev = new_event(client, instructions="45 minutes, mostly 70s-80s, 2 duets, start peppy, end with a singalong")
    assert ev["status"] == "draft" and ev["id"] == f"{FUTURE}-diwali-party"
    assert ev["filters"] == {"target_min": 45, "duets": 2, "eras": ["1970s", "1980s"], "genres": ["peppy"],
                             "arc_start": "high", "arc_end": "singalong"}
    assert ev["filters_source"] == "rules"
    assert [c["label"] for c in ev["chips"]][:3] == ["45 min", "1970s–80s", "2 duets"]
    text = (catalog.wiki_path(f"events/{ev['id']}.md")).read_text()
    assert "status: draft" in text and "## Set list" in text and "## Audience requests" in text
    assert 'instructions: "45 minutes' in text
    # Same name and date again: a new file, not an overwrite.
    assert new_event(client)["id"] == f"{FUTURE}-diwali-party-2"


def test_haiku_output_is_only_data(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    sent = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "tool_use", "name": "set_filters", "input": {
                "target_min": 45, "eras": ["1970s", "1880s"], "voice_types": ["duet-mf", "rap"],
                "arc_end": "singalong", "must_include": ["Yeh Dosti"], "song_count": 999,
                "evil": "rm -rf /"}}]}

    def fake_post(url, **kw):
        sent["url"], sent["headers"], sent["json"] = url, kw["headers"], kw["json"]
        return R()
    monkeypatch.setattr(instructions.requests, "post", fake_post)
    f, source = instructions.parse("45 min, 70s, a duet, singalong end, must have Yeh Dosti")
    assert source == "haiku" and sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["json"]["model"].startswith("claude-haiku") and sent["json"]["tool_choice"]["name"] == "set_filters"
    assert f == {"target_min": 45, "eras": ["1970s"], "voice_types": ["duet-mf"], "arc_end": "singalong",
                 "must_include": ["Yeh Dosti"]}


def test_haiku_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class R:
        status_code = 529
    monkeypatch.setattr(instructions.requests, "post", lambda *a, **k: R())
    f, source = instructions.parse("an hour of 90s songs")
    assert source == "rules" and f == {"target_min": 60, "eras": ["1990s"]}


def test_chips_can_be_removed(client, songs):
    ev = new_event(client, instructions="2 duets, 70s, singalong end")
    r = client.post(f"/api/events/{ev['id']}/chips/remove", headers=HEADERS, json={"chip": "duets"}).json()
    assert "duets" not in r["filters"] and r["filters"]["eras"] == ["1970s"]
    r = client.post(f"/api/events/{ev['id']}/chips/remove", headers=HEADERS, json={"chip": "eras:1970s"}).json()
    assert "eras" not in r["filters"]


# --- playlist ---------------------------------------------------------------------------------

def test_playlist_add_remove_order_and_summary(client, songs):
    ev = new_event(client, duration_min="20", instructions="1 duet")
    eid = ev["id"]
    for key in (songs["slow1"], songs["duet"], songs["opener"]):
        r = client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": key})
        assert r.status_code == 200
    assert client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": songs["duet"]}).status_code == 409
    r = client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={
        "song_key": "yt:outsideAAA1", "title": "Chalte Chalte", "karaoke_url": "https://www.youtube.com/watch?v=karaokeCC01"}).json()
    assert [i["key"] for i in r["playlist"]] == [songs["slow1"], songs["duet"], songs["opener"], "yt:outsideAAA1"]
    out = r["playlist"][3]
    assert out["group"] == "outside" and "outside catalog" in out["warnings"]
    sm = r["summary"]
    assert sm["songs"] == 4 and sm["duets"] == 1 and sm["duets_wanted"] == 1 and sm["target_min"] == 20
    assert sm["estimated"] and sm["total_sec"] == 250 + 280 + 240 + 270 + 4 * events.CHANGEOVER_SEC
    assert sm["energy"] == [2, 3, 5, None]
    keys = [songs["opener"], "yt:outsideAAA1", songs["slow1"], songs["duet"]]
    r = client.put(f"/api/events/{eid}/order", headers=HEADERS, json={"keys": keys}).json()
    assert [i["key"] for i in r["playlist"]] == keys
    assert client.put(f"/api/events/{eid}/order", headers=HEADERS, json={"keys": keys[:2]}).status_code == 409
    r = client.delete(f"/api/events/{eid}/songs/yt:outsideAAA1", headers=HEADERS).json()
    assert len(r["playlist"]) == 3
    text = catalog.wiki_path(f"events/{eid}.md").read_text()
    assert f"| 1 | {songs['opener']} | Aaj Ki Raat (Don, 1978) |" in text
    assert "| added" not in text and "event-edit" in catalog.wiki_path("log.md").read_text()
    assert client.post(f"/api/events/{eid}/songs", headers=HEADERS,
                       json={"song_key": "yt:outsideAAA1", "karaoke_url": "https://evil.example/x"}).status_code == 400


def test_practicing_and_recently_sung_warnings(client, songs, wiki):
    (wiki / f"events/{PAST}-last-diwali.md").write_text(
        LEGACY.replace("date: 2026-10-30", f"date: {PAST}").replace("house-party ", "festival ")
        .format(a=songs["slow1"], b=songs["opener"]))
    ev = new_event(client)
    r = client.post(f"/api/events/{ev['id']}/songs", headers=HEADERS, json={"song_key": songs["opener"]}).json()
    assert any(w.startswith("sung at Old Party") for w in r["playlist"][0]["warnings"])
    r = client.post(f"/api/events/{ev['id']}/songs", headers=HEADERS, json={"song_key": songs["slow2"]}).json()
    assert "practicing" in r["playlist"][1]["warnings"]


def test_suggestions_rank_theme_and_skip_hidden_low_and_taken(client, songs, wiki):
    ev = new_event(client, instructions="mostly 80s, 1 duet")
    eid = ev["id"]
    s = client.get(f"/api/events/{eid}/suggestions", headers=HEADERS).json()
    keys = [i["song_key"] for i in s["items"]]
    assert keys[0] == songs["duet"]  # diwali situation + 1980s + the wanted duet
    assert "diwali" in s["items"][0]["reasons"] and "duet" in s["items"][0]["reasons"]
    assert songs["low"] not in keys  # rated 2: left out like in plans
    client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": songs["duet"]})
    client.post(f"/api/events/{eid}/hide", headers=HEADERS, json={"song_key": songs["opener"]})
    keys = [i["song_key"] for i in client.get(f"/api/events/{eid}/suggestions", headers=HEADERS).json()["items"]]
    assert songs["duet"] not in keys and songs["opener"] not in keys
    # Hidden is per event: another event still suggests it.
    other = new_event(client, name="Another")
    assert songs["opener"] in [i["song_key"] for i in
                               client.get(f"/api/events/{other['id']}/suggestions", headers=HEADERS).json()["items"]]


def test_recently_sung_at_a_similar_event_ranks_lower(songs, wiki, client):
    ev = new_event(client)
    score = lambda items: next(i["score"] for i in items if i["song_key"] == songs["opener"])  # noqa: E731
    before = client.get(f"/api/events/{ev['id']}/suggestions", headers=HEADERS).json()["items"]
    (wiki / f"events/{PAST}-last-diwali.md").write_text(
        LEGACY.replace("date: 2026-10-30", f"date: {PAST}").replace("house-party ", "festival ")
        .format(a=songs["slow1"], b=songs["opener"]))
    after = client.get(f"/api/events/{ev['id']}/suggestions", headers=HEADERS).json()["items"]
    top = next(i for i in after if i["song_key"] == songs["opener"])
    assert "sung at Old Party" in top["reasons"]
    assert score(after) == score(before) - 3  # recent_similar_event


def test_avoid_and_must_include(songs, wiki, client):
    ev = new_event(client)
    with events._lock:
        e = events.load(ev["id"])
        e["filters"] = {"avoid": ["peppy", "Aaj Ki Raat"], "must_include": ["Kesariya"]}
        events.save(e, "test")
    items = client.get(f"/api/events/{ev['id']}/suggestions", headers=HEADERS).json()["items"]
    assert items[0]["song_key"] == songs["new"] and "asked for" in items[0]["reasons"]
    assert songs["opener"] not in [i["song_key"] for i in items]


def test_auto_order_follows_the_arc(client, songs):
    ev = new_event(client, instructions="start peppy, end with a singalong")
    eid = ev["id"]
    for key in (songs["slow1"], songs["sing"], songs["slow2"], songs["duet"], songs["opener"]):
        client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": key})
    r = client.post(f"/api/events/{eid}/autoorder", headers=HEADERS).json()
    order = [i["key"] for i in r["playlist"]]
    assert order[0] == songs["opener"] and order[-1] == songs["sing"]
    tempos = [i["tempo_class"] for i in r["playlist"]]
    assert all(not (a == b == "slow") for a, b in zip(tempos, tempos[1:])), tempos
    assert order.index(songs["slow2"]) < order.index(songs["slow1"])  # practicing (harder) earlier


# --- create + feedback ------------------------------------------------------------------------

def test_create_locks_in_and_sends_one_line(client, songs, telegram):
    ev = new_event(client)
    eid = ev["id"]
    assert client.post(f"/api/events/{eid}/create", headers=HEADERS).status_code == 400  # empty
    client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": songs["opener"]})
    r = client.post(f"/api/events/{eid}/create", headers=HEADERS).json()
    assert r["status"] == "planned" and r["sent"]
    assert telegram.messages[-1]["text"].startswith(f"Planned: Diwali Party · {FUTURE} 19:30 · 1 songs")
    # Planned events stay editable and planned.
    r = client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": songs["duet"]}).json()
    assert r["status"] == "planned" and len(r["playlist"]) == 2


def test_feedback_finish_updates_catalog_links_and_wishlist(client, songs, telegram, wiki):
    ev = new_event(client, date=PAST)
    eid = ev["id"]
    order = [songs["opener"], songs["duet"], songs["slow1"], songs["sing"]]
    for key in order:
        client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": key})
    client.post(f"/api/events/{eid}/create", headers=HEADERS)
    fb = lambda body: client.put(f"/api/events/{eid}/feedback", headers=HEADERS, json=body)  # noqa: E731
    assert fb({"song_key": songs["opener"], "outcome": "great"}).status_code == 422
    fb({"song_key": songs["opener"], "outcome": "standout", "comment": "Everyone danced"})
    fb({"song_key": songs["duet"], "outcome": "good"})
    fb({"song_key": songs["slow1"], "skipped": True})
    fb({"song_key": songs["sing"], "outcome": "weak"})
    fb({"requests": ["Yeh Dosti (Sholay)", "Pal Pal Dil Ke Paas", ""], "notes": ["Start earlier"]})
    r = client.post(f"/api/events/{eid}/finish", headers=HEADERS).json()
    assert r["status"] == "performed" and r["applied"]
    rows = {x["song_id"]: x for x in catalog.load_songs()}
    assert rows[songs["opener"]]["times_performed"] == "1" and rows[songs["opener"]]["crowd_score"] == "3"
    assert rows[songs["slow1"]]["times_performed"] in ("", "0")   # skipped: not sung
    assert rows[songs["sing"]]["crowd_score"] == "1" and rows[songs["sing"]]["last_performed"] == PAST
    rel = feedback.load_relations()
    assert feedback.is_liked(songs["opener"], songs["duet"], rel, both_ways=False)  # standout -> good
    assert not feedback.is_liked(songs["duet"], songs["sing"], rel)                 # weak breaks it
    wish = [x for x in catalog.load_songs() if x["title"] == "Yeh Dosti"]
    assert len(wish) == 1 and wish[0]["status"] == "wishlist" and wish[0]["needs_review"] == "true"
    assert r["wishlist_added"] == [wish[0]["song_id"]]  # Pal Pal is already in the catalog
    text = telegram.messages[-1]["text"]
    assert text.startswith("Feedback saved: Diwali Party · 1 standout, 1 good, 1 weak, 1 skipped")
    file = catalog.wiki_path(f"events/{eid}.md").read_text()
    assert "Everyone danced" in file and "- Start earlier" in file and "status: performed" in file
    # Performed: the playlist is locked, feedback still changes, and re-finishing is safe.
    assert client.post(f"/api/events/{eid}/songs", headers=HEADERS, json={"song_key": songs["new"]}).status_code == 409
    fb({"song_key": songs["duet"], "outcome": "weak"})
    r2 = client.post(f"/api/events/{eid}/finish", headers=HEADERS).json()
    assert r2["links"] == {"added": 0, "removed": 1}
    assert not feedback.is_liked(songs["opener"], songs["duet"], feedback.load_relations())
    assert r2["wishlist_added"] == []
    assert len([x for x in catalog.load_songs() if x["title"] == "Yeh Dosti"]) == 1


def test_event_apply_ignores_unplayed_events_and_marks_target(wiki, songs):
    planned = LEGACY.format(a=songs["slow1"], b=songs["opener"]).replace(
        "co_singers:\n", "co_singers:\nstatus: planned\n").replace("date: 2026-10-30", f"date: {FUTURE}")
    (wiki / f"events/{FUTURE}-future.md").write_text(planned)
    (wiki / "events/2026-10-30-old.md").write_text(LEGACY.format(a=songs["slow1"], b=songs["opener"]))
    r = catalog.event_apply("2026-10-30-old.md")
    assert r["success"]
    rows = {x["song_id"]: x for x in catalog.load_songs()}
    assert rows[songs["slow1"]]["times_performed"] == "1"  # the planned one doesn't count
    r = catalog.event_apply(f"{FUTURE}-future.md")  # applying it means it happened
    assert "status: performed" in (wiki / f"events/{FUTURE}-future.md").read_text()
    assert {x["song_id"]: x for x in catalog.load_songs()}[songs["slow1"]]["times_performed"] == "2"


def test_added_outside_song_is_rekeyed_in_event_playlists(client, songs):
    ev = new_event(client)
    client.post(f"/api/events/{ev['id']}/songs", headers=HEADERS, json={
        "song_key": "yt:outsideAAA1", "title": "Chalte Chalte", "karaoke_url": "https://www.youtube.com/watch?v=karaokeCC01"})
    sid = add(title="Chalte Chalte", film="Chalte Chalte", year=1976, status="wishlist")
    assert events.rekey_all("yt:outsideAAA1", sid) == 1
    e = events.load(ev["id"])
    assert e["songs"][0]["key"] == sid and e["songs"][0]["link"].endswith("karaokeCC01")


def test_ideas_from_playlist_mixes(client, songs, monkeypatch):
    def fake_mix(song="", url="", seed="karaoke", source="youtube", limit=25, film=""):
        return {"success": True, "seed": {"video_id": "seedVIDEO01", "title": song, "song_title": song,
                                          "channel": "X", "url": ""},
                "entries": [entry(1, "Chalte Chalte", vid="outsideAAA1"),
                            entry(2, "Only In One", vid=f"only{song[:7].replace(' ', '')}xx"[:15].ljust(11, "x")[:11])]}
    monkeypatch.setattr(youtube, "youtube_mix", fake_mix)
    monkeypatch.setattr(youtube, "karaoke_find", lambda *a, **k: {"success": True, "candidates": []})
    ev = new_event(client)
    r = client.get(f"/api/events/{ev['id']}/ideas", headers=HEADERS).json()
    assert r["items"] == [] and r["note"]
    for key in (songs["slow1"], songs["sing"]):
        client.post(f"/api/events/{ev['id']}/songs", headers=HEADERS, json={"song_key": key})
    r = client.get(f"/api/events/{ev['id']}/ideas", headers=HEADERS).json()
    assert r["status"] == "done"
    first = r["items"][0]
    assert first["title"] == "Chalte Chalte" and "2 mixes" in first["reasons"]  # suggested by both songs
    assert first["karaoke"] == "none"


def test_events_api_needs_init_data(client):
    for method, path in (("GET", "/api/events"), ("POST", "/api/events"), ("GET", "/api/events/x"),
                         ("POST", "/api/events/x/finish")):
        assert client.request(method, path).status_code == 401


def test_bad_event_ids_are_404(client):
    for eid in ("..%2F..%2FSCHEMA", "_TEMPLATE", "Upper"):
        assert client.get(f"/api/events/{eid}", headers=HEADERS).status_code == 404


def test_open_app_event_views(client, songs, telegram):
    ev = new_event(client, date=PAST, name="Last Night")
    r = appstart.open_app("feedback")
    assert r["sent"] and telegram.messages[-1]["text"] == "Feedback: Last Night"
    assert appstart.get_code(r["code"]) == {"view": "feedback", "seed": ev["id"], "url": ""}
    assert appstart.open_app("new_event")["sent"] and telegram.messages[-1]["text"] == "Plan a new event"
    assert appstart.open_app("event", seed="nope")["error_code"] == "not_found"


def test_list_upcoming_and_past(client, songs):
    new_event(client, name="Soon")
    new_event(client, name="Done", date=PAST)
    r = client.get("/api/events", headers=HEADERS).json()
    assert [e["name"] for e in r["upcoming"]] == ["Soon"]
    assert r["past"][0]["name"] == "Done" and r["past"][0]["needs_feedback"] is True
    assert json.dumps(r)  # serialisable
