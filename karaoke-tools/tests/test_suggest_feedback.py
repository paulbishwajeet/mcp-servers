"""suggest / feedback_tap / ratings / relations, with a fixture catalog, a fake
YouTube Mix and a mocked Telegram Bot API. No network: tg.requests.post is
replaced by the `telegram` fixture, everything else by conftest's no_network."""

import csv
import json
from datetime import date, timedelta

import pytest

import catalog
import feedback
import suggest
import tg
import vocab
import youtube
from conftest import FIXTURES

TODAY = date.today().isoformat()


@pytest.fixture(autouse=True)
def keyboard_mode(monkeypatch):
    """These tests cover FEEDBACK_MODE=keyboard (the older button UI); the poll
    UI (the default) is covered in test_polls.py."""
    monkeypatch.setenv("FEEDBACK_MODE", "keyboard")


def add(**row):
    r = catalog.catalog_upsert(row)
    assert r["success"], r
    return r["song_id"]


@pytest.fixture
def songs(wiki):
    ids = {}
    ids["ref"] = add(title="Pal Pal Dil Ke Paas", film="Blackmail", year=1973, singers="Kishore Kumar",
                     era="1970s", tempo_class="slow", energy=2, mood="romantic;longing", status="ready",
                     karaoke_url="https://www.youtube.com/watch?v=pw6r-izZArA")
    ids["chain"] = add(title="O Mere Dil Ke Chain", film="Mere Jeevan Saathi", year=1972,
                       singers="Kishore Kumar", tempo_class="slow", energy=2, mood="romantic",
                       status="ready", crowd_score=2.6, my_rating=9, rated_on=TODAY,
                       karaoke_url="https://www.youtube.com/watch?v=aaaaaaaaaa1")
    ids["shaam"] = add(title="Yeh Shaam Mastani", film="Kati Patang", year=1970, singers="Kishore Kumar",
                       tempo_class="medium", energy=3, mood="romantic", status="practicing")
    ids["khwab"] = add(title="Dekha Ek Khwab", film="Silsila", year=1981,
                       singers="Kishore Kumar;Lata Mangeshkar", tempo_class="slow", energy=2,
                       mood="romantic", status="wishlist", my_rating=2, rated_on=TODAY)
    ids["kk4"] = add(title="Mere Sapnon Ki Rani", film="Aradhana", year=1969, singers="Kishore Kumar",
                     tempo_class="fast", energy=4, mood="playful;happy", status="ready")
    ids["peppy"] = add(title="Aaj Ki Raat", film="Don", year=1978, singers="Asha Bhosle",
                       tempo_class="fast", energy=5, mood="energetic;playful", genre="peppy",
                       status="ready")
    return ids


def mix_entry(rank, title, in_catalog="", sources=("youtube",), kind="karaoke", vid=None):
    vid = vid or f"vid{rank:08d}"
    return {"rank": rank, "song_title": title, "film": "", "kind": kind, "sources": list(sources),
            "in_catalog": in_catalog, "status": "", "match_score": 100.0 if in_catalog else 40.0,
            "videos": [{"url": f"https://www.youtube.com/watch?v={vid}", "title": title + " Karaoke",
                        "channel": "X", "duration_sec": 300, "kind": kind}]}


@pytest.fixture
def mix(songs, monkeypatch):
    entries = [
        mix_entry(1, "Yeh Shaam Mastani", songs["shaam"], ("youtube", "youtube-music")),
        mix_entry(2, "Kishore Kumar Hits", kind="jukebox"),
        mix_entry(3, "Chalte Chalte", vid="outsideAAA1", sources=("youtube", "youtube-music")),
        mix_entry(6, "O Mere Dil Ke Chain", songs["chain"]),
        mix_entry(8, "Dekha Ek Khwab", songs["khwab"]),
        mix_entry(9, "Mere Sapnon Ki Rani", songs["kk4"]),
        mix_entry(20, "Kya Hua Tera Wada", vid="outsideBBB2", kind="original"),
    ]
    calls = []

    def fake_mix(song="", url="", seed="karaoke", source="youtube", limit=25, film=""):
        calls.append({"song": song, "url": url, "seed": seed, "source": source, "limit": limit,
                      "film": film})
        return {"success": True, "seed": {"video_id": "pw6r-izZArA", "title": "Pal Pal Karaoke",
                                          "song_title": "Pal Pal Dil Ke Paas", "channel": "X", "url": url},
                "entries": entries}

    monkeypatch.setattr(youtube, "youtube_mix", fake_mix)
    monkeypatch.setattr(youtube, "karaoke_find", lambda *a, **k: {"success": True, "candidates": []})
    return calls


# --- migration ---------------------------------------------------------------------------

def test_migration_adds_columns_once_and_keeps_values(wiki):
    old = vocab.SONG_COLUMNS_V1
    rows = [{c: "" for c in old} | {"song_id": "a-1970", "title": 'A "quoted", title', "year": "1970",
                                    "crowd_score": "2.5", "notes": "line1\nline2", "mood": "romantic;calm"},
            {c: "" for c in old} | {"song_id": "b-1980", "title": "B", "bpm": "120.0"}]
    (wiki / "catalog/songs.csv").write_text(catalog.render_csv(old, rows, vocab.NUMERIC_COLUMNS))
    with pytest.raises(Exception, match="migrate.py"):
        catalog.load_songs()
    r = catalog.migrate_songs_csv()
    assert r["migrated"] and r["rows"] == 2
    text = (wiki / "catalog/songs.csv").read_text()
    assert text.splitlines()[0] == ",".join(vocab.SONG_COLUMNS)
    new = list(csv.DictReader(open(wiki / "catalog/songs.csv", newline="")))
    for before, after in zip(rows, new):
        assert all(after[c] == before[c] for c in old)
        assert after["my_rating"] == "" and after["rated_on"] == ""
    assert catalog.migrate_songs_csv()["migrated"] is False
    assert (wiki / "catalog/songs.csv").read_text() == text


def test_rating_field_validation(wiki):
    assert not catalog.catalog_upsert({"title": "X", "my_rating": 11})["success"]
    assert not catalog.catalog_upsert({"title": "X", "my_rating": 0})["success"]
    assert catalog.catalog_upsert({"title": "X", "my_rating": 10, "rated_on": TODAY})["success"]


# --- scoring -------------------------------------------------------------------------------

def test_like_order_reasons_and_exclusions(mix, telegram):
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    assert r["success"], r
    # Seeded from the original song (title + film to find it), not its karaoke.
    assert mix[0] == {"song": "Pal Pal Dil Ke Paas", "url": "", "seed": "original",
                      "source": "both", "limit": 40, "film": "Blackmail"}
    cat = [i for i in r["items"] if i["group"] == "catalog"]
    out = [i for i in r["items"] if i["group"] == "outside"]
    titles = [i["title"] for i in cat]
    # O Mere Dil Ke Chain: mix #6 (2) + ready (3) + crowd 2.6 (2) + same era/tempo/mood (3) + ⭐9 (3) = 13
    # Yeh Shaam Mastani:   mix #1 (3) + both (2) + practicing (2) + era/tempo/mood (3) = 10
    assert titles[0] == "O Mere Dil Ke Chain" and cat[0]["score"] == 13
    assert titles[1] == "Yeh Shaam Mastani" and cat[1]["score"] == 10
    assert cat[0]["reasons"] == "mix #6 · ready · crowd 2.6 · ⭐9"
    assert cat[1]["reasons"] == "mix #1 · both · practicing"
    assert "Pal Pal Dil Ke Paas" not in titles  # the reference itself
    assert all(i["title"] != "Kishore Kumar Hits" for i in r["items"])  # jukebox dropped
    assert [i["title"] for i in out] == ["Chalte Chalte", "Kya Hua Tera Wada"]
    assert out[0]["song_key"] == "yt:outsideAAA1" and out[0]["reasons"] == "mix #3 · both · outside"
    assert out[0]["karaoke_url"] == "https://www.youtube.com/watch?v=outsideAAA1"
    assert r["reference_key"] == "pal-pal-dil-ke-paas-1973"


def test_max_per_singer_and_exclude(mix, songs, monkeypatch, telegram):
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    kishore = [i for i in r["items"] if i["group"] == "catalog" and "Kishore Kumar" in i["singers"]]
    assert len(kishore) == 3  # max_per_singer
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas",
                        exclude=[songs["chain"], "Yeh Shaam Mastani"], send_buttons=False)
    titles = [i["title"] for i in r["items"]]
    assert "O Mere Dil Ke Chain" not in titles and "Yeh Shaam Mastani" not in titles


def test_plan_drops_low_rated(mix, songs, wiki, telegram):
    (wiki / "ranking.yaml").write_text("max_per_singer: 0\n")  # 0 = no singer cap
    r = suggest.suggest(mode="plan", references=["Pal Pal Dil Ke Paas"], send_buttons=False)
    assert "Dekha Ek Khwab" not in [i["title"] for i in r["items"]]  # ⭐2 <= 2
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    assert "Dekha Ek Khwab" in [i["title"] for i in r["items"]]


@pytest.mark.parametrize("rating,points", [(10, 3), (9, 3), (8, 2), (7, 2), (6, 0), (5, 0),
                                            (4, -1), (3, -1), (2, -3), (1, -3), ("", 0), (None, 0)])
def test_rating_bands(rating, points):
    assert suggest.rating_points(rating, suggest.DEFAULT_WEIGHTS["rating"]) == points


def test_like_links_down_penalty_decay_and_cap():
    W = suggest.DEFAULT_WEIGHTS
    today = date(2026, 11, 4)

    def row(days_ago, verdict="like", ref="ref", song="song"):
        return {"logged_at": (today - timedelta(days=days_ago)).isoformat() + " 10:00:00",
                "reference_key": ref, "song_key": song, "verdict": verdict}
    assert feedback.link_score("ref", "song", [row(0)], W, today=today) == (3, True, 0)
    assert feedback.link_score("ref", "song", [row(0, "up")], W, today=today)[1] is True  # old 👍
    # Latest row wins: liked, then un-liked.
    assert feedback.link_score("ref", "song", [row(5), row(1, "unlike")], W, today=today)[1] is False
    assert feedback.link_score("ref", "song", [row(5, "unlike"), row(1)], W, today=today)[1] is True
    # Both directions for similarity, directed for "after".
    back = [row(0, ref="song", song="ref")]
    assert feedback.link_score("ref", "song", back, W, today=today)[1] is True
    assert feedback.link_score("ref", "song", back, W, both_ways=False, today=today)[1] is False
    # 👎: -1 each, halved per half-life, capped at -cap.
    assert feedback.link_score("ref", "song", [row(0, "down")], W, today=today)[0] == -1.0
    assert feedback.link_score("ref", "song", [row(180, "down")], W, today=today)[0] == -0.5
    assert feedback.link_score("ref", "song", [row(0, "down")] * 9, W, today=today)[0] == -6.0
    assert feedback.link_score("other", "song", [row(0)], W, today=today) == (0, False, 0)


def test_relations_change_the_order(mix, songs, wiki, telegram):
    for _ in range(3):  # three old-style rows: Yeh Shaam Mastani fits after Pal Pal
        feedback._append_relation({"logged_at": TODAY + " 10:00:00", "reference_key": songs["ref"],
                                   "reference_title": "Pal Pal Dil Ke Paas", "song_key": songs["shaam"],
                                   "song_title": "Yeh Shaam Mastani", "verdict": "up",
                                   "request_type": "after", "request_text": "Pal Pal Dil Ke Paas"})
    r = suggest.suggest(mode="after", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    first = r["items"][0]
    assert first["title"] == "Yeh Shaam Mastani" and first["score"] == 13  # 10 + like_link 3
    assert first["reasons"].endswith("♥ liked after this")


def test_next_uses_query_and_no_youtube(songs, monkeypatch, telegram):
    monkeypatch.setattr(youtube, "youtube_mix", lambda *a, **k: pytest.fail("YouTube called"))
    r = suggest.suggest(mode="next", query="next peppy old", send_buttons=False)
    assert r["reference_key"] == "query:old peppy"
    assert [i["title"] for i in r["items"]] == ["Aaj Ki Raat"]
    assert all(i["group"] == "catalog" for i in r["items"])


def test_youtube_failure_still_returns_catalog(songs, monkeypatch, telegram):
    monkeypatch.setattr(youtube, "youtube_mix", lambda *a, **k: {"success": False,
                                                                  "error_code": "mix_unavailable"})
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    assert r["success"] and "YouTube Mix unavailable" in r["warnings"]
    assert r["items"] and all(i["group"] == "catalog" for i in r["items"])


def test_ranking_yaml_overrides(mix, wiki, telegram):
    (wiki / "ranking.yaml").write_text("status: {ready: 0}\nrating: {'9-10': 0}\n")
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    chain = next(i for i in r["items"] if i["title"] == "O Mere Dil Ke Chain")
    assert chain["score"] == 13 - 3 - 3
    (wiki / "ranking.yaml").write_text("this: [is: not yaml")
    w, warnings = suggest.load_weights()
    assert w == suggest.DEFAULT_WEIGHTS and warnings


# --- numbering and state ----------------------------------------------------------------------

def test_numbering_continuous_across_groups_and_pages(mix, wiki, telegram):
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", limit_catalog=2,
                        limit_outside=1, send_buttons=False)
    assert [(i["n"], i["group"]) for i in r["items"]] == [(1, "catalog"), (2, "catalog"), (3, "outside")]
    more = suggest.suggest_more(send_buttons=False)
    # 3 catalog songs survive the singer cap, 2 outside: page 2 is the rest.
    assert [(i["n"], i["group"]) for i in more["items"]] == [(4, "catalog"), (5, "outside")]
    assert suggest.suggest_more(send_buttons=False)["note"] == "That's the whole list."
    st = json.loads((wiki.parent / "state" / "last_suggestions.json").read_text())
    req = st["requests"][-1]
    assert req["reference_key"] == "pal-pal-dil-ke-paas-1973" and req["mode"] == "like"
    assert {k: v["title"] for k, v in req["numbers"].items()}["3"] == "Chalte Chalte"
    assert sorted(int(k) for k in req["numbers"]) == [1, 2, 3, 4, 5]
    for _ in range(6):
        suggest.suggest(mode="next", query="peppy", send_buttons=False)
    assert len(json.loads((wiki.parent / "state" / "last_suggestions.json").read_text())["requests"]) == 5


# --- keyboards and Telegram ---------------------------------------------------------------------

def test_main_keyboard_shape(mix, telegram):
    suggest.suggest(mode="after", reference="Pal Pal Dil Ke Paas")
    assert [m for m, _ in telegram.calls] == ["sendMessage"]
    msg = telegram.messages[0]
    assert msg["chat_id"] == "4242" and msg["text"].startswith("Rate these:")
    kb = msg["reply_markup"]
    assert kb["resize_keyboard"] is True and kb["input_field_placeholder"] == "tap or type"
    rows = [[b["text"] for b in row] for row in kb["keyboard"]]
    assert rows[0] == ["1 👍", "1 👎", "1 ⭐"] and rows[-1] == ["more", "done"]
    assert len(rows) == 1 + len([r for r in rows[:-1]])


def test_rating_pad_shape():
    assert feedback.rating_pad(3) == [["3 ⭐1", "3 ⭐2", "3 ⭐3", "3 ⭐4", "3 ⭐5"],
                                      ["3 ⭐6", "3 ⭐7", "3 ⭐8", "3 ⭐9", "3 ⭐10"], ["back"]]


def test_large_pages_split_under_the_button_limit():
    rows = feedback.main_rows(list(range(1, 41)))  # 40 items = 120 buttons
    parts = tg.split_keyboard(rows, feedback.FOOTER)
    assert len(parts) == 2
    assert all(sum(len(r) for r in p) <= tg.MAX_BUTTONS for p in parts)
    assert all(p[-1] == ["more", "done"] for p in parts)
    assert len(tg.split_keyboard(feedback.main_rows(list(range(1, 21))), feedback.FOOTER)) == 1


def test_other_chats_and_inline_mode_refused(wiki, telegram, monkeypatch):
    assert tg.send("hi", chat_id="999")["error_code"] == "chat_not_allowed"
    monkeypatch.setenv("BUTTON_MODE", "inline")
    assert tg.send("hi", keyboard=[["a"]])["error_code"] == "button_mode_unsupported"
    assert tg._call("getUpdates", {})["error_code"] == "not_allowed"
    assert tg._call("setWebhook", {})["error_code"] == "not_allowed"
    assert telegram.calls == []


def test_never_receives_updates(mix, telegram):
    suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas")
    feedback.feedback_tap("1 ⭐")
    feedback.feedback_tap("done")
    assert {m for m, _ in telegram.calls} == {"sendMessage"}


# --- feedback_tap ------------------------------------------------------------------------------

@pytest.fixture
def listed(mix, telegram):
    r = suggest.suggest(mode="after", reference="Pal Pal Dil Ke Paas")
    return {i["n"]: i for i in r["items"]}


def rel_rows(wiki):
    p = wiki / "feedback/relations.csv"
    return list(csv.DictReader(open(p, newline=""))) if p.exists() else []


@pytest.mark.parametrize("text", ["1 👍", "1👍", "#1 👍", "1 👍🏻", "1 👍🏽"])
def test_thumbs_up_variants(listed, wiki, text):
    r = feedback.feedback_tap(text)
    assert r["handled"] and r["reply"] == f"👍 noted: {listed[1]['title']} after Pal Pal Dil Ke Paas"
    rows = rel_rows(wiki)
    assert len(rows) == 1 and rows[0]["verdict"] == "up" and rows[0]["request_type"] == "after"
    assert rows[0]["reference_key"] == "pal-pal-dil-ke-paas-1973"
    assert rows[0]["song_key"] == listed[1]["song_key"]


def test_double_tap_replaces(listed, wiki):
    feedback.feedback_tap("2 👍")
    feedback.feedback_tap("2 👎")
    rows = rel_rows(wiki)
    assert len(rows) == 1 and rows[0]["verdict"] == "down"
    feedback.feedback_tap("1 👍")
    assert len(rel_rows(wiki)) == 2


@pytest.mark.parametrize("text", ["2 ⭐", "2⭐️", "2 ★"])
def test_star_opens_rating_pad(listed, telegram, text):
    feedback.feedback_tap(text)
    msg = telegram.messages[-1]
    assert msg["text"] == f"Rate {listed[2]['title']}:"
    assert [b["text"] for b in msg["reply_markup"]["keyboard"][2]] == ["back"]


@pytest.mark.parametrize("text", ["1 ⭐8", "1 ⭐️8", "1⭐ 8", "rate 1 8"])
def test_rating_a_catalog_song(listed, telegram, text):
    r = feedback.feedback_tap(text)
    assert r["handled"] and r["reply"] == f"⭐8 saved for {listed[1]['title']}"
    row = catalog.catalog_get(listed[1]["song_key"])["row"]
    assert row["my_rating"] == "8" and row["rated_on"] == TODAY
    assert telegram.messages[-1]["text"].startswith("Rate these:")  # main keyboard again


def test_rating_an_outside_song_needs_add(listed, wiki):
    n = next(k for k, v in listed.items() if v["group"] == "outside")
    before = (wiki / "catalog/songs.csv").read_text()
    r = feedback.feedback_tap(f"{n} ⭐7")
    assert r["needs_add"] and r["add"][0]["title"] == listed[n]["title"]
    assert (wiki / "catalog/songs.csv").read_text() == before  # nothing written
    st = feedback.load_state()
    assert st["pending_ratings"][listed[n]["song_key"]]["rating"] == 7


def test_back_done_more(listed, telegram):
    feedback.feedback_tap("back")
    assert telegram.messages[-1]["text"].startswith("Rate these:")
    feedback.feedback_tap("done")
    assert telegram.messages[-1]["reply_markup"] == {"remove_keyboard": True}
    r = feedback.feedback_tap("more")  # the after list fit on one page
    assert r["handled"] and r["reply"] == "That's the whole list."


def test_unknown_number_and_unrelated_text(listed):
    assert feedback.feedback_tap("42 👍")["reply"] == "#42 isn't in the last list"
    assert feedback.feedback_tap("songs like Kesariya")["handled"] is False


def test_typed_batch(listed, wiki):
    r = feedback.feedback_tap("fb 1👍 2👎 1⭐8")
    assert r["handled"]
    assert r["reply"].count("noted") == 2 and "⭐8 saved" in r["reply"]
    assert [x["verdict"] for x in rel_rows(wiki)] == ["up", "down"]


def test_rate_by_title(songs, telegram):
    r = feedback.feedback_tap("rate Yeh Shaam Mastani 9")
    assert r["handled"] and r["reply"] == "⭐9 saved for Yeh Shaam Mastani"
    assert feedback.set_rating("Nonexistent Song", 5)["error_code"] == "not_found"
    assert feedback.set_rating("Yeh Shaam Mastani", 11)["error_code"] == "invalid_input"


def test_next_request_reference_is_the_query(songs, telegram, wiki):
    suggest.suggest(mode="next", query="peppy old")
    r = feedback.feedback_tap("1 👍")
    assert r["reply"] == "👍 noted: Aaj Ki Raat for 'peppy old'"
    assert rel_rows(wiki)[0]["reference_key"] == "query:old peppy"


# --- rekey + pending rating on add -----------------------------------------------------------------

def test_adding_an_outside_song_moves_relations_and_rating(listed, wiki):
    n = next(k for k, v in listed.items() if v["group"] == "outside")
    key = listed[n]["song_key"]  # yt:outsideAAA1
    feedback.feedback_tap(f"{n} 👍")
    feedback.feedback_tap(f"{n} ⭐7")
    r = catalog.catalog_upsert({"title": "Chalte Chalte", "film": "Chalte Chalte", "year": 1976,
                                "karaoke_url": "https://www.youtube.com/watch?v=outsideAAA1"})
    assert r["success"] and r["row"]["my_rating"] == "7"
    rows = rel_rows(wiki)
    assert rows[0]["song_key"] == r["song_id"] and all(x["song_key"] != key for x in rows)
    assert key not in feedback.load_state()["pending_ratings"]


def test_relations_rekey_and_for(listed, wiki, songs):
    feedback.feedback_tap("1 👍")
    feedback.feedback_tap("2 👎")
    out = feedback.relations_for("Pal Pal Dil Ke Paas")
    assert {x["song_key"] for x in out["goes_after_it"]} == {listed[1]["song_key"], listed[2]["song_key"]}
    back = feedback.relations_for(listed[1]["song_key"])
    assert back["it_goes_after"][0]["song_key"] == songs["ref"]
    r = feedback.relations_rekey(songs["ref"], "new-key")
    assert r["rewritten"] == 2 and {x["reference_key"] for x in rel_rows(wiki)} == {"new-key"}


def test_reference_tie_prefers_the_plain_better_row(songs, mix, telegram):
    add(title="Pal Pal Dil Ke Paas", year=1977, singers="Kishore Kumar", status="wishlist",
        karaoke_url="https://www.youtube.com/watch?v=6UGHmWVy1ss")  # a duplicate added later
    add(title="Pal Pal Dil Ke Paas (Unplugged)", film="Blackmail", year=1973, status="ready")
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    assert r["reference_key"] == songs["ref"]  # ready, has a film, not a version row


def test_retired_mix_matches_are_dropped(mix, songs, telegram):
    catalog.catalog_upsert({"song_id": songs["shaam"], "status": "retired"})
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", send_buttons=False)
    assert "Yeh Shaam Mastani" not in [i["title"] for i in r["items"]]
