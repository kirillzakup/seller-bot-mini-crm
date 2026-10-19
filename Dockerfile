# Seller-bot — single container:
#   aiogram bot (long-polling) + FastAPI (admin, landing, payment page, webhooks) + APScheduler.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Almaty

WORKDIR /app

# tzdata for the Almaty timezone; curl for the healthcheck; ffmpeg to transcode
# ElevenLabs mp3 → ogg/opus for native Telegram voice notes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p data

EXPOSE 8000

# Healthcheck hits the FastAPI /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
