import re
from http.client import HTTPConnection
from urllib.parse import urlencode, urljoin, urlparse

from app.applog import log_event
from app.config import (
    MEDIAMTX_HLS_CDN_SECRET,
    MEDIAMTX_HLS_PORT,
    MEDIAMTX_HOST,
)


def rewrite_hls_location(location, video_id):
    """Map MediaMTX Location (/youtube/<id>/...) to /hls/<id>/..."""
    if not location:
        return location

    parsed = urlparse(location)
    path = parsed.path or location

    prefix = f"/youtube/{video_id}/"
    if path.startswith(prefix):
        rewritten = f"/hls/{video_id}/" + path[len(prefix):]
        if parsed.query:
            rewritten += "?" + parsed.query
        return rewritten

    if path.startswith("/youtube/"):
        rewritten = "/hls/" + path[len("/youtube/"):]
        if parsed.query:
            rewritten += "?" + parsed.query
        return rewritten

    return location


def fetch_hls_upstream(video_id, filename, query="", range_header=None):
    """
    GET from MediaMTX, following cookieCheck redirects.

    Secure partitioned cookies do not work over plain HTTP, so we do NOT
    echo cookieCheck cookies back. That forces query-param session mode
    (?session=...), which works through this proxy on LAN and Cloudflare.

    On 401 (stale MediaMTX session after republish), retry once without
    the session query so the client can obtain a fresh one.
    """
    upstream_path = f"/youtube/{video_id}/{filename}"
    if query:
        upstream_path += "?" + query

    headers = {
        "Authorization": f"Bearer {MEDIAMTX_HLS_CDN_SECRET}",
    }
    if range_header:
        headers["Range"] = range_header

    conn = None
    upstream = None
    retried_without_session = False

    for _ in range(5):
        if conn is not None:
            try:
                upstream.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        conn = HTTPConnection(
            MEDIAMTX_HOST,
            int(MEDIAMTX_HLS_PORT),
            timeout=30,
        )
        conn.request("GET", upstream_path, headers=headers)
        upstream = conn.getresponse()

        if (
            upstream.status == 401
            and not retried_without_session
            and "session=" in upstream_path
        ):
            upstream.read()
            # Drop session=... and retry for a fresh MediaMTX session.
            parsed = urlparse(upstream_path)
            q = [
                (k, v)
                for k, v in [
                    part.split("=", 1) if "=" in part else (part, "")
                    for part in (parsed.query or "").split("&")
                    if part
                ]
                if k != "session"
            ]
            upstream_path = parsed.path
            if q:
                upstream_path += "?" + urlencode(q)
            retried_without_session = True
            headers.pop("Range", None)
            continue

        if upstream.status not in (301, 302, 303, 307, 308):
            return conn, upstream

        location = upstream.getheader("Location")
        if not location:
            return conn, upstream

        upstream.read()

        absolute = urljoin(
            f"http://{MEDIAMTX_HOST}:{MEDIAMTX_HLS_PORT}{upstream_path}",
            location,
        )
        parsed = urlparse(absolute)
        upstream_path = parsed.path
        if parsed.query:
            upstream_path += "?" + parsed.query

        if upstream.status in (301, 302, 303):
            headers.pop("Range", None)

    return conn, upstream


def _client_meta(handler, session_id=None):
    if hasattr(handler, "client_ip"):
        client_ip = handler.client_ip()
    else:
        client_ip = handler.address_string()

    if hasattr(handler, "client_country"):
        country = handler.client_country()
    else:
        country = "-"

    if not session_id and hasattr(handler, "client_session"):
        session_id = handler.client_session()

    return client_ip, country, session_id or "-"


def proxy_hls(handler, video_id, filename, query="", session_id=None):
    """
    Stream MediaMTX HLS through the app origin so only port 8080 is public.
    """
    client_ip, country, sid = _client_meta(handler, session_id)

    if not re.match(r"^[A-Za-z0-9._-]+$", filename):
        log_event(
            "hls",
            level="warn",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=400,
            error="invalid_path",
        )
        handler.send_text(400, "Invalid HLS path")
        return

    try:
        conn, upstream = fetch_hls_upstream(
            video_id,
            filename,
            query,
            range_header=handler.headers.get("Range"),
        )
    except Exception as e:
        log_event(
            "hls",
            level="error",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=502,
            error=f"upstream:{e}",
        )
        handler.send_text(502, f"HLS upstream error: {e}")
        return

    try:
        body = upstream.read()
    except Exception as e:
        upstream.close()
        conn.close()
        log_event(
            "hls",
            level="error",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=502,
            error=f"read:{e}",
        )
        handler.send_text(502, f"HLS read error: {e}")
        return

    status = upstream.status

    # Log playlist problems and any non-success segment responses.
    if filename.endswith(".m3u8") and status != 200:
        log_event(
            "hls",
            level="warn",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=status,
            bytes=len(body),
        )
    elif status >= 400:
        log_event(
            "hls",
            level="warn",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=status,
            bytes=len(body),
        )

    if status in (301, 302, 303, 307, 308):
        location = rewrite_hls_location(
            upstream.getheader("Location"),
            video_id,
        )
        handler.send_response(status)
        if location:
            handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if body:
            handler.wfile.write(body)
        upstream.close()
        conn.close()
        return

    handler.send_response(status)

    content_type = upstream.getheader("Content-Type")
    if not content_type:
        if filename.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
        elif filename.endswith(".ts"):
            content_type = "video/mp2t"
        else:
            content_type = "application/octet-stream"

    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))

    content_range = upstream.getheader("Content-Range")
    if content_range:
        handler.send_header("Content-Range", content_range)

    accept_ranges = upstream.getheader("Accept-Ranges")
    if accept_ranges:
        handler.send_header("Accept-Ranges", accept_ranges)

    if filename.endswith(".m3u8"):
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("CDN-Cache-Control", "no-store")
    else:
        handler.send_header("Cache-Control", "public, max-age=1")

    handler.end_headers()

    try:
        if body:
            handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        log_event(
            "hls",
            level="warn",
            ip=client_ip,
            country=country,
            sid=sid,
            video=video_id,
            file=filename,
            status=status,
            error="client_disconnected",
        )
    finally:
        upstream.close()
        conn.close()
