FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -U yt-dlp

WORKDIR /app

COPY server.py /app/server.py
COPY app /app/app
COPY templates /app/templates

EXPOSE 8080

CMD ["python", "/app/server.py"]
