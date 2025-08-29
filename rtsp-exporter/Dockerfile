FROM python:3.11-slim

# Install ffmpeg/ffprobe
RUN apt-get update && apt-get install -y ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rtsp_probe.py ./

CMD ["python", "rtsp_probe.py"]