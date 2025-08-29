import os
import gi
import time
import threading
import subprocess
import psutil
import glob
import logging
from prometheus_client import Gauge, Counter, Histogram, start_http_server

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# ---------------- CONFIG ---------------- #
Gst.init(None)

RTSP_URL = os.environ.get("RTSP_URL", "rtsp://mediamtx:8554/obs/mystream")
OUTPUT_PATH = "/mnt/jpegs/frame_%05d.jpg"
FPS = int(os.getenv("JPEG_FPS", "15"))
QUALITY = int(os.getenv("JPEG_QUALITY", "100"))
JPEG_RETENTION_SECONDS = 5

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ---------------- METRICS ---------------- #
fps_gauge = Gauge('gstreamer_jpeg_fps', 'Frames per second processed')
frames_total = Counter('gstreamer_frames_total', 'Total frames processed')
videorate_dropped = Gauge('gstreamer_videorate_dropped_frames', 'Frames dropped by videorate')
processing_latency = Histogram(
    'gstreamer_frame_processing_latency_seconds',
    'Latency per frame',
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60]
)
jpeg_size = Histogram(
    'gstreamer_jpeg_size_bytes',
    'Size of generated JPEG images',
    buckets=[10240, 20480, 100000, 500000, 1000000, 5000000, 10000000]
)
jpeg_width = Gauge('gstreamer_jpeg_frame_width', 'Width of JPEG frames')
jpeg_height = Gauge('gstreamer_jpeg_frame_height', 'Height of JPEG frames')
jpeg_quality_metric = Gauge('gstreamer_jpeg_quality', 'JPEG encoding quality')
pipeline_health = Gauge('gstreamer_pipeline_up', 'Pipeline health (1=running, 0=error)')
rtsp_connected = Gauge('gstreamer_rtsp_connected', 'RTSP connectivity (1=connected, 0=disconnected)')
jpeg_write_errors = Counter('gstreamer_jpeg_write_errors_total', 'JPEG write errors')
cpu_usage = Gauge('gstreamer_cpu_usage_percent', 'CPU usage percent')
mem_usage = Gauge('gstreamer_memory_usage_bytes', 'Memory usage in bytes')
jpeg_files_deleted = Counter('jpeg_files_deleted_total', 'Total JPEG files deleted during cleanup')

jpeg_quality_metric.set(QUALITY)

frame_count = 0
last_time = time.time()
pipeline = None
loop = GLib.MainLoop()

# ---------------- FPS UPDATER ---------------- #
def fps_updater():
    global frame_count, last_time
    while True:
        time.sleep(1)
        elapsed = time.time() - last_time
        fps = frame_count / elapsed if elapsed > 0 else 0
        fps_gauge.set(fps)
        frame_count = 0
        last_time = time.time()

# ---------------- RESOURCE MONITOR ---------------- #
def resource_monitor():
    while True:
        cpu_usage.set(psutil.cpu_percent())
        mem_usage.set(psutil.virtual_memory().used)
        time.sleep(5)

# ---------------- JPEG CLEANUP ---------------- #
def jpeg_cleanup_worker():
    jpeg_dir = os.path.dirname(OUTPUT_PATH)
    while True:
        try:
            now = time.time()
            deleted_count = 0
            for jpeg_file in glob.glob(os.path.join(jpeg_dir, "*.jpg")):
                if os.path.isfile(jpeg_file):
                    file_age = now - os.path.getmtime(jpeg_file)
                    if file_age > JPEG_RETENTION_SECONDS:
                        os.remove(jpeg_file)
                        deleted_count += 1
            if deleted_count > 0:
                logging.info(f"JPEG Cleanup: Deleted {deleted_count} old frames")
                jpeg_files_deleted.inc(deleted_count)
        except Exception as e:
            logging.error(f"Error during JPEG cleanup: {e}")
        time.sleep(5)

# ---------------- RTSP CONNECTIVITY ---------------- #
def check_rtsp_connectivity():
    while True:
        try:
            subprocess.check_output(["ffprobe", "-v", "error", RTSP_URL], stderr=subprocess.STDOUT)
            rtsp_connected.set(1)
        except subprocess.CalledProcessError:
            rtsp_connected.set(0)
        except FileNotFoundError:
            logging.warning("ffprobe not found; RTSP connectivity check skipped")
        time.sleep(5)

# ---------------- PIPELINE RESTART ---------------- #
def restart_pipeline():
    global pipeline
    logging.info("Restarting GStreamer pipeline...")
    pipeline.set_state(Gst.State.NULL)
    time.sleep(2)
    pipeline.set_state(Gst.State.PLAYING)
    pipeline_health.set(1)

# ---------------- PAD PROBE ---------------- #
def on_frame_probe(pad, info):
    global frame_count
    frame_count += 1
    frames_total.inc()
    buf = info.get_buffer()
    if buf:
        caps = pad.get_current_caps()
        if caps:
            structure = caps.get_structure(0)
            width = structure.get_int("width")[1]
            height = structure.get_int("height")[1]
            jpeg_width.set(width)
            jpeg_height.set(height)
        jpeg_size.observe(buf.get_size())
    processing_latency.observe(0)
    return Gst.PadProbeReturn.OK

# ---------------- DROPPED FRAME MONITOR ---------------- #
def monitor_dropped_frames(videorate_element):
    while True:
        try:
            dropped = videorate_element.get_property("drop")
            videorate_dropped.set(dropped)
        except Exception as e:
            logging.warning(f"Failed to get dropped frame count: {e}")
        time.sleep(5)

# ---------------- BUS MESSAGE HANDLER ---------------- #
def on_message(bus, message):
    msg_type = message.type
    if msg_type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        logging.error(f"GStreamer Error: {err}, Debug: {debug}")
        jpeg_write_errors.inc()
        pipeline_health.set(0)
        restart_pipeline()
    elif msg_type == Gst.MessageType.EOS:
        logging.info("End of stream received.")
        restart_pipeline()
    elif msg_type == Gst.MessageType.STATE_CHANGED:
        if message.src == pipeline:
            old, new, _ = message.parse_state_changed()
            if new == Gst.State.PLAYING:
                pipeline_health.set(1)
            elif new in (Gst.State.NULL, Gst.State.READY):
                pipeline_health.set(0)

# ---------------- PIPELINE SETUP ---------------- #
pipeline_str = f"""
    rtspsrc location={RTSP_URL} latency=100 !
    rtph264depay !
    avdec_h264 !
    videorate name=videorate0 !
    video/x-raw,framerate={FPS}/1 !
    videoconvert !
    jpegenc quality={QUALITY} !
    multifilesink location={OUTPUT_PATH} name=multifilesink0
"""

pipeline = Gst.parse_launch(pipeline_str)
bus = pipeline.get_bus()
bus.add_signal_watch()

# Attach frame counting and metric update
sink = pipeline.get_by_name("multifilesink0")
if sink:
    sinkpad = sink.get_static_pad("sink")
    if sinkpad:
        sinkpad.add_probe(Gst.PadProbeType.BUFFER, on_frame_probe)

# Monitor dropped frames
videorate_element = pipeline.get_by_name("videorate0")
if videorate_element:
    threading.Thread(target=monitor_dropped_frames, args=(videorate_element,), daemon=True).start()

bus.connect("message", on_message)

# ---------------- PROMETHEUS SERVER ---------------- #
start_http_server(8000)
logging.info("Prometheus metrics exposed on :8000/metrics")

# ---------------- START THREADS ---------------- #
threading.Thread(target=fps_updater, daemon=True).start()
threading.Thread(target=check_rtsp_connectivity, daemon=True).start()
threading.Thread(target=resource_monitor, daemon=True).start()
threading.Thread(target=jpeg_cleanup_worker, daemon=True).start()

# ---------------- START PIPELINE ---------------- #
logging.info(f"Starting GStreamer pipeline from {RTSP_URL} with FPS={FPS} and Quality={QUALITY}")
pipeline.set_state(Gst.State.PLAYING)

try:
    loop.run()
except KeyboardInterrupt:
    logging.info("Interrupted, stopping pipeline...")
finally:
    pipeline.set_state(Gst.State.NULL)
    pipeline_health.set(0)