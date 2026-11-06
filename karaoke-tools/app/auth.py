"""Telegram Mini App initData validation.

core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app:
data-check-string = every received field except hash, sorted by key, as
key=value lines joined by \n; secret = HMAC_SHA256(key="WebAppData",
msg=bot_token); valid when hex(HMAC_SHA256(key=secret, msg=data-check-string))
equals hash. On top of that, auth_date must be under 24 h old and the user must
be Neel (TELEGRAM_CHAT_ID).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SEC = 24 * 3600
MAX_CLOCK_SKEW_SEC = 300
MAX_INIT_DATA_LEN = 4096


class AuthError(Exception):
    pass


def sign(fields: dict, bot_token: str) -> str:
    """hash for a set of initData fields (also used by the tests)."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def validate(init_data: str, bot_token: str, allowed_user_id: str, now: float | None = None,
             max_age: int = MAX_AGE_SEC) -> dict:
    """The verified Telegram user dict, or AuthError."""
    if not bot_token or not allowed_user_id:
        raise AuthError("server not configured")
    if not init_data or len(init_data) > MAX_INIT_DATA_LEN:
        raise AuthError("missing init data")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("malformed init data") from None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise AuthError("duplicate field")
    got = fields.pop("hash", "")
    if not got or not hmac.compare_digest(sign(fields, bot_token), got.lower()):
        raise AuthError("bad signature")
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        raise AuthError("bad auth_date") from None
    now = time.time() if now is None else now
    if now - auth_date > max_age or auth_date - now > MAX_CLOCK_SKEW_SEC:
        raise AuthError("expired")
    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        raise AuthError("bad user") from None
    if not isinstance(user, dict) or str(user.get("id")) != str(allowed_user_id).strip():
        raise AuthError("user not allowed")
    return user
