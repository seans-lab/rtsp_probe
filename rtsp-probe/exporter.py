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

# Comma-separated list of RTSP URLs
STREAMS = [s.strip() for s in os.getenv("RTSP_STREAMS", "rtsp://mediamtx:8554/obs/mystream").split(",") if s.strip()]

# Probe interval (seconds)
PROBE_INTERVAL = int(os.getenv("PROBE_INTERVAL", "60"))

# RTSP transport: 'tcp' (reliable) or 'udp' (lower latency but lossy)
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").lower()

# I/O timeout for ffprobe/ffmpeg in microseconds (used with -rw_timeout and as URL param)
PROBE_TIMEOUT_US = os.getenv("RTSP_STIMEOUT_US", "15000000")  # default 15s

# Bitrate sampling controls (optional, heavier)
# If BITRATE_SAMPLE_SECONDS > 0, we’ll occasionally sample for a short window.
BITRATE_SAMPLE_SECONDS = int(os.getenv("BITRATE_SAMPLE_SECONDS", "0"))  # 0 = disabled
BITRATE_SAMPLE_EVERY_N = int(os.getenv("BITRATE_SAMPLE_EVERY_N", "4"))  # sample every N probes per stream

# Bitrate sampling method: "auto" | "ffmpeg_pipe" | "ffprobe_packets"
# - ffmpeg_pipe: remux to MPEG-TS on stdout and count bytes (recommended)
# - ffprobe_packets: sum packet sizes via ffprobe -show_packets
BITRATE_METHOD = os.getenv("BITRATE_METHOD", "auto").lower()

# ======================================================================================
# Prometheus metrics (originals)
# ======================================================================================

stream_up = Gauge("ffmpeg_stream_up", "Stream availability", ["stream"])
frame_rate = Gauge("ffmpeg_stream_frame_rate", "Frame rate of video stream", ["stream"])
frame_width = Gauge("ffmpeg_stream_frame_width", "Width of video stream", ["stream"])
frame_height = Gauge("ffmpeg_stream_frame_height", "Height of video stream", ["stream"])
video_codec_present = Gauge("ffmpeg_stream_video_codec", "Presence of video codec", ["stream", "codec"])
audio_codec_present = Gauge("ffmpeg_stream_audio_codec", "Presence of audio codec", ["stream", "codec"])

# ======================================================================================
# Added metrics & observability
# ======================================================================================

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
stream_bitrate = Gauge(
    "ffmpeg_stream_bitrate_bps",
    "Estimated container/stream bitrate (bps)",
    ["stream"],
)
audio_channels_g = Gauge(
    "ffmpeg_stream_audio_channels",
    "Audio channel count",
    ["stream"],
)
audio_sample_rate_g = Gauge(
    "ffmpeg_stream_audio_sample_rate_hz",
    "Audio sample rate (Hz)",
    ["stream"],
)
probe_exit_code = Gauge(
    "ffmpeg_probe_exit_code",
    "Exit code from last ffprobe run",
    ["stream"],
)
# Short, human-friendly last error (for dashboards). Keep it short to limit cardinality.
last_error_info = Info(
    "ffmpeg_stream_last_error",
    "Last ffprobe error (short reason)",
    ["stream"],
)

# Bitrate sampling observability
bitrate_sample_errors = Counter(
    "ffmpeg_bitrate_sample_errors_total",
    "Number of bitrate sampling failures by reason",
    ["stream", "reason"],
)
bitrate_method = Info(
    "ffmpeg_stream_bitrate_method",
    "How bitrate was determined for the last successful probe",
    ["stream"],
)
bitrate_last_sample_seconds = Gauge(
    "ffmpeg_bitrate_last_sample_seconds",
    "Sampling window used for bitrate (seconds); 0 means no sampling",
    ["stream"],
)

# Internal trackers
FAIL_STREAK: Dict[str, int] = {}
_probe_counts = defaultdict(int)  # for sampling cadence per stream

# ======================================================================================
# Helpers
# ======================================================================================

def _reason_key(stderr: str) -> str:
    """Map stderr to a compact reason label for the error counter."""
    s = (stderr or "").lower()
    if "404" in s: return "404"
    if "401" in s: return "401"
    if "connection refused" in s or "refused" in s: return "conn_refused"
    if "name or service not known" in s or "no such host" in s or "not known" in s: return "dns"
    if "timed out" in s or "timeout" in s: return "timeout"
    if "454" in s and "session not found" in s: return "rtsp_454"
    if "option not found" in s or "unrecognized option" in s: return "bad_option"
    return "other"

def _short_reason(stderr: str) -> str:
    s = (stderr or "").strip()
    if not s:
        return "unknown"
    s = s.replace("\n", " ")[:120]
    return s or "unknown"

def _zero_stream_metrics(url: str):
    stream_up.labels(stream=url).set(0)
    frame_rate.labels(stream=url).set(0)
    frame_width.labels(stream=url).set(0)
    frame_height.labels(stream=url).set(0)
    # leave codec presence gauges; they’ll be set again when seen

def _build_ffprobe_cmd(url: str, use_rw_timeout: bool = True):
    """
    Build an ffprobe command:
    - Prefer -rw_timeout (widely supported).
    - Always add URL '?timeout=<us>' as a fallback/hint for builds that parse protocol options from URL.
    """
    sep = "&" if "?" in url else "?"
    probe_url = f"{url}{sep}timeout={PROBE_TIMEOUT_US}"

    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", RTSP_TRANSPORT,
    ]
    if use_rw_timeout:
        cmd += ["-rw_timeout", PROBE_TIMEOUT_US]  # safe on most builds

    cmd += [
        "-show_streams",
        "-show_format",          # to extract format.bit_rate if present
        "-of", "json",
        probe_url,
    ]
    return cmd

def _run_ffprobe_with_fallback(url: str):
    """
    Try ffprobe with -rw_timeout; if the build doesn't support it, retry without.
    Return (rc, stdout, stderr).
    """
    cmd = _build_ffprobe_cmd(url, use_rw_timeout=True)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode == 0:
        return res.returncode, res.stdout, res.stderr

    err = (res.stderr or "").lower()
    if "unrecognized option" in err or "option not found" in err:
        cmd2 = _build_ffprobe_cmd(url, use_rw_timeout=False)
        res2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        return res2.returncode, res2.stdout, res2.stderr

    return res.returncode, res.stdout, res.stderr

def _extract_bitrate_from_streams(data: dict) -> int:
    """Try per-stream bit_rate first (some RTSP encoders set this)."""
    br_total = 0
    for s in data.get("streams", []):
        try:
            br = s.get("bit_rate")
            if br and br not in ("N/A", ""):
                br_total += int(br)
        except Exception:
            pass
    return br_total  # may be 0

def _sample_bitrate_ffmpeg_pipe(url: str, seconds: int) -> int:
    """
    Remuxes the incoming RTSP to MPEG-TS on stdout for `seconds` and counts bytes.
    No decode (copy). Returns bits per second (int), or 0.
    """
    try:
        sep = "&" if "?" in url else "?"
        probe_url = f"{url}{sep}timeout={PROBE_TIMEOUT_US}"
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-rtsp_transport", RTSP_TRANSPORT,
            "-rw_timeout", PROBE_TIMEOUT_US,
            "-i", probe_url,
            "-t", str(seconds),
            "-map", "0",
            "-c", "copy",
            "-f", "mpegts",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024,
        )

        start = time.time()
        total_bytes = 0
        while True:
            if proc.stdout is None:
                break
            chunk = proc.stdout.read(1024 * 64)
            if not chunk:
                break
            total_bytes += len(chunk)
            if (time.time() - start) > (seconds + 1):
                break

        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

        if proc.returncode not in (0, None):
            err = (proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else "").strip()
            logging.warning(f"[bitrate-sample ffmpeg_pipe] rc={proc.returncode} stderr={(err[:120] if err else 'n/a')}")
            return 0

        if seconds <= 0:
            return 0
        bps = int((total_bytes * 8) / seconds)
        logging.info(f"[bitrate-sample ffmpeg_pipe] window={seconds}s bytes={total_bytes} -> {bps} bps")
        return bps

    except Exception as e:
        logging.warning(f"[bitrate-sample ffmpeg_pipe] exception: {e}")
        return 0

def _sample_bitrate_ffprobe_packets(url: str, seconds: int) -> int:
    """
    Reads packets for `seconds` via ffprobe and sums sizes. Returns bps or 0.
    """
    try:
        sep = "&" if "?" in url else "?"
        probe_url = f"{url}{sep}timeout={PROBE_TIMEOUT_US}"
        cmd = [
            "ffprobe",
            "-v", "error",
            "-rtsp_transport", RTSP_TRANSPORT,
            "-rw_timeout", PROBE_TIMEOUT_US,
            "-show_packets",
            "-select_streams", "v:a",
            "-read_intervals", f"+{seconds}",
            "-of", "json",
            probe_url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 10)
        if res.returncode != 0 or not (res.stdout or "").strip():
            reason = (res.stderr or "no_output").strip()[:120] or "no_output"
            logging.warning(f"[bitrate-sample ffprobe_packets] rc={res.returncode} stderr={reason}")
            return 0
        pj = json.loads(res.stdout)
        total_bytes = 0
        for pkt in pj.get("packets", []):
            try:
                total_bytes += int(pkt.get("size", 0) or 0)
            except Exception:
                continue
        if seconds <= 0:
            return 0
        bps = int((total_bytes * 8) / seconds)
        logging.info(f"[bitrate-sample ffprobe_packets] window={seconds}s bytes={total_bytes} -> {bps} bps")
        return bps
    except Exception as e:
        logging.warning(f"[bitrate-sample ffprobe_packets] exception: {e}")
        return 0

def _sample_bitrate_choose(url: str, seconds: int) -> int:
    # explicit selection
    if BITRATE_METHOD == "ffmpeg_pipe":
        return _sample_bitrate_ffmpeg_pipe(url, seconds)
    if BITRATE_METHOD == "ffprobe_packets":
        return _sample_bitrate_ffprobe_packets(url, seconds)
    # auto: prefer ffmpeg_pipe, fall back to ffprobe packets
    bps = _sample_bitrate_ffmpeg_pipe(url, seconds)
    if bps > 0:
        return bps
    return _sample_bitrate_ffprobe_packets(url, seconds)

# ======================================================================================
# Probe logic
# ======================================================================================

def probe_stream(url: str):
    logging.info(f"Probing stream: {url}")
    t0 = time.time()
    try:
        rc, out, err = _run_ffprobe_with_fallback(url)
        elapsed = time.time() - t0
        probe_duration.labels(stream=url).observe(elapsed)
        probe_exit_code.labels(stream=url).set(rc)

        # ------------------------ FAILURE PATH (now samples bitrate) -------------------
        if rc != 0 or not (out or "").strip():
            reason = _short_reason(err)
            logging.warning(f"Stream {url} not available (ffprobe rc={rc}) stderr={reason}")
            last_error_info.labels(stream=url).info({"reason": reason})
            probe_errors_total.labels(stream=url, reason=_reason_key(err)).inc()

            # NEW: attempt bitrate sampling even when ffprobe failed
            if BITRATE_SAMPLE_SECONDS > 0:
                bitrate_last_sample_seconds.labels(stream=url).set(BITRATE_SAMPLE_SECONDS)
                sampled = _sample_bitrate_choose(url, BITRATE_SAMPLE_SECONDS)
                if sampled > 0:
                    stream_bitrate.labels(stream=url).set(sampled)
                else:
                    bitrate_sample_errors.labels(stream=url, reason="sample_failed_on_ffprobe_error").inc()

            FAIL_STREAK[url] = FAIL_STREAK.get(url, 0) + 1
            consecutive_failures_g.labels(stream=url).set(FAIL_STREAK[url])
            _zero_stream_metrics(url)
            return

        # ------------------------ SUCCESS PATH ----------------------------------------
        data = json.loads(out)
        video_found = False
        audio_found = False

        # Reset gauges each probe run
        stream_bitrate.labels(stream=url).set(0)
        audio_channels_g.labels(stream=url).set(0)
        audio_sample_rate_g.labels(stream=url).set(0)
        bitrate_last_sample_seconds.labels(stream=url).set(0)

        # ----- Bitrate detection -----
        reported_br = 0
        method = "none"

        # 1) Format-level bitrate
        fmt = data.get("format") or {}
        try:
            if fmt.get("bit_rate") not in (None, "N/A", ""):
                reported_br = int(fmt["bit_rate"])
                method = "format"
        except Exception:
            pass

        # 2) Per-stream bitrates
        if reported_br == 0:
            streams_br = _extract_bitrate_from_streams(data)
            if streams_br > 0:
                reported_br = streams_br
                method = "stream"

        # 3) Optional sampling
        if reported_br == 0 and BITRATE_SAMPLE_SECONDS > 0:
            _probe_counts[url] += 1
            if _probe_counts[url] % max(1, BITRATE_SAMPLE_EVERY_N) == 0:
                bitrate_last_sample_seconds.labels(stream=url).set(BITRATE_SAMPLE_SECONDS)  # set on attempt
                sampled = _sample_bitrate_choose(url, BITRATE_SAMPLE_SECONDS)
                if sampled > 0:
                    reported_br = sampled
                    method = "sample"
                else:
                    bitrate_sample_errors.labels(stream=url, reason="sample_failed").inc()

        # Publish bitrate + method tag
        bitrate_method.labels(stream=url).info({"method": method})
        if reported_br > 0:
            stream_bitrate.labels(stream=url).set(reported_br)

        # ----- Parse streams -----
        for s in data.get("streams", []):
            stype = s.get("codec_type")
            if stype == "video":
                video_found = True
                codec = s.get("codec_name", "unknown")
                width = int(s.get("width", 0) or 0)
                height = int(s.get("height", 0) or 0)

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

                try:
                    sr = int(s.get("sample_rate") or 0)
                except Exception:
                    sr = 0
                ch = int(s.get("channels", 0) or 0)
                audio_sample_rate_g.labels(stream=url).set(sr)
                audio_channels_g.labels(stream=url).set(ch)

        # Availability
        stream_up.labels(stream=url).set(1 if (video_found or audio_found) else 0)

        # If no video, zero basic video gauges
        if not video_found:
            frame_rate.labels(stream=url).set(0)
            frame_width.labels(stream=url).set(0)
            frame_height.labels(stream=url).set(0)

        # If no audio, mark unknown audio as absent for this run (optional)
        if not audio_found:
            audio_codec_present.labels(stream=url, codec="unknown").set(0)

        # Success: reset failure streak and record timestamp
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
        logging.error(f"ffprobe timed out (local process timeout) on stream {url}")
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
# Loop & main
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
        time.sleep(5)  # keep main thread alive