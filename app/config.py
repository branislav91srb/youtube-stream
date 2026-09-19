import os

VERSION = "v0.0.18"

PORT = int(os.environ.get("PORT", "8080"))

MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_RTSP_PORT = os.environ.get("MEDIAMTX_RTSP_PORT", "8554")
MEDIAMTX_HLS_PORT = os.environ.get("MEDIAMTX_HLS_PORT", "8888")

# Disables MediaMTX HLS cookie-check 302 when sent as Bearer.
# Must match hlsCDNSecret in mediamtx.yml.
MEDIAMTX_HLS_CDN_SECRET = os.environ.get(
    "MEDIAMTX_HLS_CDN_SECRET",
    "yt-restream-hls-proxy",
)

# Browser-like headers help FFmpeg fetch googlevideo URLs.
FFMPEG_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FFMPEG_HEADERS = (
    f"User-Agent: {FFMPEG_USER_AGENT}\r\n"
    "Referer: https://www.youtube.com/\r\n"
)

# Default when ?quality= is omitted.
DEFAULT_QUALITY = "720"

# quality query → yt-dlp format + FFmpeg encode targets.
QUALITY_PRESETS = {
    "240": {
        "format": (
            "bestvideo[height<=240]+bestaudio/"
            "best[height<=240]/91/best"
        ),
        "height": 240,
        "video_bitrate": "400k",
        "maxrate": "500k",
        "bufsize": "1000k",
        "audio_bitrate": "64k",
    },
    "360": {
        "format": (
            "bestvideo[height<=360]+bestaudio/"
            "best[height<=360]/93/best"
        ),
        "height": 360,
        "video_bitrate": "800k",
        "maxrate": "1000k",
        "bufsize": "2000k",
        "audio_bitrate": "96k",
    },
    "480": {
        "format": (
            "bestvideo[height<=480]+bestaudio/"
            "best[height<=480]/94/93/best"
        ),
        "height": 480,
        "video_bitrate": "1500k",
        "maxrate": "1800k",
        "bufsize": "3500k",
        "audio_bitrate": "128k",
    },
    "720": {
        "format": (
            "bestvideo[height<=720]+bestaudio/"
            "232+234/95/94/93/best[height<=720]/best"
        ),
        "height": 720,
        "video_bitrate": "3000k",
        "maxrate": "3500k",
        "bufsize": "7000k",
        "audio_bitrate": "128k",
    },
    "1080": {
        "format": (
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080]/best"
        ),
        "height": 1080,
        "video_bitrate": "5000k",
        "maxrate": "5500k",
        "bufsize": "10000k",
        "audio_bitrate": "160k",
    },
    "best": {
        "format": "bestvideo+bestaudio/best",
        "height": None,
        "video_bitrate": "6000k",
        "maxrate": "7000k",
        "bufsize": "14000k",
        "audio_bitrate": "192k",
    },
}

# Project paths (templates live next to the package root).
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")


def resolve_quality(raw):
    """
    Normalize a quality query value to a preset key.
    Accepts 360, 480, 720, 1080, best (case-insensitive).
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_QUALITY
    key = str(raw).strip().lower()
    # Allow values like 720p
    if key.endswith("p") and key[:-1].isdigit():
        key = key[:-1]
    if key not in QUALITY_PRESETS:
        raise ValueError(
            "Invalid quality. Use one of: "
            + ", ".join(QUALITY_PRESETS.keys())
        )
    return key
