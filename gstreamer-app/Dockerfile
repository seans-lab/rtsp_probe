FROM debian:bullseye-slim

# Install Python, GStreamer, FFmpeg, and Prometheus client
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-gi python3-gst-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    libcairo2-dev \
    pkg-config \
    libgirepository1.0-dev \
    gobject-introspection \
    ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip3 install prometheus_client psutil

WORKDIR /app
COPY gstreamer_app.py .

CMD ["python3", "gstreamer_app.py"]