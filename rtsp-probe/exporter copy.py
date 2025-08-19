#!/usr/bin/env python3
import os
import time
import json
import logging
import threading
import subprocess
from prometheus_client import start_http_server, Gauge, Info

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --------------------------------------------------------------------------------------
# Environment / config
# --------------------------------------------------------------------------------------
# Comma‑separated list of RTSP URLs (e.g., "rtsp://mediamtx:8554/obs/mystream,rtsp://cam2/stream")
STREAMS = [s.strip() for s in os.getenv("RTSP_STREAMS", "rtsp://mediamtx:8554/obs/mystream").split(",") if s.strip()]

# Probe interval (seconds)
PROBE_INTERVAL = int(os.getenv("PROBE_INTERVAL", "60"))

# RTSP transport: 'tcp' (reliable) or 'udp' (lower latency but lossy)
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").lower()

# I/O timeout for ffprobe in **microseconds** (works with -rw_timeout)
PROBE_TIMEOUT_US = os.getenv("RTSP_STIMEOUT_US", "15000000")  # default 15s

# --------------------------------------------------------------------------------------
# Prometheus metrics
# --------------------------------------------------------------------------------------
stream_up = Gauge("ffmpeg_stream_up", "Stream availability", ["stream"])
frame_rate = Gauge("ffmpeg_stream_frame_rate", "Frame rate of video stream", ["stream"])
frame_width = Gauge("ffmpeg_stream_frame_width", "Width of video stream", ["stream"])
frame_height = Gauge("ffmpeg_stream_frame_height", "Height of video stream", ["stream"])
video_codec_present = Gauge("ffmpeg_stream_video_codec", "Presence of video codec", ["stream", "codec"])
audio_codec_present = Gauge("ffmpeg_stream_audio_codec", "Presence of audio codec", ["stream", "codec"])
last_error = Info("ffmpeg_stream_last_error", "Last ffprobe error", ["stream"])

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _build_ffprobe_cmd(url: str):
    """
    Build an ffprobe command that works across common builds:
    - Use -rw_timeout (widely supported) for I/O timeout in microseconds.
    - Also append ?timeout=<us> as a URL param for extra compatibility.
    """
    # Append URL timeout parameter (does nothing on some builds, harmless otherwise)
    sep = "&" if "?" in url else "?"
    probe_url = f"{url}{sep}timeout={PROBE_TIMEOUT_US}"

    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", RTSP_TRANSPORT,
        "-rw_timeout", PROBE_TIMEOUT_US,  # prefer this over -stimeout
        "-show_streams",
        "-of", "json",
        probe_url,
    ]
    return cmd

def _zero_stream_metrics(url: str):
    stream_up.labels(stream=url).set(0)
    frame_rate.labels(stream=url).set(0)
    frame_width.labels(stream=url).set(0)
    frame_height.labels(stream=url).set(0)

# --------------------------------------------------------------------------------------
# Probe logic
# --------------------------------------------------------------------------------------
def probe_stream(url: str):
    logging.info(f"Probing stream: {url}")
    try:
        cmd = _build_ffprobe_cmd(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # Include stderr so we know *why* ffprobe failed (404/401/timeout/DNS/etc.)
        if result.returncode != 0 or not (result.stdout or "").strip():
            reason = (result.stderr or "").strip()[:300] or "unknown"
            logging.warning(
                f"Stream {url} not available (ffprobe error). rc={result.returncode} stderr={reason}"
            )
            last_error.labels(stream=url).info({"reason": reason})
            _zero_stream_metrics(url)
            return

        data = json.loads(result.stdout)
        video_found = False
        audio_found = False

        for s in data.get("streams", []):
            stype = s.get("codec_type")
            if stype == "video":
                video_found = True
                codec = s.get("codec_name", "unknown")
                width = int(s.get("width", 0) or 0)
                height = int(s.get("height", 0) or 0)

                # avg_frame_rate is like "30/1"
                afr = s.get("avg_frame_rate", "0/1") or "0/1"
                try:
                    num, denom = (int(x) for x in afr.split("/", 1))
                    rate = (num / denom) if denom else 0.0
                except Exception:
                    rate = 0.0

                frame_rate.labels(stream=url).set(rate)
                frame_width.labels(stream=url).set(width)
                frame_height.labels(stream=url).set(height)
                video_codec_present.labels(stream=url, codec=codec).set(1)

            elif stype == "audio":
                audio_found = True
                codec = s.get("codec_name", "unknown")
                audio_codec_present.labels(stream=url, codec=codec).set(1)

        if not video_found:
            frame_rate.labels(stream=url).set(0)
            frame_width.labels(stream=url).set(0)
            frame_height.labels(stream=url).set(0)
        if not audio_found:
            # Optional: mark 'unknown' audio absent for this run
            audio_codec_present.labels(stream=url, codec="unknown").set(0)

        stream_up.labels(stream=url).set(1 if (video_found or audio_found) else 0)

    except subprocess.TimeoutExpired:
        logging.error(f"ffprobe timed out (local process timeout) on stream {url}")
        last_error.labels(stream=url).info({"reason": "proc_timeout"})
        _zero_stream_metrics(url)
    except Exception as e:
        msg = f"Unexpected error probing {url}: {e}"
        logging.error(msg)
        last_error.labels(stream=url).info({"reason": str(e)[:120]})
        _zero_stream_metrics(url)

def run_probe_loop():
    while True:
        for url in STREAMS:
            probe_stream(url)
        time.sleep(PROBE_INTERVAL)

# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    start_http_server(8001)
    logging.info("Starting ffmpeg RTSP probe exporter on port 8001")
    threading.Thread(target=run_probe_loop, daemon=True).start()
    while True:
        time.sleep(5)  # keep main thread alive