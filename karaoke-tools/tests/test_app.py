"""Mini App server (app/), open_app and the menu button, offline: initData
auth, the API guard, start codes, rating -> status, likes, edits, adding,
Suggest Similar with a fake mix, karaoke resolution and the close summary.
Telegram is the FakeTelegram from conftest; YouTube is faked."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import appstart
import cache
import catalog
import feedback
import tg
import youtube
from app import auth, jobs, main, similar
from app.config import load_env_files, parse_env

TOKEN = "123456:TEST-token"
ROOT = Path(__file__).resolve().parent.parent


def init_data(user_id=4242, age=0, token=TOKEN, **extra) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAHtest",
              "user": json.dumps({"id": user_id, "first_name": "Neel"}, separators=(",", ":"))} | extra
    return urlencode(fields | {"hash": auth.sign(fields, token)})


HEADERS = {"X-Telegram-Init-Data": init_data(), "X-App-Session": "sess-0001"}


@pytest.fixture(autouse=True)
def fresh_app_state(monkeypatch):
    similar._sessions.clear()
    similar.added_keys.clear()
    main._activity.clear()
    main._embed_cache.clear()
    main._original_cache.clear()
    jobs._jobs.clear()
    monkeypatch.setenv("APP_URL", "https://kg.example.ts.net")
    yield


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
        "ref": add(title="Pal Pal Dil Ke Paas", film="Blackmail", year=1973, singers="Kishore Kumar",
                   era="1970s", tempo_class="slow", energy=2, mood="romantic;longing", status="ready",
                   karaoke_url="https://www.youtube.com/watch?v=pw6r-izZArA"),
        "chain": add(title="O Mere Dil Ke Chain", film="Mere Jeevan Saathi", year=1972,
                     singers="Kishore Kumar", tempo_class="slow", energy=2, mood="romantic",
                     status="wishlist", karaoke_url="https://www.youtube.com/watch?v=aaaaaaaaaa1"),
        "shaam": add(title="Yeh Shaam Mastani", film="Kati Patang", year=1970, singers="Kishore Kumar",
                     tempo_class="medium", energy=3, mood="romantic", status="practicing"),
    }


def entry(rank, title, in_catalog="", vid="xxxxxxxxxxx", kind="original"):
    return {"rank": rank, "song_title": title, "film": "", "kind": kind, "sources": ["youtube"],
            "in_catalog": in_catalog, "status": "", "match_score": 100.0 if in_catalog else 40.0,
            "videos": [{"url": f"https://www.youtube.com/watch?v={vid}", "title": title,
                        "channel": "X", "duration_sec": 300, "kind": kind}]}


@pytest.fixture
def mix(songs, monkeypatch):
    calls = []

    def fake_mix(song="", url="", seed="karaoke", source="youtube", limit=25, film=""):
        calls.append({"song": song, "seed": seed, "film": film})
        return {"success": True, "seed": {"video_id": "seedVIDEO01", "title": "Pal Pal Dil Ke Paas",
                                          "song_title": "Pal Pal Dil Ke Paas", "channel": "X", "url": url},
                "entries": [entry(1, "Yeh Shaam Mastani", songs["shaam"]),
                            entry(2, "Chalte Chalte", vid="outsideAAA1"),
                            entry(3, "Kya Hua Tera Wada", vid="outsideBBB2")]}

    finds = []

    def fake_find(title, film="", singers="", max_results=5):
        finds.append(title)
        if title == "Chalte Chalte":
            return {"success": True, "candidates": [
                {"url": "https://www.youtube.com/watch?v=karaokeCC01", "title": "Chalte Chalte Karaoke",
                 "channel": "Anil Chauhan", "score": 95.0, "source": "index", "duration_sec": 300}]}
        return {"success": True, "candidates": []}

    monkeypatch.setattr(youtube, "youtube_mix", fake_mix)
    monkeypatch.setattr(youtube, "karaoke_find", fake_find)
    return {"mix": calls, "finds": finds}


# --- initData ------------------------------------------------------------------------------

def test_init_data_valid():
    user = auth.validate(init_data(), TOKEN, "4242")
    assert user["id"] == 4242


@pytest.mark.parametrize("data,why", [
    (init_data().replace("Neel", "Eve!"), "bad signature"),           # tampered user
    (init_data(token="999:other-token"), "bad signature"),            # signed with another bot
    (init_data(age=25 * 3600), "expired"),                           # older than 24 h
    (init_data(age=-3600), "expired"),                               # from the future
    (init_data(user_id=777), "user not allowed"),                     # someone else
    ("", "missing init data"),
    ("user=%7B%7D&auth_date=1", "bad signature"),                     # no hash
])
def test_init_data_refused(data, why):
    with pytest.raises(auth.AuthError, match=why):
        auth.validate(data, TOKEN, "4242")


def test_init_data_extra_fields_are_signed_too():
    data = init_data(signature="abc", chat_type="sender")
    assert auth.validate(data, TOKEN, "4242")["id"] == 4242
    assert pytest.raises(auth.AuthError, auth.validate, data.replace("sender", "private"), TOKEN, "4242")


# --- API guard ------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/home"), ("GET", "/api/vocab"), ("GET", "/api/search?q=pal"),
    ("GET", "/api/similar?seed=x"), ("POST", "/api/like"), ("POST", "/api/rating"),
    ("GET", "/api/song/abc"), ("PUT", "/api/song/abc"), ("POST", "/api/song/add"),
    ("GET", "/api/jobs/abcdefabcdef"), ("POST", "/api/session/close"), ("GET", "/api/start/abcdefgh"),
])
def test_every_api_call_needs_init_data(client, method, path):
    # A desktop browser opening the URL: no initData -> 401, and a forged one too.
    assert client.request(method, path).status_code == 401
    bad = {"X-Telegram-Init-Data": init_data(user_id=1)}
    assert client.request(method, path, headers=bad, json={}).status_code == 401


def test_static_page_is_public_but_holds_no_data(client):
    r = client.get("/")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/home").status_code == 401


def test_no_docs_or_openapi(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- start codes + open_app ------------------------------------------------------------------

def test_open_app_sends_web_app_button_to_the_allowed_chat_only(wiki, telegram):
    r = appstart.open_app("similar", seed="Pal Pal Dil Ke Paas")
    assert r["success"] and r["sent"]
    code = r["code"]
    assert 8 <= len(code) <= 12 and appstart.CODE_RE.match(code)
    method, payload = telegram.calls[-1]
    assert method == "sendMessage" and payload["chat_id"] == "4242"
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button == {"text": "🎵 Open", "web_app": {"url": f"https://kg.example.ts.net/?start={code}"}}
    assert "callback_data" not in button
    assert appstart.get_code(code) == {"view": "similar", "seed": "Pal Pal Dil Ke Paas", "url": ""}


def test_open_app_validation(wiki, telegram, monkeypatch):
    assert appstart.open_app("events")["error_code"] == "invalid_input"
    assert appstart.open_app("similar")["error_code"] == "invalid_input"
    assert appstart.open_app("similar", url="https://evil.example/x")["error_code"] == "url_not_allowed"
    monkeypatch.setenv("APP_URL", "http://insecure.example")
    assert appstart.open_app("home")["error_code"] == "app_not_configured"
    assert telegram.calls == []


def test_open_app_via_mcp_tool(wiki, telegram):
    import server
    r = server.TOOLS["open_app"](view="home")
    assert r["success"] and r["sent"]
    assert telegram.messages[-1]["text"] == "KaraokeGayak"


def test_start_codes_expire(wiki, client):
    code = appstart.new_code("similar", "Tum Hi Ho")
    r = client.get(f"/api/start/{code}", headers=HEADERS)
    assert r.status_code == 200 and r.json()["seed"] == "Tum Hi Ho"
    codes = json.loads(appstart._path().read_text())
    codes[code]["at"] -= appstart.CODE_TTL + 1  # 7 days and a second old
    appstart._path().write_text(json.dumps(codes))
    assert client.get(f"/api/start/{code}", headers=HEADERS).status_code == 404
    assert client.get("/api/start/..%2F..%2Fetc", headers=HEADERS).status_code == 404


def test_menu_button(wiki, telegram):
    r = tg.set_menu_button("🎵 KaraokeGayak", "https://kg.example.ts.net/")
    assert r["success"]
    method, payload = telegram.calls[-1]
    assert method == "setChatMenuButton"
    assert payload == {"chat_id": "4242", "menu_button": {"type": "web_app", "text": "🎵 KaraokeGayak",
                                                          "web_app": {"url": "https://kg.example.ts.net/"}}}
    assert tg.set_menu_button("x", "http://plain")["error_code"] == "invalid_input"


def test_never_get_updates_or_webhook(wiki, telegram):
    for method in ("getUpdates", "setWebhook", "deleteWebhook", "sendPoll", "stopPoll"):
        assert tg._call(method, {})["error_code"] == "not_allowed"
    assert tg.ALLOWED_METHODS == {"sendMessage", "setChatMenuButton"}
    # No code names those methods as a string (docs and comments may mention them).
    call = re.compile(r"""["'/](getUpdates|setWebhook|deleteWebhook)["']""")
    for f in [*ROOT.glob("*.py"), *ROOT.glob("app/*.py"), ROOT / "app/static/app.js"]:
        assert not call.search(f.read_text()), f


# --- env files --------------------------------------------------------------------------------

def test_env_files_only_known_keys_and_process_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN='111:abc'\nTELEGRAM_HOME_CHANNEL=99\nOPENAI_API_KEY=nope\n"
                   "export APP_URL=https://x.ts.net\n# comment\n")
    assert parse_env(env.read_text())["TELEGRAM_BOT_TOKEN"] == "111:abc"
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APP_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APP_URL", "https://kept.ts.net")
    applied = load_env_files(str(env))
    import os
    assert sorted(applied) == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    assert os.environ["TELEGRAM_CHAT_ID"] == "99" and os.environ["APP_URL"] == "https://kept.ts.net"
    assert "OPENAI_API_KEY" not in os.environ


# --- rating -> status ------------------------------------------------------------------------------

def test_rating_drives_status(client, songs):
    sid = songs["chain"]
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": sid, "rating": 8}).json()
    assert r["success"] and r["status"] == "ready" and r["my_rating"] == "8"
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": sid, "rating": 4}).json()
    assert r["status"] == "practicing"
    row = catalog.catalog_get(sid)["row"]
    assert row["rated_on"]
    # Clearing leaves status alone.
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": sid, "rating": None}).json()
    assert r["my_rating"] == "" and r["status"] == "practicing"
    assert catalog.catalog_get(sid)["row"]["rated_on"] == ""
    # A hand-set status stands until the next rating change.
    assert catalog.catalog_upsert({"song_id": sid, "status": "wishlist"})["success"]
    assert catalog.catalog_upsert({"song_id": sid, "notes": "x"})["row"]["status"] == "wishlist"
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": sid, "rating": 10}).json()
    assert r["status"] == "ready"


@pytest.mark.parametrize("bad", [0, 11, "7", 7.5, True])
def test_rating_rejects_bad_values(client, songs, bad):
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": songs["chain"], "rating": bad})
    assert r.status_code == 400 and "my_rating" in r.json()["field_errors"]


def test_rating_an_outside_song_needs_add(client, songs):
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": "yt:outsideAAA1", "rating": 9}).json()
    assert r == {"success": True, "needs_add": True, "song_key": "yt:outsideAAA1", "adding": False}
    assert client.post("/api/rating", headers=HEADERS,
                       json={"song_key": "../etc/passwd", "rating": 9}).status_code == 400


# --- likes ---------------------------------------------------------------------------------------

def test_like_unlike_latest_wins_and_both_ways(client, songs):
    # Rows in the same second: the later row in the file wins.
    a, b = songs["ref"], songs["chain"]
    body = {"seed_key": a, "song_key": b, "liked": True, "seed_title": "Pal Pal", "song_title": "Chain"}
    assert client.post("/api/like", headers=HEADERS, json=body).json()["liked"] is True
    rows = feedback.load_relations()
    assert rows[-1]["verdict"] == "like" and rows[-1]["request_type"] == "like"
    assert feedback.is_liked(a, b, rows) and feedback.is_liked(b, a, rows)  # both ways
    assert not feedback.is_liked(b, a, rows, both_ways=False)              # directed for "after"
    client.post("/api/like", headers=HEADERS, json=body | {"liked": False})
    rows = feedback.load_relations()
    assert len(rows) == 2 and not feedback.is_liked(a, b, rows)            # append-only, latest wins
    client.post("/api/like", headers=HEADERS, json=body | {"liked": True})
    assert feedback.is_liked(a, b, feedback.load_relations())


def test_old_up_rows_count_as_likes_and_down_is_a_small_fading_penalty():
    W = {"like_link": 3, "down_penalty": -1, "relation": {"cap": 6, "half_life_days": 180}}
    from datetime import date
    rows = [{"logged_at": "2026-05-09 10:00:00", "reference_key": "a", "song_key": "b", "verdict": "up"},
            {"logged_at": "2026-11-05 10:00:00", "reference_key": "a", "song_key": "c", "verdict": "down"},
            {"logged_at": "2026-05-09 10:00:00", "reference_key": "a", "song_key": "c", "verdict": "down"}]
    assert feedback.link_score("a", "b", rows, W, today=date(2026, 11, 5)) == (3, True, 0)
    pts, liked, downs = feedback.link_score("a", "c", rows, W, today=date(2026, 11, 5))
    assert not liked and downs == 2 and pts == pytest.approx(-1.5, abs=0.01)


def test_like_rejects_bad_keys(client, songs):
    for body in ({"seed_key": "a b", "song_key": songs["ref"]}, {"seed_key": songs["ref"], "song_key": "yt:short"},
                 {"seed_key": songs["ref"], "song_key": songs["ref"]}):
        assert client.post("/api/like", headers=HEADERS, json=body | {"liked": True}).status_code == 400


# --- edit ---------------------------------------------------------------------------------------

def test_edit_field_errors(client, songs):
    sid = songs["chain"]
    r = client.put(f"/api/song/{sid}", headers=HEADERS, json={"fields": {"energy": 9, "mood": "grumpy"}})
    assert r.status_code == 422
    errs = r.json()["field_errors"]
    assert set(errs) == {"energy", "mood"}
    r = client.put(f"/api/song/{sid}", headers=HEADERS,
                   json={"fields": {"song_id": "x", "lyrics_file": "/etc/passwd",
                                    "karaoke_url": "https://evil.example/v"}})
    assert set(r.json()["field_errors"]) == {"song_id", "lyrics_file", "karaoke_url"}
    assert catalog.catalog_get(sid)["row"]["energy"] == "2"


def test_edit_saves_and_reports_karaoke_change(client, songs):
    sid = songs["shaam"]
    r = client.put(f"/api/song/{sid}", headers=HEADERS, json={"fields": {
        "genre": "romantic-ballad;sad", "karaoke_url": "https://www.youtube.com/watch?v=newKaraoke1",
        "my_rating": "7"}}).json()
    assert r["success"] and r["karaoke_changed"]
    row = catalog.catalog_get(sid)["row"]
    assert row["genre"] == "romantic-ballad;sad" and row["status"] == "ready" and row["rated_on"]
    assert f"| update | {sid} |" in catalog.wiki_path("log.md").read_text()


def test_get_song_catalog_and_outside_draft(client, songs):
    r = client.get(f"/api/song/{songs['ref']}", headers=HEADERS).json()
    assert r["in_catalog"] and r["row"]["title"] == "Pal Pal Dil Ke Paas"
    r = client.get("/api/song/yt:outsideAAA1?title=Chalte%20Chalte&karaoke_url=https://evil.example/x",
                   headers=HEADERS).json()
    assert not r["in_catalog"] and r["row"]["title"] == "Chalte Chalte" and r["row"]["karaoke_url"] == ""
    assert client.get("/api/song/not-here", headers=HEADERS).status_code == 404


def test_vocab_matches_schema(client):
    v = client.get("/api/vocab", headers=HEADERS).json()
    import vocab
    assert v["multi"]["mood"] == vocab.MOOD and v["enums"]["status"] == vocab.STATUS


# --- add a song ------------------------------------------------------------------------------------

def test_add_validates_up_front(client, songs):
    r = client.post("/api/song/add", headers=HEADERS, json={"song_key": "yt:outsideAAA1", "fields": {"film": "x"}})
    assert r.status_code == 422 and "title" in r.json()["field_errors"]
    r = client.post("/api/song/add", headers=HEADERS,
                    json={"song_key": "yt:outsideAAA1", "fields": {"title": "X", "energy": 7}})
    assert "energy" in r.json()["field_errors"]


def test_add_job_runs_the_pipeline_and_moves_likes(client, songs, monkeypatch):
    import audio
    import lookup
    import lyrics
    feedback.set_like(songs["ref"], "yt:outsideAAA1", True)
    monkeypatch.setattr(lookup, "song_lookup", lambda t, f="", y="": {
        "success": True, "found": True, "page_title": "Chalte Chalte (1976 film)", "release_year": 1976,
        "infobox": {"music_by": "Bappi Lahiri"}})
    monkeypatch.setattr(cache, "karaoke_many", lambda songs_: [
        {"url": "https://www.youtube.com/watch?v=karaokeCC01", "title": "Chalte Chalte Karaoke",
         "channel": "Anil Chauhan", "source": "index"}])
    monkeypatch.setattr(youtube, "youtube_fetch_audio", lambda url: {
        "success": True, "job_id": "20261105-101010-abcdef12", "audio_path": "/tmp/x.mp3"})
    cleaned = []
    monkeypatch.setattr(youtube, "cleanup_job", lambda j: cleaned.append(j) or {"success": True})
    monkeypatch.setattr(audio, "analyze_audio", lambda p: {
        "success": True, "bpm": 92.3, "musical_key": "C# minor", "energy": 3, "duration_sec": 301})
    monkeypatch.setattr(lyrics, "lyrics_search", lambda *a: {"success": True, "found": False})
    r = client.post("/api/song/add", headers=HEADERS, json={
        "song_key": "yt:outsideAAA1", "fields": {"title": "Chalte Chalte", "film": "Chalte Chalte"}, "rating": 9})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}", headers=HEADERS).json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    row = catalog.catalog_get(job["song_id"])["row"]
    assert row["year"] == "1976" and row["music_director"] == "Bappi Lahiri"
    assert row["karaoke_url"].endswith("karaokeCC01") and row["bpm"] == "92.3" and row["energy"] == "3"
    assert row["my_rating"] == "9" and row["status"] == "ready" and row["needs_review"] == "true"
    assert row["genre"] == "" and cleaned == ["20261105-101010-abcdef12"]
    assert any(r_["problem"] == "unsure-match" for r_ in catalog.load_review())
    assert feedback.is_liked(songs["ref"], job["song_id"], feedback.load_relations())
    # The outside key now resolves to the new song.
    r = client.post("/api/rating", headers=HEADERS, json={"song_key": "yt:outsideAAA1", "rating": 5}).json()
    assert r["needs_add"] is False and r["status"] == "practicing"


# --- Suggest Similar --------------------------------------------------------------------------------

def wait_mix(client, sid, page=0):
    r = client.get(f"/api/similar/mix?session={sid}&page={page}", headers=HEADERS).json()
    assert r["mix"] != "loading"
    return r


def test_similar_catalog_first_then_mix_with_karaoke_tracks(client, songs, mix):
    r = client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS).json()
    assert r["success"] and r["seed"]["key"] == songs["ref"] and r["seed"]["in_catalog"]
    assert all(it["group"] == "catalog" for it in r["catalog"]["items"])
    m = wait_mix(client, r["session"])
    assert mix["mix"][0] == {"song": "Pal Pal Dil Ke Paas", "seed": "original", "film": "Blackmail"}
    assert m["mix"] == "done" and m["catalog"]["final"]
    assert m["catalog"]["items"][0]["title"] == "Yeh Shaam Mastani"  # mix #1 now counts
    out = {it["title"]: it for it in m["outside"]["items"]}
    assert out["Chalte Chalte"]["karaoke"] == "found"
    assert out["Chalte Chalte"]["karaoke_url"].endswith("karaokeCC01")
    assert out["Chalte Chalte"]["video_url"].endswith("outsideAAA1")      # original kept
    assert out["Kya Hua Tera Wada"]["karaoke"] == "none" and out["Kya Hua Tera Wada"]["karaoke_url"] == ""
    assert m["outside"]["end"] and m["catalog"]["end"]
    # Recent seeds remember it.
    assert client.get("/api/home", headers=HEADERS).json()["recent"][0]["seed"] == "Pal Pal Dil Ke Paas"
    # Re-opening uses the cached mix: no new fetch, outside comes with the page.
    similar._sessions.clear()
    r2 = client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS).json()
    assert len(mix["mix"]) == 1 and r2["mix"] == "done"


def test_similar_likes_show_and_paging_ends(client, songs, mix):
    feedback.set_like(songs["ref"], "yt:outsideAAA1", True)
    r = client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS).json()
    m = wait_mix(client, r["session"])
    liked = {it["title"]: it["liked"] for it in m["outside"]["items"]}
    assert liked == {"Chalte Chalte": True, "Kya Hua Tera Wada": False}
    assert "♥ liked" in next(it for it in m["outside"]["items"] if it["title"] == "Chalte Chalte")["reasons"]
    p1 = wait_mix(client, r["session"], page=1)
    assert p1["catalog"]["items"] == [] and p1["outside"]["items"] == [] and p1["outside"]["end"]


def test_similar_rejects_non_youtube_urls(client, songs):
    r = client.get("/api/similar?url=https://evil.example/watch?v=x", headers=HEADERS)
    assert r.status_code == 400
    assert client.get("/api/similar", headers=HEADERS).status_code == 400


def test_mix_failure_keeps_the_catalog(client, songs, monkeypatch):
    monkeypatch.setattr(youtube, "youtube_mix", lambda **k: {"success": False, "error_code": "x", "error": "down"})
    r = client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS).json()
    m = wait_mix(client, r["session"])
    assert m["mix"] == "error" and m["outside"]["items"] == []
    assert any(it["title"] == "Yeh Shaam Mastani" for it in m["catalog"]["items"])


# --- karaoke resolution (cache.karaoke_many) -------------------------------------------------------

def test_karaoke_resolution_index_search_none_and_cache(wiki, monkeypatch):
    calls = []

    def fake_find(title, film="", singers="", max_results=5):
        calls.append(title)
        return {"success": True, "candidates": {
            "Index Song": [{"url": "https://www.youtube.com/watch?v=index000001", "title": "Index Song",
                            "channel": "Anil Chauhan", "score": 92, "source": "index"}],
            "Search Song": [{"url": "https://www.youtube.com/watch?v=search00001", "title": "Search Song Cover",
                             "channel": "Y", "score": 99, "source": "search"},
                            {"url": "https://www.youtube.com/watch?v=search00002", "title": "Search Song Karaoke",
                             "channel": "Z", "score": 90, "source": "search"}],
            "Weak Song": [{"url": "https://www.youtube.com/watch?v=weak0000001", "title": "Weak Song karaoke",
                           "channel": "Z", "score": 60, "source": "search"}],
        }.get(title, [])}
    monkeypatch.setattr(youtube, "karaoke_find", fake_find)
    got = cache.karaoke_many([("Index Song", ""), ("Search Song", ""), ("Weak Song", ""), ("Nothing", "")])
    assert got[0]["url"].endswith("index000001") and got[0]["source"] == "index"
    assert got[1]["url"].endswith("search00002")          # "karaoke" in the title, not the cover
    assert got[2] is None and got[3] is None
    assert sorted(calls) == ["Index Song", "Nothing", "Search Song", "Weak Song"]
    again = cache.karaoke_many([("index song", ""), ("Nothing", "")])  # normalised title, cached "none" too
    assert again[0]["url"].endswith("index000001") and again[1] is None
    assert len(calls) == 4


def test_karaoke_errors_are_not_cached(wiki, monkeypatch):
    calls = []

    def boom(title, *a, **k):
        calls.append(title)
        raise RuntimeError("net down")
    monkeypatch.setattr(youtube, "karaoke_find", boom)
    assert cache.karaoke_many([("X", "")]) == [None]
    assert cache.karaoke_many([("X", "")]) == [None]
    assert calls == ["X", "X"]


def test_catalog_items_keep_their_own_karaoke_link(client, songs, mix):
    r = client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS).json()
    m = wait_mix(client, r["session"])
    chain = next(it for it in m["catalog"]["items"] if it["title"] == "O Mere Dil Ke Chain")
    assert chain["karaoke_url"].endswith("aaaaaaaaaa1")
    assert "O Mere Dil Ke Chain" not in mix["finds"]


# --- close summary --------------------------------------------------------------------------------

def test_close_sends_one_line(client, songs, telegram, mix):
    client.get("/api/similar?seed=Pal%20Pal%20Dil%20Ke%20Paas", headers=HEADERS)
    for song in (songs["chain"], songs["shaam"]):
        client.post("/api/like", headers=HEADERS, json={"seed_key": songs["ref"], "song_key": song, "liked": True})
    client.post("/api/rating", headers=HEADERS, json={"song_key": songs["chain"], "rating": 9})
    before = len(telegram.messages)
    r = client.post("/api/session/close", headers=HEADERS).json()
    assert r["sent"] and r["text"] == "Saved 2 likes, 1 rating (similar to Pal Pal Dil Ke Paas)"
    assert len(telegram.messages) == before + 1 and telegram.messages[-1]["chat_id"] == "4242"
    # Done + the best-effort close on hide: the second call has nothing new.
    assert client.post("/api/session/close", headers=HEADERS).json()["sent"] is False
    assert len(telegram.messages) == before + 1


def test_close_with_nothing_saved_sends_nothing(client, telegram):
    assert client.post("/api/session/close", headers=HEADERS).json()["sent"] is False
    assert telegram.messages == []
    r = client.post("/api/session/close", headers={"X-Telegram-Init-Data": init_data()})
    assert r.status_code == 400  # no X-App-Session
