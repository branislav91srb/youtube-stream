# YouTube Stream

Restream YouTube videos and live streams as HLS through a single HTTP origin. The app resolves media with **yt-dlp**, publishes with **FFmpeg** to **MediaMTX** over RTSP, and proxies HLS on port `8080` so browsers (and tunnels like Cloudflare) only need one public URL.

## Architecture

```
Browser  →  youtube-stream (:8080)  →  MediaMTX HLS (:8888, internal)
                 │
                 └─ FFmpeg  →  MediaMTX RTSP (:8554, internal)
                         ↑
                      yt-dlp (YouTube URLs)
```

- **youtube-stream** — HTTP UI/player, session cookies, HLS proxy, stream lifecycle
- **mediamtx** — RTSP ingest + HLS remux (not exposed publicly in the default compose file)

## Quick start

Requires [Docker](https://docs.docker.com/get-docker/) and Docker Compose.

```bash
docker compose up -d --build
```

Open:

- Home: http://localhost:8080/
- Watch: http://localhost:8080/watch/`VIDEO_ID`

Example:

```text
http://localhost:8080/watch/dQw4w9WgXcQ?quality=720
```

Stop:

```bash
docker compose down
```

## Usage

| Path | Description |
|------|-------------|
| `/` | Short usage notes |
| `/watch/{video_id}` | Start (or join) a stream and open the player |
| `/watch/{video_id}?quality=720` | Choose encode preset (`q=` also works) |
| `/watch/{video_id}?restart=1` | Force a fresh publish (VOD from the start) |
| `/hls/{video_id}/index.m3u8` | Same-origin HLS playlist |
| `/status/{video_id}` | JSON stream status |
| `POST /events/{video_id}` | Client playback events (logging) |

`video_id` must be an 11-character YouTube ID (`A-Za-z0-9_-`).

### Quality presets

| Value | Notes |
|-------|--------|
| `240`, `360`, `480`, `720`, `1080` | Cap height and bitrate |
| `best` | Highest available |
| *(omitted)* | Defaults to **720** |

Values like `720p` are accepted.

## Environment

Set on the `youtube-stream` service (see `docker-compose.yml`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `8080` | HTTP listen port |
| `MEDIAMTX_HOST` | `mediamtx` | MediaMTX hostname on the Docker network |
| `MEDIAMTX_RTSP_PORT` | `8554` | RTSP publish port |
| `MEDIAMTX_HLS_PORT` | `8888` | Internal HLS port (proxied via `/hls/...`) |
| `MEDIAMTX_HLS_CDN_SECRET` | `yt-restream-hls-proxy` | Must match `hlsCDNSecret` in `mediamtx/mediamtx.yml` |

## Project layout

```text
server.py              # Entry point
app/                   # HTTP server, stream worker, yt-dlp, HLS proxy
templates/player.html  # In-browser HLS player
mediamtx/mediamtx.yml  # MediaMTX config
Dockerfile             # Python 3.13 + ffmpeg + node + yt-dlp
docker-compose.yml     # App + MediaMTX stack
```

## Notes

- Designed for a **single public origin** (e.g. Unraid + Cloudflare Tunnel): only `8080` is published; RTSP/HLS stay on the Docker network.
- VOD is restreamed in real time (`-re`); live YouTube streams are supported when yt-dlp can resolve them.
- Keep **yt-dlp** current inside the image (`pip install -U yt-dlp` in the Dockerfile) — YouTube changes often break extractors.
