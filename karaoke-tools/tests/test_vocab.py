"""vocab.py must mirror SCHEMA.md. This parses the SCHEMA.md tables (a copy of
the real one lives in tests/fixtures/wiki/) and fails on any drift.

When SCHEMA.md changes: copy the new one into tests/fixtures/wiki/, run
this test, and update vocab.py until it passes."""

import re

import pytest

import vocab
from conftest import FIXTURES

SCHEMA = (FIXTURES / "wiki" / "SCHEMA.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", SCHEMA, re.S | re.M)
    assert m, f"SCHEMA.md has no '## {title}' section"
    return m.group(1)


def song_table() -> dict[str, tuple[str, str]]:
    """column -> (type, meaning) from the songs.csv table."""
    rows = {}
    for line in section("`songs.csv` columns").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] not in ("Column", "---") and not set(cells[0]) <= set("-"):
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def test_song_columns_match_schema_order():
    assert list(song_table()) == vocab.SONG_COLUMNS


def test_song_columns_match_skeleton_header():
    header = (FIXTURES / "wiki/catalog/songs.csv").read_text().splitlines()[0]
    assert header.split(",") == vocab.SONG_COLUMNS


def test_review_columns_match_schema_and_skeleton():
    m = re.search(r"`(logged_at[^`]*)`", section("`review.csv` columns"))
    assert [c.strip() for c in m.group(1).split(",")] == vocab.REVIEW_COLUMNS
    header = (FIXTURES / "wiki/catalog/review.csv").read_text().splitlines()[0]
    assert header.split(",") == vocab.REVIEW_COLUMNS


def test_index_columns_match_schema():
    m = re.search(r"`(video_id,[^`]*)`", section("Files"))
    assert m.group(1).split(",") == vocab.INDEX_COLUMNS


def test_every_listed_value_set_matches():
    """Every enum/multi column whose meaning lists backticked values must
    match vocab exactly, and vocab must not have lists SCHEMA.md lacks."""
    listed = {}
    for col, (typ, meaning) in song_table().items():
        if typ not in ("enum", "multi"):
            continue
        values = re.findall(r"`([^`]+)`", meaning)
        if values:
            listed[col] = set(values)
    assert set(listed) == set(vocab.SCHEMA_VALUE_LISTS), "columns with value lists differ"
    for col, values in listed.items():
        mine = vocab.SCHEMA_VALUE_LISTS[col]
        assert len(mine) == len(set(mine)), f"{col}: duplicates in vocab"
        assert set(mine) == values, f"{col}: vocab {sorted(set(mine) ^ values)} differ from SCHEMA.md"


def test_types_match_vocab_categories():
    table = song_table()
    for col, (typ, _) in table.items():
        if typ == "enum":
            assert col in vocab.ENUM_FIELDS, col
        elif typ == "multi":
            assert col in vocab.MULTI_ENUM_FIELDS or col in vocab.MULTI_FREE_FIELDS, col
        elif typ.startswith("int"):
            assert col in vocab.INT_FIELDS, col
            m = re.search(r"(\d+)-(\d+)", typ)
            if m:
                assert vocab.INT_FIELDS[col] == (int(m.group(1)), int(m.group(2))), col
        elif typ == "number":
            assert col in vocab.NUMBER_FIELDS, col
        elif typ == "date":
            assert col in vocab.DATE_FIELDS, col
        elif typ == "bool":
            assert col in vocab.BOOL_FIELDS, col
        elif typ == "url":
            assert col in vocab.URL_FIELDS, col
    assert set(vocab.ENUM_FIELDS) == {c for c, (t, _) in table.items() if t == "enum"}


def test_review_problems_match():
    m = re.search(r"`problem` is one of (.*?)\.\n", section("`review.csv` columns"))
    assert set(re.findall(r"`([^`]+)`", m.group(1))) == set(vocab.REVIEW_PROBLEM)


def test_outcome_scores_match():
    meaning = song_table()["crowd_score"][1]
    pairs = dict((k, int(v)) for k, v in re.findall(r"(\w+) = (\d)", meaning))
    assert pairs == vocab.OUTCOME_SCORES


@pytest.mark.parametrize("year,era", [(1955, "pre-1960"), (1960, "1960s"), (1973, "1970s"),
                                      (1999, "1990s"), (2024, "2020s")])
def test_era_for_year(year, era):
    assert vocab.era_for_year(year) == era
