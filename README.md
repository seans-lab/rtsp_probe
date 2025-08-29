# RTSP Probe & Prometheus Exporter

This project provides a containerized RTSP stream probe and Prometheus metrics exporter, designed for monitoring RTSP streams and exporting detailed metrics for observability.

## Features

- Probes RTSP streams using FFmpeg/ffprobe.
- Exports stream health, video/audio properties, bitrate, and error metrics to Prometheus.
- Optional bitrate sampling for more accurate metrics.
- Ready for use with Docker Compose and Grafana Cloud.

## Project Structure

```
docker-compose.yml
mediamtx.yml
prometheus.yml
rtsp-probe/
  Dockerfile
  exporter.py
  requirements.txt
```

## Quick Start

### 1. Prerequisites

- Docker & Docker Compose installed.
- (Optional) Grafana Cloud account for remote metrics.

### 2. Configuration

- **RTSP Streams:** Set the `RTSP_STREAMS` environment variable in `docker-compose.yml` to your stream URLs.
- **Probe Interval:** Adjust `PROBE_INTERVAL` (seconds) as needed.
- **MediaMTX:** Optionally configure `mediamtx.yml` for custom RTSP ingest paths.

### 3. Build & Run

From the project root, start all services:

```sh
docker-compose up --build
```

This will start:
- **mediamtx:** RTSP server for ingest/playback (optional, see below).
- **rtsp-probe:** Probes streams and exposes metrics at `localhost:8001/metrics`.
- **prometheus:** Scrapes metrics and (optionally) pushes to Grafana Cloud. Prometheus web UI is available at [http://localhost:9000](http://localhost:9000).

### 4. Prometheus Configuration

Edit [`prometheus.yml`](prometheus.yml) to set your Grafana Cloud credentials:

```yaml
remote_write:
- url: https://<prometheus_instance>.grafana.net/api/prom/push
  basic_auth:
    username: <username>
    password: <password>
```

### 5. Metrics

Visit [http://localhost:8001/metrics](http://localhost:8001/metrics) to see exported metrics.

Key metrics include:
- `ffmpeg_stream_up`
- `ffmpeg_stream_frame_rate`
- `ffmpeg_probe_errors_total`
- ...and more (see [`exporter.py`](rtsp-probe/exporter.py))

### 6. Customization

- To use a custom MediaMTX config, uncomment the `volumes` section in [`docker-compose.yml`](docker-compose.yml).
- Add or change RTSP streams by editing the `RTSP_STREAMS` environment variable.

## MediaMTX Usage (Optional)

MediaMTX is included as an optional RTSP/RTMP server.  
You can send an RTMP feed to MediaMTX using OBS or another encoder:

- **RTMP ingest URL:**  
  ```
  rtmp://<docker_host>:1935/obs/mystream
  ```
  Replace `<docker_host>` with your Docker host's IP or hostname.

- MediaMTX will make the stream available for probing and playback via RTSP or HLS.

If you already have an RTSP server, you can disable or remove the MediaMTX service from `docker-compose.yml`.

## Streaming Demo Instructions

### Sending a Video Stream Using OBS

1. **Install OBS Studio**  
   Download and install from [https://obsproject.com/](https://obsproject.com/).

2. **Configure OBS for RTMP Streaming**
   - Go to **Settings → Stream**.
   - Set **Service** to `Custom`.
   - Set **Server** to your RTMP endpoint, e.g.:
     ```
     rtmp://<docker_host>:1935/obs/mystream
     ```
   - Set **Stream Key** as needed (or leave blank if not required).
   - Click **Start Streaming**.

---

### Playing Back the Video Feed Using VLC

1. **Install VLC Media Player**  
   Download and install from [https://www.videolan.org/vlc/](https://www.videolan.org/vlc/).

2. **Open the Network Stream**
   - Go to **Media → Open Network Stream**.
   - Enter the RTMP or RTSP URL, for example:
     ```
     rtmp://<docker_host>:1935/obs/mystream
     ```
     or
     ```
     rtsp://<docker_host>:8554/mystream
     ```
   - Click **Play**.

---

## Metrics & Prometheus

- The RTSP probe exports metrics at [http://localhost:8001/metrics](http://localhost:8001/metrics).
- Prometheus scrapes these metrics and provides a web UI at [http://localhost:9000](http://localhost:9000).
- You can query stream health, frame rate, errors, and more.

---

## Credits

This project uses the following technologies:

- **NGINX with RTMP Module** or **MediaMTX** – Streaming server
- **GStreamer** – Media processing and pipeline management
- **OBS Studio** – Video source and streaming
- **VLC Media Player** – Playback
- **Prometheus** – Metrics scraping and monitoring
- **Docker & Docker Compose** – Container orchestration
- **OpenTelemetry** – Tracing and metrics
- **Python** – Scripting and custom exporters

Special thanks to the open-source communities behind these projects!!!