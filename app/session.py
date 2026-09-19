"""Client session IDs for correlating access / HLS / playback logs."""

import re
import secrets

SESSION_COOKIE = "ysid"
SESSION_HEADER = "X-Session-Id"
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,24}$")


def new_session_id():
    # Short, URL/cookie-safe, easy to grep in docker logs.
    return secrets.token_urlsafe(9)[:12]


def is_valid_session_id(value):
    return bool(value and _SESSION_RE.match(value))


def parse_cookie_header(cookie_header, name):
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            value = value.strip()
            return value if is_valid_session_id(value) else None
    return None


def session_cookie_header(session_id, max_age=86_400):
    # Not HttpOnly so the player can also read it if needed.
    # SameSite=Lax works for normal navigation + same-origin fetch.
    return (
        f"{SESSION_COOKIE}={session_id}; Path=/; "
        f"Max-Age={max_age}; SameSite=Lax"
    )
