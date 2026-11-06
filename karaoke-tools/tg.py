"""Telegram Bot API, send side only.

The Hermes gateway owns the bot's incoming updates. Calling getUpdates or
setting a webhook from here would steal them, so this module can only call
sendMessage, sendPoll, stopPoll and setChatMenuButton, and only for
TELEGRAM_CHAT_ID. Poll votes
are read by closing the poll (stopPoll returns the final counts), not from
updates.

Buttons are a reply keyboard (BUTTON_MODE=reply): tapping a button sends its
text as an ordinary message, which Hermes already delivers to the agent.
Inline buttons don't work with Hermes: its callback_query handler silently
drops callback_data it doesn't recognise, so BUTTON_MODE=inline is refused.
The one inline button used is a web_app button (open_app): tapping it opens
the Mini App inside Telegram and sends no update to the bot at all.
"""

import re

import requests

from common import button_mode, fail, log, ok, telegram_chat_id, telegram_token

API = "https://api.telegram.org/bot{token}/{method}"
ALLOWED_METHODS = {"sendMessage", "sendPoll", "stopPoll", "setChatMenuButton"}
# Bot API 10.3 sendPoll limits (core.telegram.org/bots/api, checked 2026-11-05).
MAX_POLL_OPTIONS = 12
MAX_OPTION_LEN = 100
MAX_QUESTION_LEN = 300
TIMEOUT = 15
# Telegram's practical ceiling for a keyboard is ~100 buttons. A chat shows one
# reply keyboard at a time (a newer one replaces it), so a page stays in one
# keyboard unless it would pass this many buttons.
MAX_BUTTONS = 100
PLACEHOLDER = "tap or type"


def _call(method: str, payload: dict) -> dict:  # noqa: C901
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
    result = data.get("result") or {}
    return ok(message_id=result.get("message_id"), result=result)


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


def _https(url: str) -> bool:
    return (url or "").lower().startswith("https://")


def send_web_app_button(text: str, button_text: str, url: str) -> dict:
    """sendMessage with one inline web_app button that opens the Mini App at url
    (Telegram requires https). No callback_data, so nothing reaches Hermes."""
    allowed = telegram_chat_id()
    if not allowed:
        return fail("telegram_not_configured", "TELEGRAM_CHAT_ID is not set.")
    if not _https(url):
        return fail("invalid_input", "A web_app button needs an https:// URL (APP_URL).")
    payload = {"chat_id": allowed, "text": text, "disable_web_page_preview": True,
               "reply_markup": {"inline_keyboard": [[{"text": button_text, "web_app": {"url": url}}]]}}
    return _call("sendMessage", payload)


def set_menu_button(text: str, url: str) -> dict:
    """The chat's menu button (bottom-left) opens the Mini App at url."""
    allowed = telegram_chat_id()
    if not allowed:
        return fail("telegram_not_configured", "TELEGRAM_CHAT_ID is not set.")
    if not _https(url):
        return fail("invalid_input", "The menu button needs an https:// URL (APP_URL).")
    return _call("setChatMenuButton", {"chat_id": allowed, "menu_button": {
        "type": "web_app", "text": text, "web_app": {"url": url}}})


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def send_poll(question: str, options: list[str]) -> dict:
    """A multi-select, anonymous poll in Neel's chat. Returns {message_id}.

    No reply_markup: Telegram accepts ReplyKeyboardRemove on sendPoll, but a
    poll sent with it can't be closed later ("poll can't be stopped"), and
    closing is how votes are read. Remove an old keyboard with a separate
    message instead."""
    allowed = telegram_chat_id()
    if not allowed:
        return fail("telegram_not_configured", "TELEGRAM_CHAT_ID is not set.")
    if not 2 <= len(options) <= MAX_POLL_OPTIONS:
        return fail("invalid_input", f"A poll needs 2-{MAX_POLL_OPTIONS} options.")
    payload = {"chat_id": allowed, "question": clip(question, MAX_QUESTION_LEN),
               "options": [{"text": clip(o, MAX_OPTION_LEN)} for o in options],
               "is_anonymous": True, "allows_multiple_answers": True}
    return _call("sendPoll", payload)


def stop_poll(message_id: int) -> dict:
    """Close a poll the bot sent; returns {poll} with voter_count per option."""
    allowed = telegram_chat_id()
    if not allowed:
        return fail("telegram_not_configured", "TELEGRAM_CHAT_ID is not set.")
    r = _call("stopPoll", {"chat_id": allowed, "message_id": message_id})
    return ok(poll=r["result"]) if r["success"] else r


def split_keyboard(rows: list[list[str]], footer: list[str]) -> list[list[list[str]]]:
    """Split item rows into keyboards under MAX_BUTTONS, each ending with the footer."""
    per = max(1, (MAX_BUTTONS - len(footer)) // max(1, max((len(r) for r in rows), default=1)))
    parts = [rows[i:i + per] for i in range(0, len(rows), per)] or [[]]
    return [part + [footer] for part in parts]
