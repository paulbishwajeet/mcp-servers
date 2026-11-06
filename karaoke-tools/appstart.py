"""Opening the Mini App from the chat: start codes and the open_app tool.

The agent can't open a Mini App by itself; Telegram only opens one when the
user taps a button. open_app sends one short line with a web_app button
"🎵 Open" whose URL carries a start code. The code is a short URL-safe ID that
points at the stored request (view, seed, url) in STATE_ROOT, so the URL
stays short and carries no song text. Codes expire after 7 days.
"""

import json
import re
import secrets
import time

import catalog
import locks
import tg
from common import app_url, fail, is_youtube_url, ok, state_root

CODE_TTL = 7 * 24 * 3600
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,12}$")
VIEWS = ("home", "similar")
MAX_SEED_LEN = 200


def _path():
    return state_root() / "app_start_codes.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def new_code(view: str, seed: str = "", url: str = "") -> str:
    """Store a start request; returns its code (11 URL-safe characters)."""
    now = time.time()
    with locks.LOCK:
        codes = {c: v for c, v in _load().items() if now - v.get("at", 0) < CODE_TTL}
        code = secrets.token_urlsafe(8)
        while code in codes:
            code = secrets.token_urlsafe(8)
        codes[code] = {"view": view, "seed": seed, "url": url, "at": now}
        _path().parent.mkdir(parents=True, exist_ok=True)
        catalog.atomic_write(_path(), json.dumps(codes, ensure_ascii=False))
    return code


def get_code(code: str) -> dict | None:
    """The stored request for a code, or None when unknown or expired."""
    if not CODE_RE.match(code or ""):
        return None
    hit = _load().get(code)
    if not hit or time.time() - hit.get("at", 0) >= CODE_TTL:
        return None
    return {"view": hit["view"], "seed": hit.get("seed", ""), "url": hit.get("url", "")}


def open_app(view: str = "similar", seed: str = "", url: str = "") -> dict:
    view = (view or "").strip().lower()
    seed, url = (seed or "").strip(), (url or "").strip()
    if view not in VIEWS:
        return fail("invalid_input", f"view must be one of {', '.join(VIEWS)}.")
    if view == "similar" and not (seed or url):
        return fail("invalid_input", "view=similar needs a seed song or a YouTube url.")
    if url and not is_youtube_url(url):
        return fail("url_not_allowed", "url must be a YouTube link.")
    if len(seed) > MAX_SEED_LEN:
        return fail("invalid_input", f"seed is longer than {MAX_SEED_LEN} characters.")
    base = app_url()
    if not base.startswith("https://"):
        return fail("app_not_configured", "APP_URL is not set to the app's https:// address.")
    code = new_code(view, seed, url)
    text = f"Similar to {tg.clip(seed or url, 80)}" if view == "similar" else "KaraokeGayak"
    r = tg.send_web_app_button(text, "🎵 Open", f"{base}/?start={code}")
    if not r["success"]:
        return r | {"sent": False, "code": code}
    return ok(sent=True, code=code)
