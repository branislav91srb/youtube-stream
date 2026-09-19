import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.applog import log_event
from app.config import PORT, TEMPLATES_DIR, resolve_quality
from app.hls_proxy import proxy_hls
from app.session import (
    SESSION_COOKIE,
    SESSION_HEADER,
    is_valid_session_id,
    new_session_id,
    parse_cookie_header,
    session_cookie_header,
)
from app.stream import ensure_stream, get_stream_status
from app.youtube import validate_video_id

_PLAYER_TEMPLATE = None

# Quiet noisy polling in the default HTTP access logger.
_QUIET_PATH_PREFIXES = ("/status/",)


def _load_player_template():
    global _PLAYER_TEMPLATE
    if _PLAYER_TEMPLATE is None:
        path = os.path.join(TEMPLATES_DIR, "player.html")
        with open(path, encoding="utf-8") as f:
            _PLAYER_TEMPLATE = f.read()
    return _PLAYER_TEMPLATE


def render_player(video_id, session_id):
    html = _load_player_template()
    html = html.replace("{{VIDEO_ID}}", video_id)
    html = html.replace("{{SESSION_ID}}", session_id)
    return html


HOME_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>YouTube Stream</title>
</head>
<body>
    <h2>YouTube Stream Proxy</h2>
    <p>Use: /watch/YOUTUBE_VIDEO_ID</p>
    <p>Optional quality: /watch/YOUTUBE_VIDEO_ID?quality=720</p>
    <p>Allowed: 240, 360, 480, 720 (default), 1080, best</p>
    <p>HLS (same origin): /hls/YOUTUBE_VIDEO_ID/index.m3u8</p>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def client_ip(self):
        """Prefer Cloudflare / proxy headers when present."""
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = self.headers.get("X-Real-IP")
        if real:
            return real.strip()
        return self.client_address[0]

    def client_ua(self):
        return self.headers.get("User-Agent", "-")

    def client_country(self):
        """Cloudflare sets CF-IPCountry (e.g. RU, RS, XX)."""
        country = self.headers.get("CF-IPCountry")
        if country:
            return country.strip().upper()
        return "-"

    def client_session(self, query=None, body_sid=None):
        """
        Resolve session id from (in order):
        body, ?sid=, X-Session-Id, ysid cookie.
        """
        if is_valid_session_id(body_sid):
            return body_sid

        if query is not None:
            params = parse_qs(query) if isinstance(query, str) else query
            raw = (params.get("sid") or [None])[0]
            if is_valid_session_id(raw):
                return raw

        header = self.headers.get(SESSION_HEADER)
        if is_valid_session_id(header):
            return header.strip()

        return parse_cookie_header(
            self.headers.get("Cookie"),
            SESSION_COOKIE,
        )

    def ensure_session(self, query=None):
        """Return (session_id, is_new)."""
        existing = self.client_session(query=query)
        if existing:
            return existing, False
        return new_session_id(), True

    def log_message(self, format, *args):
        # Suppress chatty status polls; keep other HTTP lines.
        try:
            message = format % args
        except Exception:
            message = format
        if any(token in message for token in _QUIET_PATH_PREFIXES):
            return
        # Successful .ts segment GETs are very noisy.
        if '".ts' in message and " 200 " in message:
            return
        print(message)

    def _extra_headers(self, headers=None):
        out = list(headers or [])
        return out

    def send_html(self, status, html, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in self._extra_headers(extra_headers):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, text, extra_headers=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in self._extra_headers(extra_headers):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in self._extra_headers(extra_headers):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _parse_video_id(self, raw_id):
        try:
            return validate_video_id(raw_id)
        except ValueError:
            log_event(
                "access",
                level="warn",
                event="invalid_video_id",
                ip=self.client_ip(),
                country=self.client_country(),
                sid=self.client_session() or "-",
                video=raw_id,
                ua=self.client_ua(),
            )
            self.send_text(400, "Invalid YouTube video ID")
            return None

    def _parse_quality(self, query):
        params = parse_qs(query)
        raw = None
        if "quality" in params and params["quality"]:
            raw = params["quality"][0]
        elif "q" in params and params["q"]:
            raw = params["q"][0]
        try:
            return resolve_quality(raw)
        except ValueError as e:
            log_event(
                "access",
                level="warn",
                event="invalid_quality",
                ip=self.client_ip(),
                country=self.client_country(),
                sid=self.client_session(query=query) or "-",
                detail=str(e),
                ua=self.client_ua(),
            )
            self.send_text(400, str(e))
            return None

    def _handle_client_event(self, video_id):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 16_384:
            self.send_text(400, "Invalid body")
            return

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_text(400, "Invalid JSON")
            return

        if not isinstance(payload, dict):
            self.send_text(400, "Expected JSON object")
            return

        event = str(payload.get("event") or "unknown")[:64]
        level = str(payload.get("level") or "info")[:16]
        detail = payload.get("detail")
        if detail is not None:
            detail = str(detail)[:500]

        sid = self.client_session(body_sid=payload.get("sid"))

        log_event(
            "playback",
            level=level,
            event=event,
            ip=self.client_ip(),
            country=self.client_country(),
            sid=sid or "-",
            video=video_id,
            ua=self.client_ua(),
            detail=detail,
            fatal=payload.get("fatal"),
            error_type=payload.get("type"),
        )
        self.send_json(200, {"ok": True, "sid": sid})

    def do_POST(self):
        parsed = urlparse(self.path)
        match = re.match(r"^/events/([A-Za-z0-9_-]{11})$", parsed.path)
        if match:
            video_id = self._parse_video_id(match.group(1))
            if not video_id:
                return
            self._handle_client_event(video_id)
            return
        self.send_text(404, "Not found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        match = re.match(r"^/watch/([A-Za-z0-9_-]{11})$", path)
        if match:
            video_id = self._parse_video_id(match.group(1))
            if not video_id:
                return
            quality = self._parse_quality(parsed.query)
            if quality is None:
                return

            sid, is_new = self.ensure_session(query=parsed.query)
            extra = [("Set-Cookie", session_cookie_header(sid))]
            if is_new:
                extra.append((SESSION_HEADER, sid))

            log_event(
                "access",
                level="info",
                event="watch",
                ip=self.client_ip(),
                country=self.client_country(),
                sid=sid,
                video=video_id,
                quality=quality,
                ua=self.client_ua(),
                new_session=is_new,
            )

            # Soft-join if this quality is already publishing.
            # Quality changes restart automatically. Use ?restart=1 to
            # force a fresh publish (VOD from the beginning).
            params = parse_qs(parsed.query)
            force = (params.get("restart") or params.get("refresh") or [""])[0]
            force_restart = str(force).lower() in ("1", "true", "yes")
            ensure_stream(
                video_id,
                restart=force_restart,
                quality=quality,
            )
            self.send_html(
                200,
                render_player(video_id, sid),
                extra_headers=extra,
            )
            return

        match = re.match(r"^/hls/([A-Za-z0-9_-]{11})/(.+)$", path)
        if match:
            video_id = self._parse_video_id(match.group(1))
            if not video_id:
                return
            proxy_hls(
                self,
                video_id,
                match.group(2),
                parsed.query,
                session_id=self.client_session(query=parsed.query),
            )
            return

        match = re.match(r"^/status/([A-Za-z0-9_-]{11})$", path)
        if match:
            video_id = self._parse_video_id(match.group(1))
            if not video_id:
                return
            self.send_json(200, get_stream_status(video_id))
            return

        if path == "/":
            self.send_html(200, HOME_HTML)
            return

        log_event(
            "access",
            level="warn",
            event="not_found",
            ip=self.client_ip(),
            country=self.client_country(),
            sid=self.client_session(query=parsed.query) or "-",
            path=path,
            ua=self.client_ua(),
        )
        self.send_text(404, "Not found")


def make_server():
    return ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
