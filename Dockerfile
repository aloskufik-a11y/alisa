FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . ./

# Подготовим volume для session/db
RUN mkdir -p /data
ENV BOT_LOG_FILE=/data/bot.log \
    DB_PATH=/data/gifts.db \
    SETTINGS_PATH=/data/settings.json \
    SESSION_PATH=/data/userbot_session

CMD ["python", "run.py"]
