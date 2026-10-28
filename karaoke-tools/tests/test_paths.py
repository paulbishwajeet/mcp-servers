"""Path safety: '..' and symlink escapes are rejected for audio, wiki and cleanup."""

import os

import pytest

import audio
import catalog
import youtube
from common import PathError, resolve_inside


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.mp3").write_bytes(b"ID3" + b"\0" * 2000)
    (d / "SCHEMA.md").write_text("evil")
    return d


@pytest.mark.parametrize("p", ["../outside/secret.mp3", "sub/../../outside/secret.mp3",
                               "/etc/passwd", "~/x.mp3", ""])
def test_audio_traversal_rejected(wiki, outside, p):
    r = audio.analyze_audio(p)
    assert not r["success"]
    assert r["error_code"] in ("path_not_allowed", "not_found")
    if ".." in p or p.startswith("/etc") or not p:
        assert r["error_code"] == "path_not_allowed"


def test_audio_symlink_escape_rejected(wiki, outside, tmp_path):
    seed = tmp_path / "seed"
    os.symlink(outside / "secret.mp3", seed / "link.mp3")
    os.symlink(outside, seed / "linkdir")
    assert audio.analyze_audio("link.mp3")["error_code"] == "path_not_allowed"
    assert audio.analyze_audio("linkdir/secret.mp3")["error_code"] == "path_not_allowed"
    assert audio.scan_folder("linkdir")["error_code"] == "path_not_allowed"
    assert audio.scan_folder()["total"] == 0  # symlinks out are skipped, not followed


def test_audio_media_root_allowed_absolute(wiki, tmp_path):
    media = tmp_path / "media-tmp"
    (media / "job").mkdir()
    f = media / "job" / "audio.txt"
    f.write_text("x")
    assert audio.analyze_audio(str(f))["error_code"] == "unsupported_format"  # passed path check


def test_scan_traversal(wiki):
    assert audio.scan_folder("../")["error_code"] == "path_not_allowed"


def test_wiki_paths(wiki, outside):
    with pytest.raises(PathError):
        catalog.wiki_path("../outside/SCHEMA.md")
    with pytest.raises(PathError):
        catalog.wiki_path("/etc/passwd")
    os.symlink(outside, wiki / "events" / "evil")
    with pytest.raises(PathError):
        catalog.wiki_path("events/evil/SCHEMA.md")
    assert catalog.wiki_path("catalog/songs.csv") == wiki / "catalog/songs.csv"


def test_wiki_file_symlinked_out_is_refused(wiki, outside):
    songs = wiki / "catalog/songs.csv"
    songs.unlink()
    os.symlink(outside / "SCHEMA.md", songs)
    from server import TOOLS
    r = TOOLS["catalog_upsert"](row={"title": "X"})
    assert r["error_code"] == "path_not_allowed"
    assert (outside / "SCHEMA.md").read_text() == "evil"


def test_lyrics_symlink_escape(wiki, outside):
    catalog.catalog_upsert({"title": "X", "song_id": "x"})
    os.symlink(outside / "SCHEMA.md", wiki / "lyrics" / "x.txt")
    from server import TOOLS
    r = TOOLS["lyrics_save"](song_id="x", text="a\nb", source="neel")
    assert r["error_code"] == "path_not_allowed"
    assert (outside / "SCHEMA.md").read_text() == "evil"


def test_event_symlink_escape(wiki, outside):
    os.symlink(outside / "SCHEMA.md", wiki / "events" / "2026-01-01-x.md")
    assert catalog.event_apply("2026-01-01-x.md")["error_code"] == "path_not_allowed"


@pytest.mark.parametrize("job_id", ["../x", "20261027-120000-zzzzzzzz", "", "a/b",
                                    "20261027-120000-abcdef01/..", None, 5])
def test_cleanup_rejects_bad_ids(wiki, job_id):
    assert youtube.cleanup_job(job_id)["success"] is False


def test_cleanup_symlinked_job_dir_not_followed(wiki, tmp_path, outside):
    media = tmp_path / "media-tmp"
    jid = "20261027-120000-abcdef01"
    os.symlink(outside, media / jid)
    r = youtube.cleanup_job(jid)
    assert r["error_code"] == "path_not_allowed"
    assert (outside / "secret.mp3").exists()


def test_cleanup_deletes_job(wiki, tmp_path):
    jid = "20261027-120000-abcdef01"
    d = tmp_path / "media-tmp" / jid
    d.mkdir()
    (d / "audio.mp3").write_bytes(b"x")
    assert youtube.cleanup_job(jid)["success"]
    assert not d.exists()
    assert youtube.cleanup_job(jid)["note"] == "already gone"


def test_resolve_inside_basics(tmp_path):
    assert resolve_inside("a/b", [tmp_path], base=tmp_path) == (tmp_path / "a/b").resolve()
    with pytest.raises(PathError):
        resolve_inside("a/b", [tmp_path])  # relative without base
    with pytest.raises(PathError):
        resolve_inside("x\x00y", [tmp_path], base=tmp_path)
