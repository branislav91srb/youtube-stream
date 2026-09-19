import re
import subprocess

from app.config import DEFAULT_QUALITY, QUALITY_PRESETS, resolve_quality


def validate_video_id(video_id):
    if not re.match(r"^[A-Za-z0-9_-]{11}$", video_id):
        raise ValueError("Invalid YouTube video ID")
    return video_id


def run_ytdlp(args):
    """Run yt-dlp with shared flags; raise RuntimeError on failure."""
    command = ["yt-dlp", "--js-runtimes", "node", *args]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "yt-dlp failed").strip()
        raise RuntimeError(err)
    return result


def get_video_meta(video_id):
    """Return (live_status, duration_seconds_or_None)."""
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    result = run_ytdlp(
        [
            "--print",
            "%(live_status)s",
            "--print",
            "%(duration)s",
            youtube_url,
        ]
    )

    lines = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
    live_status = lines[0] if lines else "not_live"
    duration = None
    if len(lines) >= 2 and lines[1] not in ("NA", "None", ""):
        try:
            duration = float(lines[1])
        except ValueError:
            duration = None

    return live_status, duration


def get_youtube_urls(video_id, quality=DEFAULT_QUALITY):
    """
    Resolve one or two direct media URLs via yt-dlp -g.

    Returns a list of URLs: one for muxed streams, two for separate A/V.
    """
    quality = resolve_quality(quality)
    fmt = QUALITY_PRESETS[quality]["format"]
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[{video_id}] Resolving YouTube URLs (quality={quality})...")

    result = run_ytdlp(["-f", fmt, "-g", youtube_url])

    urls = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if not urls:
        raise RuntimeError("yt-dlp returned no media URLs")

    if len(urls) == 1:
        print(f"[{video_id}] Muxed URL resolved")
    else:
        print(f"[{video_id}] Video + audio URLs resolved ({len(urls)})")

    return urls
