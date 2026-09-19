"""Structured application logs (easy to grep in docker logs)."""

from datetime import datetime, timezone


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(value):
    if value is None:
        return "-"
    text = str(value).replace("\n", " ").replace("\r", " ")
    if " " in text or "=" in text or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def log_event(kind, **fields):
    """
    Emit one structured line, e.g.:
    [access] ts=... ip=1.2.3.4 video=abc quality=720 ua="Mozilla/5.0"
    """
    parts = [f"[{kind}]", f"ts={_ts()}"]
    for key, value in fields.items():
        parts.append(f"{key}={_fmt(value)}")
    print(" ".join(parts), flush=True)
