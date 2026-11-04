"""Telegram Bot API, send side only.

The Hermes gateway owns the bot's incoming updates. Calling getUpdates or
setting a webhook from here would steal them, so this module can only call
sendMessage, and only to TELEGRAM_CHAT_ID.

Buttons are a reply keyboard (BUTTON_MODE=reply): tapping a button sends its
text as an ordinary message, which Hermes already delivers to the agent.
Inline buttons don't work with Hermes: its callback_query handler silently
drops callback_data it doesn't recognise, so BUTTON_MODE=inline is refused.
"""

import re

import requests

from common import button_mode, fail, log, ok, telegram_chat_id, telegram_token

API = "https://api.telegram.org/bot{token}/{method}"
ALLOWED_METHODS = {"sendMessage"}
TIMEOUT = 15
# Telegram's practical ceiling for a keyboard is ~100 buttons. A chat shows one
# reply keyboard at a time (a newer one replaces it), so a page stays in one
# keyboard unless it would pass this many buttons.
MAX_BUTTONS = 100
PLACEHOLDER = "tap or type"


def _call(method: str, payload: dict) -> dict:
    if method not in ALLOWED_METHODS:  # never getUpdates / setWebhook
        return fail("not_allowed", f"Bot API method {method} is not allowed.")
    token = telegram_token()
    if not token:
        return fail("telegram_not_configured", "TELEGRAM_BOT_TOKEN is not set.")
    try:
        resp = requests.post(API.format(token=token, method=method), json=payload, timeout=TIMEOUT)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        msg = re.sub(r"bot\d+:[\w-]+", "bot<token>", str(e))  # never echo the token
        return fail("telegram_error", msg)
    if not data.get("ok"):
        return fail("telegram_error", data.get("description") or f"HTTP {resp.status_code}")
    return ok(message_id=(data.get("result") or {}).get("message_id"))


def send(text: str, keyboard: list[list[str]] | None = None, remove_keyboard: bool = False,
         chat_id: str | None = None) -> dict:
    """sendMessage to Neel's chat, optionally with a reply keyboard or its removal."""
    allowed = telegram_chat_id()
    if not allowed:
        return fail("telegram_not_configured", "TELEGRAM_CHAT_ID is not set.")
    if chat_id is not None and str(chat_id).strip() != allowed:
        return fail("chat_not_allowed", "Messages can only be sent to TELEGRAM_CHAT_ID.")
    if button_mode() != "reply" and (keyboard or remove_keyboard):
        return fail("button_mode_unsupported", "BUTTON_MODE=inline is not supported: Hermes "
                    "drops unknown inline-button taps. Use BUTTON_MODE=reply.")
    payload = {"chat_id": allowed, "text": text, "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"keyboard": [[{"text": b} for b in row] for row in keyboard],
                                   "resize_keyboard": True, "is_persistent": True,
                                   "input_field_placeholder": PLACEHOLDER}
    elif remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    return _call("sendMessage", payload)


def split_keyboard(rows: list[list[str]], footer: list[str]) -> list[list[list[str]]]:
    """Split item rows into keyboards under MAX_BUTTONS, each ending with the footer."""
    per = max(1, (MAX_BUTTONS - len(footer)) // max(1, max((len(r) for r in rows), default=1)))
    parts = [rows[i:i + per] for i in range(0, len(rows), per)] or [[]]
    return [part + [footer] for part in parts]
