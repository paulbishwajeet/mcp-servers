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
    """Records Bot API calls instead of sending them. stopPoll answers from
    `votes` (message_id -> option indexes ticked) or `stop_errors`."""

    def __init__(self):
        self.calls = []
        self.votes = {}
        self.stop_errors = {}
        self.polls = {}  # message_id -> sendPoll payload

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        mid = len(self.calls)
        if method == "sendPoll":
            self.polls[mid] = json
            body = {"ok": True, "result": {"message_id": mid}}
        elif method == "stopPoll":
            m = json["message_id"]
            if "reply_markup" in self.polls.get(m, {}):  # like the real API (seen 2026-11-05)
                body = {"ok": False, "description": "Bad Request: poll can't be stopped"}
            elif m in self.stop_errors:
                body = {"ok": False, "description": self.stop_errors[m]}
            else:
                opts = self.polls.get(m, {}).get("options", [])
                body = {"ok": True, "result": {"id": f"p{m}", "is_closed": True, "options": [
                    {"text": o["text"], "voter_count": 1 if i in self.votes.get(m, ()) else 0}
                    for i, o in enumerate(opts)]}}
        elif method == "setChatMenuButton":
            body = {"ok": True, "result": True}  # like the real API
        else:
            body = {"ok": True, "result": {"message_id": mid}}

        class R:
            status_code = 200

            @staticmethod
            def json():
                return body
        return R()

    @property
    def messages(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    @property
    def sent_polls(self):
        return [(i + 1, p) for i, (m, p) in enumerate(self.calls) if m == "sendPoll"]


@pytest.fixture
def telegram(monkeypatch):
    import tg
    fake = FakeTelegram()
    monkeypatch.setattr(tg.requests, "post", fake.post)
    return fake
