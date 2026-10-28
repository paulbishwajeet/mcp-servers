"""Shared helpers: env config, result shape, path safety, URL allowlists, logging.

Config is read from the environment on every call (not cached at import),
so tests can monkeypatch.setenv and the same process sees the change.
stdout is the MCP protocol channel: everything here logs to stderr.
"""

import functools
import os
import shutil
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_WIKI_ROOT = "~/.hermes/profiles/karaoke-gayak/karaoke-gayak-wiki"
DEFAULT_MEDIA_ROOT = "~/.hermes/profiles/karaoke-gayak/media-tmp"
DEFAULT_CHANNEL_URL = "https://www.youtube.com/@AnilChauhankaraoke"

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com",
                           "music.youtube.com", "youtu.be"})


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- config -----------------------------------------------------------------

def _path_env(name: str, default: str | None) -> Path | None:
    value = os.environ.get(name, "").strip() or default
    return Path(value).expanduser().resolve() if value else None


def wiki_root() -> Path:
    return _path_env("WIKI_ROOT", DEFAULT_WIKI_ROOT)


def seed_root() -> Path | None:
    return _path_env("SEED_ROOT", None)


def media_root() -> Path:
    return _path_env("MEDIA_ROOT", DEFAULT_MEDIA_ROOT)


def preferred_channel_url() -> str:
    return os.environ.get("PREFERRED_CHANNEL_URL", "").strip() or DEFAULT_CHANNEL_URL


def max_duration_sec() -> int:
    try:
        return int(os.environ.get("MAX_DURATION_SEC", "900"))
    except ValueError:
        return 900


def ffmpeg_path() -> str | None:
    """Absolute ffmpeg path, or None. Hermes runs under launchd without
    /usr/local/bin on PATH, so FFMPEG_PATH should be a full path."""
    return shutil.which(os.environ.get("FFMPEG_PATH", "").strip() or "ffmpeg")


def js_runtime() -> tuple[str, str] | None:
    """(name, path) of the JS runtime yt-dlp uses for YouTube's challenges.

    YTDLP_JS_RUNTIME is `node:/full/path/to/node` or `deno:/path`; when unset
    we look for deno, then node, on PATH.
    """
    spec = os.environ.get("YTDLP_JS_RUNTIME", "").strip()
    if spec:
        name, _, path = spec.partition(":")
        found = shutil.which(path or name)
        return (name.strip().lower(), found) if found else None
    for name in ("deno", "node"):
        found = shutil.which(name)
        if found:
            return name, found
    return None


def genius_token() -> str:
    return os.environ.get("GENIUS_ACCESS_TOKEN", "").strip()


def lyrics_extra_domains() -> list[str]:
    raw = os.environ.get("LYRICS_EXTRA_DOMAINS", "")
    return [d.strip().lower().rstrip(".") for d in raw.split(",") if d.strip()]


def lyrics_delay_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("LYRICS_DELAY_SEC", "2")))
    except ValueError:
        return 2.0


# --- result shape -------------------------------------------------------------

def ok(**fields) -> dict:
    return {"success": True, "error_code": "", "error": ""} | fields


def fail(code: str, message: str, **fields) -> dict:
    return {"success": False, "error_code": code, "error": str(message)[:500]} | fields


def safe_tool(fn):
    """Never let an exception reach the MCP layer: turn it into a fail() result."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PathError as e:
            return fail("path_not_allowed", str(e))
        except SchemaError as e:
            return fail("schema_mismatch", str(e))
        except Exception as e:  # noqa: BLE001 -- last-resort guard
            log(f"[{fn.__name__}] unexpected error: {e!r}\n{traceback.format_exc()}")
            return fail("internal_error", f"Unexpected error: {e}")

    return wrapper


# --- path safety --------------------------------------------------------------

class PathError(ValueError):
    pass


class SchemaError(ValueError):
    """A wiki file is missing or doesn't match SCHEMA.md."""


def resolve_inside(path: str | Path, roots: list[Path], base: Path | None = None) -> Path:
    """Resolve `path` (absolute, or relative to `base`) and require it to sit
    inside one of `roots` after symlinks are resolved.

    Rejects any `..` component outright, even one that would stay inside.
    """
    raw = str(path or "").strip()
    if not raw:
        raise PathError("Empty path.")
    if "\x00" in raw:
        raise PathError("Path contains a NUL byte.")
    p = Path(raw).expanduser()
    if ".." in p.parts:
        raise PathError("Path must not contain '..'.")
    if not p.is_absolute():
        if base is None:
            raise PathError("Path must be absolute.")
        p = base / p
    resolved = p.resolve()
    for root in roots:
        if root is None:
            continue
        root = root.resolve()
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise PathError("Path resolves outside the allowed folders.")


# --- URL checks -----------------------------------------------------------------

def url_host(url: str) -> str:
    """Lowercased host of an http(s) URL, or '' if it isn't one."""
    try:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https") or not host:
        return ""
    return host


def is_youtube_url(url: str) -> bool:
    return url_host(url) in YOUTUBE_HOSTS
