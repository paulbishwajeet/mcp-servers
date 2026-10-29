"""Karaoke title normalisation, channel-index fuzzy matching on a small
fixture CSV, search-result ranking, error classification and karaoke_find
with yt-dlp stubbed out."""

import shutil

import pytest

import youtube
from conftest import FIXTURES


@pytest.mark.parametrize("raw,norm", [
    ("Pal Pal Dil Ke Paas Tum Rehti Ho - Full Karaoke With Scrolling Lyrics Eng & हिंदी",
     "pal pal dil ke paas tum rehti ho"),
    ("Jaaneman Jaanema - (𝕀𝕞𝕡𝕣𝕠𝕧𝕖𝕕 𝕍𝕖𝕣) Full Karaoke With Scrolling Lyrics Eng & हिंदी",
     "jaaneman jaanema"),
    ("TUM SE HI - KARAOKE TRACK ||Jab We Met | Mohit Chauhan [HQ]", "tum se hi jab we met mohit chauhan"),
    ("Sama Hai Suhana Suhana Karaoke with Lyrics Eng. & हिंदी", "sama hai suhana suhana"),
    ("Kuchh Na Kaho (Male) - Original Clean HD Karaoke", "kuchh na kaho"),
])
def test_normalize_title(raw, norm):
    assert youtube.normalize_title(raw) == norm


@pytest.fixture
def index(wiki):
    shutil.copy(FIXTURES / "index_small.csv", wiki / "index/anilchauhan.csv")
    return youtube.load_index()


def test_index_match_strong(index):
    c = youtube.match_index(index, "Pal Pal Dil Ke Paas", "Blackmail")
    assert c[0]["url"] == "https://www.youtube.com/watch?v=pw6r-izZArA"
    assert c[0]["score"] >= youtube.STRONG_INDEX_SCORE
    assert c[0]["source"] == "index" and c[0]["duration_sec"] == 323
    # The female version is penalised below the original.
    urls = [x["url"] for x in c]
    if "https://www.youtube.com/watch?v=AAAAAAAAAA2" in urls:
        assert urls.index("https://www.youtube.com/watch?v=AAAAAAAAAA2") > 0


def test_index_full_version_beats_abridged(index):
    c = youtube.match_index(index, "Pal Pal Dil Ke Paas")
    urls = [x["url"][-11:] for x in c]
    assert urls.index("pw6r-izZArA") < urls.index("chKG5o_7xCw")


def test_index_female_version_when_asked(index):
    c = youtube.match_index(index, "Pal Pal Dil Ke Paas female version")
    assert c[0]["url"].endswith("AAAAAAAAAA2")


def test_index_no_match_for_other_song(index):
    c = youtube.match_index(index, "Tum Hi Ho", "Aashiqui 2")
    assert not c or c[0]["score"] < youtube.STRONG_INDEX_SCORE


def test_karaoke_find_uses_index_without_search(index, monkeypatch):
    monkeypatch.setattr(youtube, "_extract", lambda *a, **k: pytest.fail("searched"))
    r = youtube.karaoke_find("Pal Pal Dil Ke Paas", "Blackmail")
    assert r["success"] and not r["used_search"]
    assert r["candidates"][0]["url"].endswith("pw6r-izZArA")


SEARCH_ENTRIES = [
    {"id": "byWCBGoXBWk", "title": "Tum Hi Ho | Arijit Singh | Aashiqui 2 | Cover by Someone",
     "channel": "Covers", "uploader_id": "@covers", "duration": 250},
    {"id": "YD6MN1SvjAc", "title": "Tum Hi Ho Karaoke with Scrolling Lyrics", "channel": "Sudhanshu Karaoke",
     "uploader_id": "@sudhanshukaraoke", "duration": 262},
    {"id": "Ab3defGhi_1", "title": "Tum Hi Ho - Full Karaoke With Scrolling Lyrics Eng & हिंदी",
     "channel": "ANIL CHAUHAN KARAOKE", "uploader_id": "@AnilChauhankaraoke", "duration": 263},
    {"id": "Zz3defGhi_2", "title": "Jaaneman Jaanema - Full Karaoke", "channel": "ANIL CHAUHAN KARAOKE",
     "uploader_id": "@AnilChauhankaraoke", "duration": 300},
    {"id": "Tm3defGhi_3", "title": "Tum Hi Ho (Aashiqui 2) song", "channel": "T-Series",
     "uploader_id": "@tseries", "duration": 262},
    {"id": "bad", "title": "a channel", "ie_key": "YoutubeTab"},
]


def test_search_ranking_preferred_then_similarity_then_karaoke(wiki):
    ranked = youtube.rank_search_results(SEARCH_ENTRIES, "Tum Hi Ho", "Aashiqui 2")
    ids = [c["url"].split("=")[1] for c in ranked]
    assert ids[0] == "Ab3defGhi_1"  # preferred channel, same song
    assert ids.index("YD6MN1SvjAc") < ids.index("byWCBGoXBWk")  # cover penalised
    assert "Zz3defGhi_2" not in ids[:2]  # preferred channel but a different song
    assert all(c["source"] == "search" for c in ranked)
    assert len(ranked) == 5  # the non-video entry is dropped


def test_karaoke_find_falls_back_to_search(index, monkeypatch):
    seen = {}

    def fake_extract(url, opts, download=False):
        seen["url"] = url
        assert opts["extract_flat"] is True
        return {"entries": SEARCH_ENTRIES}

    monkeypatch.setattr(youtube, "_extract", fake_extract)
    r = youtube.karaoke_find("Tum Hi Ho", "Aashiqui 2")
    assert r["used_search"] and seen["url"] == "ytsearch10:Tum Hi Ho Aashiqui 2 karaoke"
    assert r["candidates"][0]["url"].endswith("Ab3defGhi_1")


def test_karaoke_find_search_failure_is_soft(wiki, monkeypatch):
    def fail_extract(*a, **k):
        raise Exception("ERROR: HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(youtube, "_extract", fail_extract)
    r = youtube.karaoke_find("Tum Hi Ho")
    assert r["success"] is False and r["error_code"] == "rate_limited"


@pytest.mark.parametrize("msg,code", [
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "not_found"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: [youtube] x: Sign in to confirm your age.", "login_required"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "rate_limited"),
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("Something odd", "download_failed"),
])
def test_classify_error(msg, code):
    assert youtube.classify_error(msg) == code


def test_fetch_audio_too_long_cleans_up(wiki, monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_DURATION_SEC", "600")
    monkeypatch.setattr(youtube, "ffmpeg_path", lambda: "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(youtube, "_extract",
                        lambda url, opts, download=False: {"id": "pw6r-izZArA", "title": "Long", "duration": 3600})
    r = youtube.youtube_fetch_audio("https://youtu.be/pw6r-izZArA")
    assert r["error_code"] == "too_long" and r["job_id"] == "" and r["duration_sec"] == 3600
    assert list((tmp_path / "media-tmp").iterdir()) == []


def test_check_links_reports_dead_not_network_errors(wiki, monkeypatch):
    import catalog
    for i, title in enumerate(["A", "B", "C", "D"]):
        catalog.catalog_upsert({"title": title, "karaoke_url": f"https://youtu.be/aaaaaaaaaa{i}"})
    results = {
        "0": {"success": True, "availability": "public"},
        "1": {"success": False, "error_code": "private", "error": "Private video"},
        "2": {"success": False, "error_code": "rate_limited", "error": "429"},
        "3": {"success": True, "availability": "needs_auth"},
    }
    monkeypatch.setattr(youtube, "youtube_info", lambda url: results[url[-1]] | {
        "error_code": results[url[-1]].get("error_code", ""), "error": results[url[-1]].get("error", "")})
    r = youtube.check_links()
    assert [(d["title"], d["status"]) for d in r["dead"]] == [("B", "private"), ("D", "needs_auth")]
    assert [e["title"] for e in r["errors"]] == ["C"]
    assert catalog.catalog_get("b")["row"]["karaoke_url"]  # catalog untouched


def test_repeated_word_query_is_not_a_strong_subset_match():
    """token_set alone scores "Deewangi Deewangi" 100 against a different song."""
    s = youtube.title_score("Deewangi Deewangi", "Aisi Deewangi Dekhi Nahin Kahin - Full Karaoke")
    assert s < youtube.STRONG_INDEX_SCORE
    assert youtube.title_score("Baharon Phool Barsao",
                               "Baharon Phool Barsaao Mera Mehboob Aaya Hai - Full Karaoke") > 80


# --- playlists ----------------------------------------------------------------------

PL = "https://www.youtube.com/watch?v=uVQvcga6JWc&list=PLMJjuIqd6cqlhxmr1jFnHaOj1hlkEbNeo"
PL_ENTRIES = [
    {"id": "aaaaaaaaaa1", "title": "Pal Pal Dil Ke Paas Tum Rehti Ho - Full Karaoke", "duration": 323},
    {"id": "aaaaaaaaaa2", "title": "Bole Chudiyan Bole Kangna Karaoke With Scrolling Lyrics", "duration": 440},
    {"id": "aaaaaaaaaa3", "title": "Diwana hua badal - karaoke --Remix...", "duration": 298},
    {"id": "aaaaaaaaaa4", "title": "Kishore Kumar Medley Karaoke", "duration": 500},
    {"id": "aaaaaaaaaa5", "title": "[Private video]", "duration": None},
    {"id": "aaaaaaaaaa6", "title": "Soch Na Sake Karaoke", "duration": 283},
    {"id": "aaaaaaaaaa6", "title": "Soch Na Sake Karaoke (dup entry)", "duration": 283},
    {"id": "aaaaaaaaaa7", "title": "Bole Chudiyan Bole Kangna Haye Main Karaoke", "duration": 250},
    {"id": "aaaaaaaaaa8", "title": "KARAOKE - KHAMOSHIYA || TERI DEEWANI || JANAM JANAM || BHEEGI", "duration": 345},
]


@pytest.fixture
def playlist(wiki, monkeypatch):
    import catalog
    catalog.catalog_upsert({"title": "Pal Pal Dil Ke Paas", "film": "Blackmail", "year": 1973,
                            "karaoke_url": "https://www.youtube.com/watch?v=aaaaaaaaaa1"})
    catalog.catalog_upsert({"title": "Bole Chudiyan", "film": "Kabhi Khushi Kabhie Gham", "year": 2001,
                            "karaoke_url": "https://www.youtube.com/watch?v=zzzzzzzzzzz"})
    catalog.catalog_upsert({"title": "Deewana Hua Badal", "film": "Kashmir Ki Kali", "year": 1964})
    seen = {}

    def fake_extract(url, opts, download=False):
        seen["url"], seen["flat"] = url, opts.get("extract_flat")
        return {"id": "PLMJjuIqd6cqlhxmr1jFnHaOj1hlkEbNeo", "title": "karaoke", "entries": PL_ENTRIES}

    monkeypatch.setattr(youtube, "_extract", fake_extract)
    return seen


@pytest.mark.parametrize("url,code", [
    ("https://vimeo.com/playlist?list=PLMJjuIqd6cqlhxmr1jFnHaOj1hlkEbNeo", "domain_not_allowed"),
    ("https://www.youtube.com/watch?v=uVQvcga6JWc", "invalid_url"),
    ("https://www.youtube.com/playlist?list=bad id!", "invalid_url"),
])
def test_playlist_url_checks(wiki, url, code):
    assert youtube.playlist_list(url)["error_code"] == code
    assert youtube.playlist_triage(url)["error_code"] == code


def test_playlist_list_flat_and_paged(playlist):
    r = youtube.playlist_list(PL, limit=3)
    assert playlist["url"] == "https://www.youtube.com/playlist?list=PLMJjuIqd6cqlhxmr1jFnHaOj1hlkEbNeo"
    assert playlist["flat"] == "in_playlist"  # never a full per-video extraction
    assert r["total"] == 8  # duplicate id dropped
    assert [v["video_id"] for v in r["videos"]] == ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"]
    assert r["next_offset"] == 3
    assert youtube.playlist_list(PL, offset=6)["next_offset"] is None
    assert youtube.playlist_list(PL, offset=4)["videos"][0]["unavailable"] is True


def test_playlist_triage_groups(playlist):
    r = youtube.playlist_triage(PL)
    assert r["success"] and r["total"] == 8
    ex = {i["video_id"]: i for i in r["existing"]}
    assert ex["aaaaaaaaaa1"]["song_id"] == "pal-pal-dil-ke-paas-1973" and ex["aaaaaaaaaa1"]["link"] == "same"
    assert ex["aaaaaaaaaa2"]["song_id"] == "bole-chudiyan-2001" and ex["aaaaaaaaaa2"]["link"] == "different"
    nv = r["new_version"][0]
    assert nv["video_id"] == "aaaaaaaaaa3" and nv["version"] == "remix" and nv["song_id"] == "deewana-hua-badal-1964"
    assert [i["video_id"] for i in r["medley"]] == ["aaaaaaaaaa4"]
    assert [i["video_id"] for i in r["unavailable"]] == ["aaaaaaaaaa5"]
    new = {i["video_id"]: i for i in r["new"]}
    assert new["aaaaaaaaaa6"]["song_text"] == "soch na sake"
    assert "may be a medley" in new["aaaaaaaaaa8"]["note"]
    assert r["maybe"][0]["video_id"] == "aaaaaaaaaa7"  # short title + many extra words: confirm
    assert sum(r["counts"].values()) == 8


def test_short_catalog_title_does_not_match_everything():
    assert youtube.match_score("Pal", "pal pal dil ke paas") < youtube.MAYBE_SCORE
    assert youtube.match_score("Pal Pal Dil Ke Paas", "pal pal dil ke paas tum rehti ho") >= youtube.MATCH_SCORE
    assert youtube.match_score("Bole Chudiyan", "bole chudiyan bole kangna") >= youtube.MATCH_SCORE
    assert youtube.match_score("Deewana Hua Badal", "diwana hua badal") >= youtube.MATCH_SCORE
    assert youtube.MAYBE_SCORE <= youtube.match_score("Tum Hi Ho", "tum hi ho bandhu sakha tum hi") < youtube.MATCH_SCORE
