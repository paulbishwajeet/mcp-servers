"""catalog.py: songs.csv upsert/validation/writes, review.csv, lyrics files,
events, stats, and Telegram chunking."""

import csv
import os

import pytest

import catalog
import vocab

PAL = {"title": "Pal Pal Dil Ke Paas", "film": "Blackmail", "year": 1973,
       "singers": ["Kishore Kumar"], "music_director": "Kalyanji-Anandji",
       "language": "hindi", "voice_type": "solo-male", "mood": "romantic;longing",
       "added_from": "song-name"}


def songs_csv(wiki):
    return (wiki / "catalog/songs.csv").read_text(encoding="utf-8")


def log_lines(wiki):
    return [ln for ln in (wiki / "log.md").read_text().splitlines() if ln[:2] == "20"]


# --- upsert -------------------------------------------------------------------------

def test_insert_generates_id_fills_era_and_defaults(wiki):
    r = catalog.catalog_upsert(dict(PAL))
    assert r["success"], r
    assert r["action"] == "add"
    assert r["song_id"] == "pal-pal-dil-ke-paas-1973"
    row = r["row"]
    assert row["era"] == "1970s"
    assert row["status"] == "wishlist"
    assert row["added_on"]
    assert row["singers"] == "Kishore Kumar"
    assert row["mood"] == "romantic;longing"


def test_local_mp3_starts_practicing(wiki):
    r = catalog.catalog_upsert({"title": "Chandan Sa Badan", "year": 1965,
                                "added_from": "local-mp3"})
    assert r["row"]["status"] == "practicing"
    assert r["row"]["era"] == "1960s"


def test_pre_1960_era(wiki):
    assert catalog.catalog_upsert({"title": "Old", "year": 1955})["row"]["era"] == "pre-1960"


def test_collision_suffix(wiki):
    ids = [catalog.catalog_upsert(dict(PAL))["song_id"] for _ in range(3)]
    assert ids == ["pal-pal-dil-ke-paas-1973", "pal-pal-dil-ke-paas-1973-2",
                   "pal-pal-dil-ke-paas-1973-3"]


def test_duplicate_warning(wiki):
    catalog.catalog_upsert(dict(PAL))
    r = catalog.catalog_upsert(dict(PAL))
    assert any("Possible duplicate" in w for w in r["warnings"])


def test_id_without_year_and_non_ascii(wiki):
    assert catalog.catalog_upsert({"title": "Ae Dil Hai Mushkil!"})["song_id"] == "ae-dil-hai-mushkil"
    assert catalog.catalog_upsert({"title": "पल पल"})["song_id"] == "song"


def test_merge_update_changes_only_given_fields(wiki):
    sid = catalog.catalog_upsert(dict(PAL))["song_id"]
    before = catalog.catalog_get(sid)["row"]
    r = catalog.catalog_upsert({"song_id": sid, "energy": 2, "status": "ready",
                                "bpm": "72.44"})
    assert r["success"] and r["action"] == "update"
    assert sorted(r["changed"]) == ["bpm", "energy", "status"]
    after = catalog.catalog_get(sid)["row"]
    assert after["bpm"] == "72.4"
    for col in vocab.SONG_COLUMNS:
        if col not in ("energy", "status", "bpm"):
            assert after[col] == before[col], col


def test_update_with_no_change_does_not_write_or_log(wiki):
    sid = catalog.catalog_upsert(dict(PAL))["song_id"]
    n = len(log_lines(wiki))
    mtime = os.stat(wiki / "catalog/songs.csv").st_mtime_ns
    r = catalog.catalog_upsert({"song_id": sid, "film": "Blackmail"})
    assert r["action"] == "unchanged"
    assert len(log_lines(wiki)) == n
    assert os.stat(wiki / "catalog/songs.csv").st_mtime_ns == mtime


def test_year_change_recomputes_era(wiki):
    sid = catalog.catalog_upsert(dict(PAL))["song_id"]
    r = catalog.catalog_upsert({"song_id": sid, "year": 1984})
    assert r["row"]["era"] == "1980s"
    assert r["song_id"] == sid  # ids are permanent


@pytest.mark.parametrize("field,value", [
    ("voice_type", "male-solo"), ("mood", "romantic;dreamy"), ("status", "done"),
    ("genre", ["peppy", "bollywood"]), ("era", "60s"), ("language", "hinglish"),
])
def test_enum_rejection_lists_allowed_values(wiki, field, value):
    r = catalog.catalog_upsert({"title": "X", field: value})
    assert not r["success"]
    assert r["error_code"] == "validation_failed"
    assert "Allowed:" in r["error"]
    assert not catalog.load_songs()  # nothing written


@pytest.mark.parametrize("field,value", [
    ("energy", 6), ("energy", "high"), ("singalong", 0), ("year", 1800),
    ("my_key_change", "+15"), ("bpm", "fast"), ("added_on", "27/10/2026"),
    ("needs_review", "maybe"), ("karaoke_url", "https://vimeo.com/1"),
    ("karaoke_alt_urls", ["https://youtu.be/aaaaaaaaaaa"] * 1 + ["https://www.youtube.com/watch?v=bbbbbbbbbbb", "https://youtu.be/ccccccccccc"]),
    ("emotion", "Very Sad Feeling!"), ("lyrics_file", "lyrics/other.txt"),
])
def test_type_and_range_rejection(wiki, field, value):
    r = catalog.catalog_upsert({"title": "X", field: value})
    assert not r["success"], (field, value)


def test_unknown_column_rejected(wiki):
    r = catalog.catalog_upsert({"title": "X", "tempo": "slow"})
    assert r["error_code"] == "unknown_column"


def test_placeholders_become_empty(wiki):
    r = catalog.catalog_upsert({"title": "X", "film": "N/A", "lyricist": "?"})
    assert r["success"]
    assert r["row"]["film"] == "" and r["row"]["lyricist"] == ""
    assert r["warnings"]


def test_case_normalised_enums_and_my_key_change(wiki):
    r = catalog.catalog_upsert({"title": "X", "voice_type": "Duet-MF", "my_key_change": "-2",
                                "emotion": ["yearning", "first-love"]})
    assert r["row"]["voice_type"] == "duet-mf"
    assert r["row"]["my_key_change"] == "-2"
    assert r["row"]["emotion"] == "yearning;first-love"


# --- file format ----------------------------------------------------------------------

def test_sorted_rows_and_quoting_round_trip(wiki):
    catalog.catalog_upsert({"title": "Zaalima", "year": 2017, "notes": 'Say "hi", then sing'})
    catalog.catalog_upsert({"title": "Aaj Ki Raat", "year": 2024,
                            "film": "Stree 2", "notes": "Hinglish: dil, jaan; pyaar\nline 2"})
    catalog.catalog_upsert({"title": "Mera Mann", "year": 2015, "bpm": 96})
    text = songs_csv(wiki)
    lines = text.splitlines()
    assert lines[0] == ",".join(vocab.SONG_COLUMNS)  # header bare, as in the skeleton
    with open(wiki / "catalog/songs.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["song_id"] for r in rows] == ["aaj-ki-raat-2024", "mera-mann-2015", "zaalima-2017"]
    assert rows[2]["notes"] == 'Say "hi", then sing'
    assert rows[0]["notes"] == "Hinglish: dil, jaan; pyaar\nline 2"
    assert '"Say ""hi"", then sing"' in text
    assert ',96.0,' in text  # numbers bare
    assert ',"Mera Mann",' in text  # text quoted
    assert ',"",' in text  # empty text quoted


def test_atomic_write_leaves_old_file_on_failure(wiki, monkeypatch):
    catalog.catalog_upsert(dict(PAL))
    before = songs_csv(wiki)

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(catalog.os, "replace", broken_replace)
    with pytest.raises(OSError):
        catalog.catalog_upsert({"title": "Another"})
    assert songs_csv(wiki) == before
    assert not [p for p in (wiki / "catalog").iterdir() if p.name.endswith(".tmp")]


def test_log_line_format(wiki):
    sid = catalog.catalog_upsert(dict(PAL))["song_id"]
    catalog.catalog_upsert({"song_id": sid, "energy": 2})
    lines = log_lines(wiki)
    assert len(lines) == 2
    parts = [p.strip() for p in lines[0].split("|")]
    assert len(parts) == 4
    assert parts[1] == "add" and parts[2] == sid
    assert lines[1].split(" | ")[1:3] == ["update", sid]
    assert "energy" in lines[1]


def test_bad_header_is_schema_mismatch(wiki):
    (wiki / "catalog/songs.csv").write_text("song_id,title\n")
    from server import TOOLS
    r = TOOLS["catalog_search"]()
    assert r["error_code"] == "schema_mismatch"


# --- search + get --------------------------------------------------------------------------

@pytest.fixture
def stocked(wiki):
    rows = [
        {"title": "Pal Pal Dil Ke Paas", "film": "Blackmail", "year": 1973,
         "singers": "Kishore Kumar", "voice_type": "solo-male", "tempo_class": "slow",
         "energy": 2, "mood": "romantic;longing", "status": "ready", "crowd_score": 2.5,
         "singalong": 4},
        {"title": "Aaj Ki Raat", "film": "Stree 2", "year": 2024, "singers": "Madhubanti Bagchi",
         "voice_type": "solo-female", "tempo_class": "fast", "energy": 5,
         "mood": "energetic;playful", "status": "wishlist", "singalong": 3},
        {"title": "Tum Se Hi", "film": "Jab We Met", "year": 2007, "singers": "Mohit Chauhan",
         "voice_type": "solo-male", "tempo_class": "medium", "energy": 3, "mood": "romantic",
         "status": "practicing", "singalong": 4},
        {"title": "Jab Koi Baat", "film": "Jurm", "year": 1990,
         "singers": "Kumar Sanu;Asha Bhosle", "voice_type": "duet-mf", "mood": "romantic",
         "status": "ready", "crowd_score": 3, "singalong": 2},
    ]
    return [catalog.catalog_upsert(r)["song_id"] for r in rows]


def test_search_sort_and_compact(stocked):
    r = catalog.catalog_search(mood=["romantic"])
    ids = [x["song_id"] for x in r["rows"]]
    assert ids == ["jab-koi-baat-1990", "pal-pal-dil-ke-paas-1973", "tum-se-hi-2007"]
    assert set(r["rows"][0]) == set(catalog.COMPACT_FIELDS)
    full = catalog.catalog_search(mood="romantic", full=True)["rows"][0]
    assert set(full) == set(vocab.SONG_COLUMNS)


def test_search_filters(stocked):
    assert [x["song_id"] for x in catalog.catalog_search(min_energy=4)["rows"]] == ["aaj-ki-raat-2024"]
    assert catalog.catalog_search(voice_type="duet-mf")["total"] == 1
    assert catalog.catalog_search(singer="asha")["rows"][0]["song_id"] == "jab-koi-baat-1990"
    assert catalog.catalog_search(text="pal pal dil")["rows"][0]["song_id"] == "pal-pal-dil-ke-paas-1973"
    assert catalog.catalog_search(text="jab we met")["rows"][0]["song_id"] == "tum-se-hi-2007"
    assert catalog.catalog_search(exclude_ids=["jab-koi-baat-1990"], status="ready")["total"] == 1
    assert catalog.catalog_search(era=["1970s", "2020s"])["total"] == 2
    assert catalog.catalog_search(limit=1)["returned"] == 1


def test_search_rejects_unknown_filter_values(stocked):
    r = catalog.catalog_search(mood=["dreamy"])
    assert r["error_code"] == "invalid_filter" and "Allowed:" in r["error"]


def test_get_by_id_and_fuzzy(stocked):
    assert catalog.catalog_get("tum-se-hi-2007")["row"]["film"] == "Jab We Met"
    r = catalog.catalog_get("pal pal dil")
    assert r["found"] is False
    assert r["matches"][0]["song_id"] == "pal-pal-dil-ke-paas-1973"
    assert len(r["matches"]) <= 5


# --- review.csv ---------------------------------------------------------------------------------

def test_review_add_list_resolve(stocked, wiki):
    sid = stocked[0]
    r = catalog.review_add(sid, "/seed/x.mp3", "unsure-match", "two films with this song")
    assert r["success"] and r["row_number"] == 1 and r["song_flagged"]
    assert catalog.catalog_get(sid)["row"]["needs_review"] == "true"
    catalog.review_add(sid, "song name", "no-lyrics")
    assert catalog.review_list()["count"] == 2
    assert catalog.review_resolve(1)["success"]
    assert catalog.catalog_get(sid)["row"]["needs_review"] == "true"  # one still open
    r = catalog.review_resolve(2)
    assert r["song_unflagged"]
    assert catalog.catalog_get(sid)["row"]["needs_review"] == "false"
    assert catalog.review_list()["count"] == 0
    assert catalog.review_list(open_only=False)["count"] == 2  # never deleted
    with open(wiki / "catalog/review.csv", newline="") as f:
        assert [r["resolved"] for r in csv.DictReader(f)] == ["true", "true"]
    assert catalog.review_resolve(9)["error_code"] == "not_found"


def test_review_rejects_unknown_problem(wiki):
    r = catalog.review_add("", "x.mp3", "bad-vibes")
    assert not r["success"] and "unsure-match" in r["error"]


# --- lyrics files -------------------------------------------------------------------------------

def test_lyrics_save_and_get(stocked, wiki):
    sid = stocked[0]
    text = "Pal pal dil ke paas tum rehti ho\nJeevan meethi pyaas\n\n\n\nHar shaam aankhon par"
    r = catalog.lyrics_save(sid, text, "genius", "https://genius.com/x", chords="C Am F G")
    assert r["success"], r
    content = (wiki / f"lyrics/{sid}.txt").read_text()
    assert content.startswith("# Pal Pal Dil Ke Paas\n# Blackmail (1973) | Kishore Kumar | "
                              "source: genius | https://genius.com/x\n\n")
    assert "\n\n\n" not in content
    assert "## Chords" in content
    row = catalog.catalog_get(sid)["row"]
    assert (row["lyrics_file"], row["lyrics_source"], row["lyrics_url"]) == \
        (f"lyrics/{sid}.txt", "genius", "https://genius.com/x")
    g = catalog.lyrics_get(sid)
    assert g["title"] == "Pal Pal Dil Ke Paas"
    assert g["text"].startswith("Pal pal dil")
    assert g["chords"] == "C Am F G"
    assert g["chunks"][0].startswith("Pal Pal Dil Ke Paas\n\n")


def test_lyrics_save_validation(stocked):
    assert catalog.lyrics_save(stocked[0], "x", "azlyrics")["error_code"] == "validation_failed"
    assert catalog.lyrics_save("no-such-song", "x\ny", "neel")["error_code"] == "not_found"
    assert catalog.lyrics_get("../../etc/passwd")["error_code"] == "invalid_input"


# --- chunking -----------------------------------------------------------------------------------

def test_chunks_under_limit_and_on_verse_boundaries():
    verses = [f"Verse {i} line one\n" + "\n".join(f"line {j} " + "la " * 20 for j in range(8))
              for i in range(40)]
    text = "\n\n".join(verses)
    chunks = catalog.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) < 3500 for c in chunks)
    for c in chunks:  # every chunk starts at a verse start and holds whole verses
        assert c.startswith("Verse ")
        assert all(v.startswith("Verse ") for v in c.split("\n\n"))
    assert "\n\n".join(chunks) == text.strip()


def test_chunks_split_giant_verse_and_line():
    giant = "\n".join("x" * 100 for _ in range(100))  # one 10k-char verse
    line = "y" * 9000
    for text in (giant, line, giant + "\n\n" + line):
        chunks = catalog.chunk_text(text)
        assert all(0 < len(c) < 3500 for c in chunks)
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# --- events ---------------------------------------------------------------------------------------

EVENT = """---
event: Diwali 2026
date: 2026-11-01
venue: Community hall
event_type: festival        # festival | wedding
---

# Diwali 2026

## Set list

| # | song_id | Title | Key change | Link used | Outcome | Notes |
|---|---------|-------|-----------|-----------|---------|-------|
| 1 | pal-pal-dil-ke-paas-1973 | Pal Pal Dil Ke Paas | -2 | | standout | |
| 2 | tum-se-hi-2007 | Tum Se Hi | 0 | | weak | mic issue |
| 3 | not-in-catalog-1999 | Mystery | | | good | |
| 4 | jab-koi-baat-1990 | Jab Koi Baat | | | standout / good / weak | |

## Audience requests

-
"""

EVENT2 = """---
event: House party
date: 2026-12-24
---

## Set list

| # | song_id | Title | Key change | Link used | Outcome | Notes |
|---|---|---|---|---|---|---|
| 1 | pal-pal-dil-ke-paas-1973 | Pal Pal | | | good | |
"""


def test_event_apply_and_idempotency(stocked, wiki):
    (wiki / "events/2026-11-01-diwali.md").write_text(EVENT)
    r = catalog.event_apply("2026-11-01-diwali.md")
    assert r["success"], r
    assert r["event_date"] == "2026-11-01"
    assert r["unmatched"] == [{"song_id": "not-in-catalog-1999", "title": "Mystery"}]
    pal = catalog.catalog_get("pal-pal-dil-ke-paas-1973")["row"]
    assert (pal["times_performed"], pal["crowd_score"], pal["last_performed"]) == \
        ("1", "3", "2026-11-01")
    tum = catalog.catalog_get("tum-se-hi-2007")["row"]
    assert (tum["times_performed"], tum["crowd_score"]) == ("1", "1")
    jab = catalog.catalog_get("jab-koi-baat-1990")["row"]  # placeholder outcome: counted, unscored
    assert (jab["times_performed"], jab["crowd_score"]) == ("1", "")
    snapshot = songs_csv(wiki)
    n_log = len(log_lines(wiki))

    again = catalog.event_apply("events/2026-11-01-diwali.md")
    assert again["success"] and again["updated"] == []
    assert songs_csv(wiki) == snapshot
    assert len(log_lines(wiki)) == n_log

    (wiki / "events/2026-12-24-party.md").write_text(EVENT2)
    r2 = catalog.event_apply("2026-12-24-party.md")
    pal = catalog.catalog_get("pal-pal-dil-ke-paas-1973")["row"]
    assert (pal["times_performed"], pal["crowd_score"], pal["last_performed"]) == \
        ("2", "2.5", "2026-12-24")
    assert [u["song_id"] for u in r2["updated"]] == ["pal-pal-dil-ke-paas-1973"]
    # Re-applying the first event now sees both events and changes nothing.
    assert catalog.event_apply("2026-11-01-diwali.md")["updated"] == []


def test_event_apply_rejects_template_and_traversal(wiki):
    assert catalog.event_apply("_TEMPLATE.md")["error_code"] == "invalid_input"
    assert catalog.event_apply("../SCHEMA.md")["error_code"] == "invalid_input"
    assert catalog.event_apply("missing.md")["error_code"] == "not_found"


# --- stats ------------------------------------------------------------------------------------------

def test_stats_and_readme_block(stocked, wiki):
    catalog.review_add(stocked[0], "x", "no-lyrics")
    (wiki / "events/2026-11-01-diwali.md").write_text(EVENT)
    r = catalog.catalog_stats()
    assert r["songs"] == 4
    assert r["by_status"] == {"practicing": 1, "ready": 2, "wishlist": 1}
    assert r["open_reviews"] == 1
    assert r["missing_lyrics"] == 4 and r["missing_links"] == 4
    assert r["events_logged"] == 1
    assert r["readme_updated"]
    readme = (wiki / "README.md").read_text()
    assert "- Songs: 4 (ready: 2, practicing: 1, wishlist: 1)" in readme
    assert "- Events logged: 1" in readme
    assert readme.count("## Quick stats") == 1
    assert readme.startswith("# KaraokeGayak repertoire")
    assert catalog.catalog_stats()["readme_updated"] is False  # no change, no rewrite
