FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg (includes ffprobe) for probing + sampling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .

EXPOSE 8001

# Healthcheck: exporter must be serving metrics
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/metrics >/dev/null || exit 1

CMD ["python", "exporter.py"]