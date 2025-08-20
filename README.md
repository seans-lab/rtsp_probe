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
- **Bitrate Sampling:** Enable `BITRATE_SAMPLE_SECONDS` and set `BITRATE_METHOD` for bitrate metrics.
- **MediaMTX:** Optionally configure `mediamtx.yml` for custom RTSP ingest paths.

### 3. Build & Run

From the project root, start all services:

```sh
docker-compose up --build
```

This will start:
- **mediamtx:** RTSP server for ingest/playback.
- **rtsp-probe:** Probes streams and exposes metrics at `localhost:8001/metrics`.
- **prometheus:** Scrapes metrics and (optionally) pushes to Grafana Cloud.

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
- `ffmpeg_stream_bitrate_bps`
- `ffmpeg_probe_errors_total`
- ...and more (see [`exporter.py`](rtsp-probe/exporter.py))

### 6. Customization

- To use a custom MediaMTX config, uncomment the `volumes` section in [`docker-compose.yml`](docker-compose.yml).
- Add or change RTSP streams by editing the `RTSP_STREAMS` environment variable.

## Troubleshooting

- Ensure all containers are running: `docker ps`
- Check logs: `docker-compose logs`
- Verify stream URLs and network connectivity.