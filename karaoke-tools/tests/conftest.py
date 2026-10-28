"""Shared fixtures. Every test runs against a fresh temp copy of the wiki
skeleton (tests/fixtures/wiki, a copy of the real SCHEMA.md and empty CSVs)
with WIKI_ROOT/SEED_ROOT/MEDIA_ROOT pointing into tmp_path. Network access
fails loudly."""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    shutil.copytree(FIXTURES / "wiki", root)
    seed = tmp_path / "seed"
    seed.mkdir()
    media = tmp_path / "media-tmp"
    media.mkdir()
    monkeypatch.setenv("WIKI_ROOT", str(root))
    monkeypatch.setenv("SEED_ROOT", str(seed))
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("LYRICS_DELAY_SEC", "0")
    monkeypatch.delenv("LYRICS_EXTRA_DOMAINS", raising=False)
    monkeypatch.delenv("GENIUS_ACCESS_TOKEN", raising=False)
    return root.resolve()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test tries to reach the network."""
    import requests
    import yt_dlp

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", boom)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
