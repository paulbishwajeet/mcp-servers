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
    monkeypatch.setenv("STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    monkeypatch.setenv("BUTTON_MODE", "reply")
    return root.resolve()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test tries to reach the network."""
    import requests
    import yt_dlp

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", boom)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTelegram:
    """Records Bot API calls instead of sending them."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method == "setChatMenuButton":
            body = {"ok": True, "result": True}  # like the real API
        else:
            body = {"ok": True, "result": {"message_id": len(self.calls)}}

        class R:
            status_code = 200

            @staticmethod
            def json():
                return body
        return R()

    @property
    def messages(self):
        return [p for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
def telegram(monkeypatch):
    import tg
    fake = FakeTelegram()
    monkeypatch.setattr(tg.requests, "post", fake.post)
    return fake
