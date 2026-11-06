"""Env for the app server. launchd starts it with only non-secret settings;
the bot token is read from the Hermes profile's .env (never copied anywhere).
Only known keys are taken from env files, and the process env always wins."""

import os
from pathlib import Path

DEFAULT_ENV_FILES = "~/.hermes/profiles/karaoke-gayak/.env"
KEYS = {"WIKI_ROOT", "STATE_ROOT", "MEDIA_ROOT", "SEED_ROOT", "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID", "APP_URL", "YTDLP_JS_RUNTIME", "FFMPEG_PATH",
        "PREFERRED_CHANNEL_URL", "GENIUS_ACCESS_TOKEN", "LYRICS_EXTRA_DOMAINS",
        "MAX_DURATION_SEC", "LYRICS_DELAY_SEC"}
# Names the Hermes profile uses for the same thing.
ALIASES = {"TELEGRAM_HOME_CHANNEL": "TELEGRAM_CHAT_ID"}


def parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.removeprefix("export ").partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def load_env_files(spec: str | None = None) -> list[str]:
    """Apply known keys from the env files; returns the keys set (not values)."""
    spec = os.environ.get("KARAOKE_APP_ENV_FILES", DEFAULT_ENV_FILES) if spec is None else spec
    applied = []
    for raw in [s for s in spec.split(":") if s.strip()]:
        try:
            values = parse_env(Path(raw.strip()).expanduser().read_text(encoding="utf-8"))
        except OSError:
            continue
        for k, v in values.items():
            k = ALIASES.get(k, k)
            if k in KEYS and v and not os.environ.get(k, "").strip():
                os.environ[k] = v
                applied.append(k)
    return applied
