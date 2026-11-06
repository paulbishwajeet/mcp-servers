"""FEEDBACK_MODE=poll (the default): 👍/👎 multi-select polls, collected with
stopPoll. Telegram is the FakeTelegram from conftest; stopPoll answers from
fixture vote counts. Never getUpdates or setWebhook."""

import csv
import json
from datetime import datetime, timedelta

import pytest

import catalog
import feedback
import server
import suggest
import tg
from test_suggest_feedback import add, mix, songs  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def poll_mode(monkeypatch):
    monkeypatch.setenv("FEEDBACK_MODE", "poll")


def rel_rows(wiki):
    p = wiki / "feedback/relations.csv"
    return list(csv.DictReader(open(p, newline=""))) if p.exists() else []


def run_like(**kw):
    r = suggest.suggest(mode="like", reference="Pal Pal Dil Ke Paas", **kw)
    assert r["success"], r
    return r


def polls_of(telegram):
    return [p for _, p in telegram.sent_polls]


# --- sending ---------------------------------------------------------------------------

def test_poll_pair_payload(mix, telegram):
    r = run_like()
    polls = polls_of(telegram)
    assert len(polls) == 2 and {m for m, _ in telegram.calls} == {"sendPoll"}
    up, down = polls
    assert up["question"] == "👍 Which fit after Pal Pal Dil Ke Paas?"
    assert down["question"] == "👎 Which don't fit after Pal Pal Dil Ke Paas?"
    for p in polls:
        assert p["chat_id"] == "4242"
        assert p["allows_multiple_answers"] is True and p["is_anonymous"] is True
    # Never on a poll: Telegram can't stop a poll sent with ReplyKeyboardRemove.
    assert all("reply_markup" not in p for p in polls)
    texts = [o["text"] for o in up["options"]]
    assert texts[0] == f"#1 {r['items'][0]['title']} (Kishore Kumar)"
    assert texts == [o["text"] for o in down["options"]]
    assert len(texts) == len(r["items"])


def test_option_and_question_truncation(wiki, telegram):
    long = "Very " * 40 + "Long Title"
    opt = feedback._poll_option({"n": 7, "title": long, "singers": "Somebody"})
    assert len(opt) == tg.MAX_OPTION_LEN and opt.endswith("…") and opt.startswith("#7 Very")
    tg.send_poll("q" * 400, ["a", "b"])
    assert len(polls_of(telegram)[0]["question"]) == tg.MAX_QUESTION_LEN


def items(groups):
    out, n = [], 1
    for g, count in groups:
        for _ in range(count):
            out.append({"n": n, "group": g, "title": f"Song {n}", "singers": ""})
            n += 1
    return out


def test_split_by_group_and_page():
    one, typed = feedback.poll_groups(items([("catalog", 10), ("outside", 2)]))
    assert [(label, len(p)) for label, p in one] == [("", 12)] and typed == []
    split, typed = feedback.poll_groups(items([("catalog", 10), ("outside", 10)]))
    assert [(label, len(p)) for label, p in split] == [("catalog", 10), ("outside", 10)]
    big, typed = feedback.poll_groups(items([("catalog", 25), ("outside", 1)]))
    assert [len(p) for _, p in big] == [9, 8, 8]  # balanced, each <= 12
    assert big[0][0] == "catalog #1–#9" and typed == [26]  # a lone outside song: no poll
    tiny, typed = feedback.poll_groups(items([("catalog", 1)]))
    assert tiny == [] and typed == [1]


def test_lone_song_gets_typed_hint(wiki, telegram):
    req = {"request_id": "r1", "mode": "after", "reference_title": "X", "polls": [],
           "numbers": {}, "taps": {}}
    feedback.push_request(req)
    r = feedback.send_polls(req, items([("catalog", 12), ("outside", 1)]))
    assert r["typed_only"] == [13] and r["polls_sent"] == 2  # one pair: the 12 catalog songs
    assert [len(p["options"]) for p in polls_of(telegram)] == [12, 12]
    assert polls_of(telegram)[0]["question"] == "👍 Which fit after X? (catalog)"
    assert telegram.messages[-1]["text"] == "Not in a poll: #13. Rate them by typing e.g. fb 13👍"


def test_questions_for_next_and_plan(songs, telegram, monkeypatch):
    monkeypatch.setattr(suggest.youtube, "youtube_mix", lambda *a, **k: pytest.fail("YouTube"))
    for i in range(3):
        add(title=f"Peppy Old {i}", year=1975, genre="peppy", mood="playful", status="ready",
            singers=f"Singer {i}")
    suggest.suggest(mode="next", query="peppy old")
    assert polls_of(telegram)[0]["question"] == '👍 Which fit "peppy old"?'
    assert feedback._questions({"mode": "plan", "reference_title": "a; b"})[0] == "👍 Which fit this plan?"


def test_only_the_configured_chat(wiki, telegram, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert tg.send_poll("q", ["a", "b"])["error_code"] == "telegram_not_configured"
    assert tg.stop_poll(5)["error_code"] == "telegram_not_configured"
    assert telegram.calls == []


def test_state_saved_per_poll(mix, telegram, wiki):
    r = run_like()
    req = feedback.latest_request()
    assert [p["type"] for p in req["polls"]] == ["up", "down"]
    assert all(p["status"] == "open" and p["message_id"] for p in req["polls"])
    assert req["polls"][0]["options"] == {str(i): it["n"] for i, it in enumerate(r["items"])}


# --- collecting ---------------------------------------------------------------------------

def test_collect_up_down_conflict(mix, telegram, wiki):
    r = run_like()
    up_id, down_id = [p["message_id"] for p in feedback.latest_request()["polls"]]
    telegram.votes = {up_id: [0, 1, 2], down_id: [2, 3]}  # #3 ticked in both
    c = feedback.feedback_collect()
    assert [(x["n"], x["verdict"]) for x in c["collected"]] == [(1, "up"), (2, "up"), (4, "down")]
    assert [x["n"] for x in c["conflicts"]] == [3]
    assert c["reply"].startswith("Saved: 👍 2 songs, 👎 1 song (after Pal Pal Dil Ke Paas)")
    assert "ticked in both" in c["reply"]
    rows = rel_rows(wiki)
    assert [(x["song_key"], x["verdict"]) for x in rows] == \
        [(r["items"][0]["song_key"], "up"), (r["items"][1]["song_key"], "up"),
         (r["items"][3]["song_key"], "down")]
    assert all(x["reference_key"] == "pal-pal-dil-ke-paas-1973" and x["request_type"] == "like"
               and x["reference_title"] == "Pal Pal Dil Ke Paas" for x in rows)
    assert {m for m, _ in telegram.calls} == {"sendPoll", "stopPoll"}


def test_nothing_ticked(mix, telegram, wiki):
    run_like()
    c = feedback.feedback_collect()
    assert c["collected"] == [] and c["reply"] == "No feedback (after Pal Pal Dil Ke Paas)"
    assert rel_rows(wiki) == []


def test_collect_is_idempotent(mix, telegram, wiki):
    run_like()
    up_id = feedback.latest_request()["polls"][0]["message_id"]
    telegram.votes = {up_id: [0]}
    feedback.feedback_collect()
    stops = sum(1 for m, _ in telegram.calls if m == "stopPoll")
    again = feedback.feedback_collect()
    assert again["collected"] == [] and again["reply"] == "No open polls."
    assert sum(1 for m, _ in telegram.calls if m == "stopPoll") == stops
    assert len(rel_rows(wiki)) == 1


def test_lost_poll_is_reported_not_raised(mix, telegram, wiki):
    run_like()
    up_id, down_id = [p["message_id"] for p in feedback.latest_request()["polls"]]
    telegram.stop_errors = {up_id: "Bad Request: message to stop not found"}
    telegram.votes = {down_id: [1]}
    c = feedback.feedback_collect()
    assert c["success"] and len(c["lost"]) == 1 and c["lost"][0]["type"] == "up"
    assert [(x["n"], x["verdict"]) for x in c["collected"]] == [(2, "down")]
    assert "couldn't be read" in c["reply"]
    assert [p["status"] for p in feedback.latest_request()["polls"]] == ["lost", "closed"]
    assert feedback.feedback_collect()["lost"] == []  # lost polls aren't retried


def test_typed_feedback_overrides_poll_ticks(mix, telegram, wiki):
    run_like()
    up_id, down_id = [p["message_id"] for p in feedback.latest_request()["polls"]]
    telegram.votes = {up_id: [0, 1], down_id: []}
    feedback.feedback_tap("fb 2👎")  # typed before the poll is collected
    c = feedback.feedback_collect()
    assert [(x["n"], x["verdict"]) for x in c["collected"]] == [(1, "up")]
    assert c["overridden"] == [{"request_id": feedback.latest_request()["request_id"], "n": 2}]
    verdicts = {x["song_key"]: x["verdict"] for x in rel_rows(wiki)}
    assert list(verdicts.values()).count("down") == 1 and len(rel_rows(wiki)) == 2


def test_suggest_collects_previous_polls_first(mix, telegram, wiki):
    run_like()
    first = feedback.latest_request()
    telegram.votes = {first["polls"][0]["message_id"]: [0]}
    r = run_like()
    assert r["previous_feedback"]["reply"].startswith("Saved: 👍 1 song")
    assert r["previous_feedback"]["collected"][0]["request_id"] == first["request_id"]
    st = feedback.load_state()
    assert [p["status"] for p in st["requests"][0]["polls"]] == ["closed", "closed"]
    assert [p["status"] for p in st["requests"][-1]["polls"]] == ["open", "open"]
    # The 👍 just collected already counts in the new ranking.
    assert "♥ liked" in r["items"][0]["reasons"]


def test_polls_older_than_24h_collected_by_any_tool(mix, telegram, wiki):
    run_like()
    st = feedback.load_state()
    for p in st["requests"][-1]["polls"]:
        p["sent_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    feedback.save_state(st)
    telegram.votes = {st["requests"][-1]["polls"][0]["message_id"]: [0]}
    server.TOOLS["catalog_stats"]()
    assert [p["status"] for p in feedback.latest_request()["polls"]] == ["closed", "closed"]
    assert len(rel_rows(wiki)) == 1


def test_fresh_polls_left_open_by_other_tools(mix, telegram, wiki):
    run_like()
    calls = len(telegram.calls)
    server.TOOLS["catalog_stats"]()
    assert len(telegram.calls) == calls  # < 24 h: no stopPoll
    assert feedback.collect_stale() is None


# --- feedback_tap in poll mode ---------------------------------------------------------------

def test_done_collects(mix, telegram, wiki):
    run_like()
    telegram.votes = {feedback.latest_request()["polls"][0]["message_id"]: [0, 1]}
    r = feedback.feedback_tap("done")
    assert r["handled"] and r["reply"].startswith("Saved: 👍 2 songs")
    assert len(rel_rows(wiki)) == 2


def test_star_alone_hints_and_rating_still_works(mix, telegram, wiki):
    r = run_like()
    hint = feedback.feedback_tap("1 ⭐")
    assert hint["reply"] == f"Type 1 ⭐<1-10> to rate {r['items'][0]['title']}"
    calls = len(telegram.calls)
    saved = feedback.feedback_tap("1 ⭐8")
    assert saved["reply"] == f"⭐8 saved for {r['items'][0]['title']}"
    assert len(telegram.calls) == calls  # no keyboard or pad in poll mode
    assert catalog.catalog_get(r["items"][0]["song_key"])["row"]["my_rating"] == "8"


def test_typed_thumbs_still_work(mix, telegram, wiki):
    run_like()
    r = feedback.feedback_tap("3 👍")
    assert r["handled"] and r["reply"].startswith("👍 noted:")
    assert rel_rows(wiki)[0]["verdict"] == "up"


def test_more_sends_polls_for_next_page(mix, telegram, wiki):
    run_like(limit_catalog=2, limit_outside=1)
    before = len(polls_of(telegram))
    r = feedback.feedback_tap("more")
    assert r["handled"] and [i["n"] for i in r["items"]] == [4, 5]
    new = polls_of(telegram)[before:]
    assert [o["text"].split()[0] for o in new[0]["options"]] == ["#4", "#5"]
    assert len(feedback.latest_request()["polls"]) == 4


def test_never_receives_updates(mix, telegram, wiki):
    run_like()
    feedback.feedback_tap("done")
    feedback.feedback_collect()
    assert {m for m, _ in telegram.calls} <= {"sendPoll", "stopPoll", "sendMessage"}
    assert tg._call("getUpdates", {})["error_code"] == "not_allowed"
    assert tg._call("setWebhook", {})["error_code"] == "not_allowed"


def test_old_keyboard_removed_by_separate_message(mix, telegram, wiki, monkeypatch):
    monkeypatch.setenv("FEEDBACK_MODE", "keyboard")
    run_like()  # leaves a reply keyboard on screen
    assert feedback.load_state()["keyboard_shown"] is True
    monkeypatch.setenv("FEEDBACK_MODE", "poll")
    before = len(telegram.calls)
    run_like()
    new = telegram.calls[before:]
    assert new[0] == ("sendMessage", new[0][1]) and new[0][1]["reply_markup"] == {"remove_keyboard": True}
    assert [m for m, _ in new[1:]] == ["sendPoll", "sendPoll"]
    assert all("reply_markup" not in p for m, p in new if m == "sendPoll")
    assert feedback.load_state()["keyboard_shown"] is False
    run_like()  # no keyboard to remove any more: no extra message
    assert [m for m, _ in telegram.calls[-2:]] == ["sendPoll", "sendPoll"]


def test_all_polls_of_a_page_can_be_collected(mix, telegram, wiki):
    run_like()
    ids = [p["message_id"] for p in feedback.latest_request()["polls"]]
    telegram.votes = {ids[0]: [0]}
    c = feedback.feedback_collect()
    assert c["lost"] == [] and len(c["collected"]) == 1  # the first poll too


def test_feedback_mode_off_sends_nothing_but_typed_feedback_works(mix, telegram, wiki, monkeypatch):
    monkeypatch.setenv("FEEDBACK_MODE", "off")
    r = run_like()
    assert r["success"] and telegram.calls == []
    assert feedback.latest_request().get("polls", []) == []
    assert feedback.feedback_tap("1 👍")["reply"].startswith("👍 noted:")
    assert feedback.feedback_tap("1 ⭐7")["reply"].startswith("⭐7 saved")
    assert telegram.calls == []
