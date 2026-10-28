"""Domain allowlists for YouTube and lyrics. Rejections happen before any request."""

import pytest

import catalog
import lyrics
import youtube
from common import is_youtube_url


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=pw6r-izZArA", "https://youtube.com/watch?v=pw6r-izZArA",
    "https://m.youtube.com/watch?v=pw6r-izZArA", "https://music.youtube.com/watch?v=pw6r-izZArA",
    "https://youtu.be/pw6r-izZArA", "HTTPS://WWW.YOUTUBE.COM/watch?v=pw6r-izZArA",
])
def test_youtube_allowed(url):
    assert is_youtube_url(url)
    assert youtube.video_id_from_url(url) == "pw6r-izZArA"


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.com/watch?v=pw6r-izZArA", "https://evilyoutube.com/watch?v=x",
    "https://www.youtube.com@evil.com/watch?v=pw6r-izZArA", "https://gaming.youtube.com/x",
    "ftp://youtube.com/watch?v=pw6r-izZArA", "javascript:alert(1)", "youtube.com/watch?v=x",
    "https://vimeo.com/123", "",
])
def test_youtube_rejected(url, wiki):
    assert not is_youtube_url(url)
    for fn in (youtube.youtube_info, youtube.youtube_fetch_audio):
        r = fn(url)
        assert r["success"] is False and r["error_code"] == "domain_not_allowed"
    assert not list((wiki.parent / "media-tmp").iterdir())  # no job folder


@pytest.mark.parametrize("url", ["https://www.youtube.com/@AnilChauhankaraoke",
                                 "https://www.youtube.com/playlist?list=PL123",
                                 "https://www.youtube.com/watch?v=short"])
def test_youtube_non_video_urls(url, wiki):
    assert youtube.youtube_info(url)["error_code"] == "invalid_url"


def test_shorts_and_embed_ids():
    assert youtube.video_id_from_url("https://www.youtube.com/shorts/pw6r-izZArA") == "pw6r-izZArA"
    assert youtube.video_id_from_url("https://www.youtube.com/embed/pw6r-izZArA?x=1") == "pw6r-izZArA"


def test_channel_index_refresh_url_checks(wiki):
    assert youtube.channel_index_refresh("https://vimeo.com/x")["error_code"] == "domain_not_allowed"
    assert youtube.channel_index_refresh("https://www.youtube.com/watch?v=pw6r-izZArA")[
        "error_code"] == "invalid_url"


def test_karaoke_url_field_requires_youtube(wiki):
    assert not catalog.catalog_upsert({"title": "X", "karaoke_url": "https://evil.com/v"})["success"]
    assert catalog.catalog_upsert({"title": "X", "karaoke_url": "https://youtu.be/pw6r-izZArA"})["success"]


@pytest.mark.parametrize("url", [
    "https://genius.com/Kishore-kumar-pal-pal-dil-ke-paas-lyrics",
    "https://www.smule.com/song/x", "https://smule.com/song/x",
    "https://tabs.ultimate-guitar.com/tab/x/y-chords-1",
])
def test_lyrics_pages_allowed(url):
    assert lyrics.is_allowed_page_url(url)


@pytest.mark.parametrize("url", [
    "https://genius.com.evil.com/x", "https://evilgenius.com/x", "https://azlyrics.com/x",
    "https://www.ultimate-guitar.com/search.php?value=x",  # search host: internal use only
    "https://api.genius.com/search?q=x", "https://ultimate-guitar.com/tab/x",
    "http://localhost/x", "file:///etc/passwd", "",
])
def test_lyrics_pages_rejected(url, wiki):
    assert not lyrics.is_allowed_page_url(url)
    r = lyrics.lyrics_from_url(url)
    assert r["success"] is False and r["error_code"] in ("domain_not_allowed", "invalid_url")


def test_lyrics_extra_domains(wiki, monkeypatch):
    monkeypatch.setenv("LYRICS_EXTRA_DOMAINS", "hindilyrics.net, lyricsbogie.com")
    assert lyrics.is_allowed_page_url("https://www.hindilyrics.net/x")
    assert lyrics.is_allowed_page_url("https://lyricsbogie.com/x")
    assert not lyrics.is_allowed_page_url("https://hindilyrics.net.evil.com/x")


def test_fetch_refuses_redirect_off_allowlist(wiki, monkeypatch):
    class Resp:
        status_code = 302
        is_redirect = True
        headers = {"location": "https://evil.com/steal"}

        class raw:
            @staticmethod
            def read(n, decode_content=True):
                return b""

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(lyrics.requests, "get", fake_get)
    with pytest.raises(lyrics.FetchError) as e:
        lyrics.fetch("https://genius.com/x")
    assert e.value.code == "domain_not_allowed"
    assert calls == ["https://genius.com/x"]
