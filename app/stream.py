import subprocess
import threading
import time

from app.applog import log_event
from app.config import (
    DEFAULT_QUALITY,
    FFMPEG_HEADERS,
    FFMPEG_USER_AGENT,
    MEDIAMTX_HOST,
    MEDIAMTX_RTSP_PORT,
    QUALITY_PRESETS,
    resolve_quality,
)
from app.youtube import get_video_meta, get_youtube_urls

processes = {}
workers = {}
stream_status = {}
stop_requests = set()
# Bumped on stop so a superseded worker cannot publish again.
worker_generation = {}
lock = threading.RLock()


def set_stream_status(video_id, **fields):
    with lock:
        current = stream_status.get(video_id, {})
        current.update(fields)
        stream_status[video_id] = current


def get_stream_status(video_id):
    with lock:
        process = processes.get(video_id)
        running = bool(process and process.poll() is None)
        status = dict(stream_status.get(video_id, {}))

    status.setdefault("running", running)
    status.setdefault("publishing", running)
    status.setdefault("last_error", None)
    status.setdefault("is_live", None)
    status.setdefault("duration", None)
    status.setdefault("started_at", None)
    status.setdefault("quality", None)
    status["running"] = running
    status["publishing"] = running

    # Wall-clock position for VOD restreamed with -re (1:1 with media time).
    started_at = status.get("started_at")
    duration = status.get("duration")
    is_live = status.get("is_live")
    if (
        not is_live
        and started_at is not None
        and duration
        and duration > 0
    ):
        if running:
            position = max(0.0, time.time() - float(started_at))
        elif status.get("phase") == "ended":
            position = float(duration)
        else:
            position = max(0.0, time.time() - float(started_at))
        status["position"] = min(float(duration), position)
    else:
        status["position"] = None

    return status


def _worker_is_current(video_id, generation):
    return worker_generation.get(video_id) == generation


def _should_stop(video_id, generation):
    return (
        not _worker_is_current(video_id, generation)
        or video_id in stop_requests
    )

def start_ffmpeg(video_id, quality=DEFAULT_QUALITY):
    quality = resolve_quality(quality)
    preset = QUALITY_PRESETS[quality]

    live_status, duration = get_video_meta(video_id)
    is_live = live_status in ("is_live", "is_upcoming", "post_live")
    print(
        f"[{video_id}] live_status={live_status} "
        f"is_live={is_live} duration={duration} quality={quality}"
    )
    set_stream_status(
        video_id,
        is_live=is_live,
        live_status=live_status,
        duration=duration,
        quality=quality,
        phase="resolving",
        last_error=None,
    )

    urls = get_youtube_urls(video_id, quality=quality)

    rtsp_url = (
        f"rtsp://{MEDIAMTX_HOST}:"
        f"{MEDIAMTX_RTSP_PORT}/youtube/{video_id}"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
    ]

    # VOD must use -re (realtime). Without it FFmpeg downloads too fast,
    # YouTube drops the connection, and the worker restarts from t=0.
    for url in urls:
        if not is_live:
            command.append("-re")

        command.extend(
            [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_on_network_error",
                "1",
                "-reconnect_delay_max",
                "5",
                "-user_agent",
                FFMPEG_USER_AGENT,
                "-headers",
                FFMPEG_HEADERS,
                "-i",
                url,
            ]
        )

    if len(urls) >= 2:
        command.extend(["-map", "0:v:0", "-map", "1:a:0"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a:0?"])

    vf = []
    if preset["height"]:
        vf.append(f"scale=-2:{preset['height']}")

    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if vf:
        command.extend(["-vf", ",".join(vf)])

    command.extend(
        [
            # 2s GOP → ~2s HLS segments.
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-b:v",
            preset["video_bitrate"],
            "-maxrate",
            preset["maxrate"],
            "-bufsize",
            preset["bufsize"],
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:a",
            "aac",
            "-b:a",
            preset["audio_bitrate"],
            "-ar",
            "48000",
            "-rtsp_transport",
            "tcp",
            "-pkt_size",
            "1200",
            "-f",
            "rtsp",
            rtsp_url,
        ]
    )

    print(f"[{video_id}] Starting FFmpeg")
    print(f"[{video_id}] Publishing to {rtsp_url}")
    log_event(
        "stream",
        level="info",
        event="ffmpeg_start",
        video=video_id,
        quality=quality,
        is_live=is_live,
        rtsp=rtsp_url,
    )
    set_stream_status(
        video_id,
        phase="publishing",
        last_error=None,
        started_at=time.time(),
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, is_live


def stream_worker(video_id, quality, generation):
    """
    Live: restart forever on failure.
    VOD: play once at realtime; stop when FFmpeg finishes cleanly.

    `generation` must match worker_generation[video_id] or this
    worker exits without publishing (prevents dual FFmpeg on one path).
    """
    quality = resolve_quality(quality)

    while True:
        with lock:
            if _should_stop(video_id, generation):
                break

        is_live = True
        returncode = None
        process = None

        try:
            process, is_live = start_ffmpeg(video_id, quality=quality)

            with lock:
                if _should_stop(video_id, generation):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        process.kill()
                    break
                processes[video_id] = process

            set_stream_status(
                video_id,
                running=True,
                publishing=True,
                phase="live",
                last_error=None,
                quality=quality,
            )

            for line in process.stderr:
                with lock:
                    if _should_stop(video_id, generation):
                        process.terminate()
                        break
                line = line.strip()
                if line:
                    print(f"[ffmpeg:{video_id}] {line}")

            process.wait()
            returncode = process.returncode
            print(
                f"[{video_id}] FFmpeg stopped "
                f"(exit code {returncode})"
            )
            log_event(
                "stream",
                level="warn" if returncode else "info",
                event="ffmpeg_exit",
                video=video_id,
                quality=quality,
                code=returncode,
            )

        except Exception as e:
            print(f"[{video_id}] Stream error: {e}")
            log_event(
                "stream",
                level="error",
                event="ffmpeg_error",
                video=video_id,
                quality=quality,
                error=e,
            )
            set_stream_status(
                video_id,
                phase="error",
                last_error=str(e),
            )

        finally:
            with lock:
                if process is not None and processes.get(video_id) is process:
                    processes.pop(video_id, None)
            set_stream_status(
                video_id,
                running=False,
                publishing=False,
            )

        with lock:
            if _should_stop(video_id, generation):
                break

        if not is_live and returncode == 0:
            print(f"[{video_id}] VOD finished")
            log_event(
                "stream",
                level="info",
                event="vod_ended",
                video=video_id,
                quality=quality,
            )
            set_stream_status(
                video_id,
                phase="ended",
                last_error=None,
            )
            break

        set_stream_status(
            video_id,
            phase="stopped",
            last_error=(
                None
                if returncode is None
                else f"FFmpeg exited with code {returncode}"
            ),
        )

        print(f"[{video_id}] Restarting in 3 seconds...")
        for _ in range(30):
            with lock:
                if _should_stop(video_id, generation):
                    break
            time.sleep(0.1)
        else:
            continue
        break

    with lock:
        if workers.get(video_id) is threading.current_thread():
            workers.pop(video_id, None)
        if _worker_is_current(video_id, generation):
            stop_requests.discard(video_id)

    print(f"[{video_id}] Stream worker stopped (gen={generation})")


def stop_stream(video_id):
    """Stop FFmpeg + worker so the next ensure_stream starts from t=0."""
    with lock:
        stop_requests.add(video_id)
        worker_generation[video_id] = worker_generation.get(video_id, 0) + 1
        process = processes.get(video_id)
        worker = workers.get(video_id)

    if process is not None and process.poll() is None:
        print(f"[{video_id}] Stopping FFmpeg for restart")
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    if worker is not None and worker.is_alive():
        worker.join(timeout=8)

    with lock:
        processes.pop(video_id, None)
        if worker is not None and workers.get(video_id) is worker:
            workers.pop(video_id, None)
        # Keep stop_requests set until ensure_stream starts a new generation.
        current = stream_status.get(video_id, {})
        current.update(
            phase="starting",
            started_at=None,
            position=None,
            last_error=None,
            running=False,
            publishing=False,
        )
        stream_status[video_id] = current


def ensure_stream(video_id, restart=False, quality=DEFAULT_QUALITY):
    quality = resolve_quality(quality)

    with lock:
        process = processes.get(video_id)
        running = bool(process and process.poll() is None)
        current_q = stream_status.get(video_id, {}).get("quality")
        worker = workers.get(video_id)
        worker_alive = bool(worker and worker.is_alive())

    # Already publishing the requested quality — do not kick the stream
    # (avoids multi-tab / probe fights that cause HLS 401 reconnect loops).
    if running and current_q == quality and not restart:
        return

    if running and current_q == quality and restart:
        stop_stream(video_id)
    elif running and current_q != quality:
        print(
            f"[{video_id}] Quality change {current_q} -> {quality}, restarting"
        )
        stop_stream(video_id)
    elif worker_alive and not running:
        # Zombie worker (e.g. stuck in restart sleep) — invalidate it.
        stop_stream(video_id)

    with lock:
        process = processes.get(video_id)
        if process and process.poll() is None:
            return

        worker = workers.get(video_id)
        if worker and worker.is_alive():
            return

        stop_requests.discard(video_id)
        generation = worker_generation.get(video_id, 0) + 1
        worker_generation[video_id] = generation

        print(
            f"[{video_id}] Starting stream worker "
            f"(quality={quality} gen={generation})"
        )
        current = stream_status.get(video_id, {})
        current.update(
            phase="starting",
            last_error=None,
            quality=quality,
        )
        stream_status[video_id] = current

        thread = threading.Thread(
            target=stream_worker,
            args=(video_id, quality, generation),
            daemon=True,
        )
        workers[video_id] = thread
        thread.start()
