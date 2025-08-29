#!/usr/bin/env python3
import os
import time
import json
import logging
import threading
import subprocess
from typing import Dict
from collections import defaultdict

from prometheus_client import (
    start_http_server,
    Gauge,
    Histogram,
    Counter,
    Info,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ======================================================================================
# Environment / config
# ======================================================================================

STREAMS = [s.strip() for s in os.getenv("RTSP_STREAMS", "rtsp://mediamtx:8554/obs/mystream").split(",") if s.strip()]
PROBE_INTERVAL = int(os.getenv("PROBE_INTERVAL", "60"))
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").lower()
PROBE_TIMEOUT_US = os.getenv("RTSP_STIMEOUT_US", "15000000")  # 15s

# ======================================================================================
# Prometheus metrics
# ======================================================================================

stream_up = Gauge("ffmpeg_stream_up", "Stream availability", ["stream"])
frame_rate = Gauge("ffmpeg_stream_frame_rate", "Frame rate of video stream", ["stream"])
frame_width = Gauge("ffmpeg_stream_frame_width", "Width of video stream", ["stream"])
frame_height = Gauge("ffmpeg_stream_frame_height", "Height of video stream", ["stream"])
video_codec_present = Gauge("ffmpeg_stream_video_codec", "Presence of video codec", ["stream", "codec"])
audio_codec_present = Gauge("ffmpeg_stream_audio_codec", "Presence of audio codec", ["stream", "codec"])

probe_duration = Histogram(
    "ffmpeg_probe_duration_seconds",
    "Time taken by a single ffprobe run",
    ["stream"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)
probe_errors_total = Counter(
    "ffmpeg_probe_errors_total",
    "Number of ffprobe failures by reason",
    ["stream", "reason"],
)
last_success_ts = Gauge(
    "ffmpeg_stream_last_success_timestamp",
    "Unix time of last successful probe",
    ["stream"],
)
consecutive_failures_g = Gauge(
    "ffmpeg_stream_consecutive_failures",
    "Number of consecutive probe failures since last success",
    ["stream"],
)
probe_exit_code = Gauge(
    "ffmpeg_probe_exit_code",
    "Exit code from last ffprobe run",
    ["stream"],
)
last_error_info = Info(
    "ffmpeg_stream_last_error",
    "Last ffprobe error (short reason)",
    ["stream"],
)

FAIL_STREAK: Dict[str, int] = {}

# ======================================================================================
# Helpers
# ======================================================================================

def _reason_key(stderr: str) -> str:
    s = (stderr or "").lower()
    if "404" in s: return "404"
    if "401" in s: return "401"
    if "connection refused" in s: return "conn_refused"
    if "name or service not known" in s or "no such host" in s or "not known" in s: return "dns"
    if "timed out" in s or "timeout" in s: return "timeout"
    if "454" in s and "session not found" in s: return "rtsp_454"
    if "option not found" in s or "unrecognized option" in s: return "bad_option"
    return "other"

def _short_reason(stderr: str) -> str:
    s = (stderr or "").strip().replace("\n", " ")[:120]
    return s or "unknown"

def _zero_stream_metrics(url: str):
    stream_up.labels(stream=url).set(0)
    frame_rate.labels(stream=url).set(0)
    frame_width.labels(stream=url).set(0)
    frame_height.labels(stream=url).set(0)

def _build_ffprobe_cmd(url: str):
    sep = "&" if "?" in url else "?"
    probe_url = f"{url}{sep}timeout={PROBE_TIMEOUT_US}"
    return [
        "ffprobe", "-v", "error",
        "-rtsp_transport", RTSP_TRANSPORT,
        "-rw_timeout", PROBE_TIMEOUT_US,
        "-show_streams", "-of", "json",
        probe_url
    ]

# ======================================================================================
# Probe logic
# ======================================================================================

def probe_stream(url: str):
    logging.info(f"Probing stream: {url}")
    t0 = time.time()
    try:
        res = subprocess.run(_build_ffprobe_cmd(url), capture_output=True, text=True, timeout=60)
        elapsed = time.time() - t0
        probe_duration.labels(stream=url).observe(elapsed)
        probe_exit_code.labels(stream=url).set(res.returncode)

        if res.returncode != 0 or not (res.stdout or "").strip():
            reason = _short_reason(res.stderr)
            logging.warning(f"Stream {url} not available (rc={res.returncode}) stderr={reason}")
            last_error_info.labels(stream=url).info({"reason": reason})
            probe_errors_total.labels(stream=url, reason=_reason_key(res.stderr)).inc()
            FAIL_STREAK[url] = FAIL_STREAK.get(url, 0) + 1
            consecutive_failures_g.labels(stream=url).set(FAIL_STREAK[url])
            _zero_stream_metrics(url)
            return

        data = json.loads(res.stdout)
        video_found = False
        audio_found = False

        for s in data.get("streams", []):
            stype = s.get("codec_type")
            if stype == "video":
                video_found = True
                codec = s.get("codec_name", "unknown")
                width = int(s.get("width", 0) or 0)
                height = int(s.get("height", 0) or 0)
                afr = s.get("avg_frame_rate", "0/1") or "0/1"
                try:
                    num, denom = map(int, afr.split("/", 1))
                    rate = num / denom if denom else 0.0
                except:
                    rate = 0.0
                frame_rate.labels(stream=url).set(rate)
                frame_width.labels(stream=url).set(width)
                frame_height.labels(stream=url).set(height)
                video_codec_present.labels(stream=url, codec=codec).set(1)
            elif stype == "audio":
                audio_found = True
                codec = s.get("codec_name", "unknown")
                audio_codec_present.labels(stream=url, codec=codec).set(1)

        stream_up.labels(stream=url).set(1 if video_found or audio_found else 0)

        if not video_found:
            frame_rate.labels(stream=url).set(0)
            frame_width.labels(stream=url).set(0)
            frame_height.labels(stream=url).set(0)
        if not audio_found:
            audio_codec_present.labels(stream=url, codec="unknown").set(0)

        FAIL_STREAK[url] = 0
        consecutive_failures_g.labels(stream=url).set(0)
        last_success_ts.labels(stream=url).set(int(time.time()))

    except subprocess.TimeoutExpired:
        probe_duration.labels(stream=url).observe(time.time() - t0)
        probe_exit_code.labels(stream=url).set(124)
        reason = "proc_timeout"
        last_error_info.labels(stream=url).info({"reason": reason})
        probe_errors_total.labels(stream=url, reason=reason).inc()
        FAIL_STREAK[url] = FAIL_STREAK.get(url, 0) + 1
        consecutive_failures_g.labels(stream=url).set(FAIL_STREAK[url])
        logging.error(f"ffprobe timed out on stream {url}")
        _zero_stream_metrics(url)

    except Exception as e:
        probe_duration.labels(stream=url).observe(time.time() - t0)
        probe_exit_code.labels(stream=url).set(1)
        reason = str(e)[:120] or "exception"
        last_error_info.labels(stream=url).info({"reason": reason})
        probe_errors_total.labels(stream=url, reason="exception").inc()
        FAIL_STREAK[url] = FAIL_STREAK.get(url, 0) + 1
        consecutive_failures_g.labels(stream=url).set(FAIL_STREAK[url])
        logging.error(f"Unexpected error probing {url}: {e}")
        _zero_stream_metrics(url)

# ======================================================================================
# Main loop
# ======================================================================================

def run_probe_loop():
    while True:
        for url in STREAMS:
            probe_stream(url)
        time.sleep(PROBE_INTERVAL)

if __name__ == "__main__":
    start_http_server(8001)
    logging.info("Starting ffmpeg RTSP probe exporter on port 8001")
    threading.Thread(target=run_probe_loop, daemon=True).start()
    while True:
        time.sleep(5)