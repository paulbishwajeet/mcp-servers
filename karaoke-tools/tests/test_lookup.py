"""Wikipedia lookup: HTML-to-text and infobox parsing on fixtures, and the
lookup flow with the API stubbed out."""

import lookup
from conftest import fixture_text


def test_section_html_to_text():
    text = lookup.html_to_text(fixture_text("wiki_music_section.html"))
    assert "edit" not in text and "[1]" not in text
    assert '"Pal Pal Dil Ke Paas" | Kishore Kumar' in text
    assert '"Mile Mile Do Badan" | Kishore Kumar, Lata Mangeshkar' in text
    assert "Rajendra Krishan" in text


def test_infobox():
    box = lookup.parse_infobox(fixture_text("wiki_lead.html"))
    assert box["music_by"] == "Kalyanji–Anandji"
    assert box["release_date"] == "30 November 1973"
    assert box["language"] == "Hindi"


def test_song_lookup_flow(monkeypatch):
    def fake_api(**p):
        if p.get("list") == "search":
            return {"query": {"search": [{"title": "List of Hindi films of 1973"},
                                         {"title": "Blackmail (1973 film)"},
                                         {"title": "Blackmail (disambiguation)"}]}}
        if p.get("prop") == "sections|categories":
            return {"parse": {"title": "Blackmail (1973 film)",
                              "sections": [{"index": "1", "line": "Plot"}, {"index": "3", "line": "Music"}],
                              "categories": [{"category": "1973_films"}]}}
        if p.get("section") == 0:
            return {"parse": {"text": fixture_text("wiki_lead.html")}}
        assert p.get("section") == "3"
        return {"parse": {"text": fixture_text("wiki_music_section.html")}}

    monkeypatch.setattr(lookup, "_api", fake_api)
    r = lookup.song_lookup("Pal Pal Dil Ke Paas", "Blackmail")
    assert r["success"] and r["found"]
    assert r["page_title"] == "Blackmail (1973 film)"
    assert r["release_year"] == "1973"
    assert r["section_title"] == "Music"
    assert r["song_mentioned"] is True
    assert r["infobox"]["music_by"] == "Kalyanji–Anandji"


def test_song_lookup_not_found(monkeypatch):
    monkeypatch.setattr(lookup, "_api", lambda **p: {"query": {"search": []}})
    r = lookup.song_lookup("Nonexistent Song", "Nonexistent Film")
    assert r["success"] and r["found"] is False


def test_song_lookup_network_error_is_soft(monkeypatch):
    import requests

    def boom(**p):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(lookup, "_api", boom)
    r = lookup.song_lookup("X", "Y")
    assert r["success"] is False and r["error_code"] == "network_error"


def test_song_lookup_validates_year():
    assert lookup.song_lookup("X", "Y", "73")["error_code"] == "invalid_input"
