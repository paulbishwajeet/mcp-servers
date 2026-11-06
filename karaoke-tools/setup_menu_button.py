#!/usr/bin/env python3
"""Set the bot's menu button (bottom-left of Neel's chat) to "🎵 KaraokeGayak",
opening the Mini App's home at APP_URL. Run once, and again if APP_URL changes:

    venv/bin/python setup_menu_button.py https://<machine>.<tailnet>.ts.net

The URL argument is optional when APP_URL is set in the environment.

Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (or TELEGRAM_HOME_CHANNEL) and APP_URL
from the environment or the env files the app server uses (app/config.py); the
token is never printed. Calls setChatMenuButton only (never getUpdates or a webhook).
"""

import sys

from app.config import load_env_files

import tg
from common import app_url


def main() -> int:
    load_env_files()
    url = sys.argv[1].strip().rstrip("/") if len(sys.argv) > 1 else app_url()
    if not url.startswith("https://"):
        print("Give the app's https:// address (Tailscale Funnel): "
              "setup_menu_button.py https://<machine>.<tailnet>.ts.net", file=sys.stderr)
        return 1
    r = tg.set_menu_button("🎵 KaraokeGayak", url + "/")
    print("menu button set" if r["success"] else f"failed: {r['error_code']}: {r['error']}")
    return 0 if r["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
