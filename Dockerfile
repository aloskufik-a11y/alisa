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

# Persistence стратегия:
# - Если хост даёт volume (Fly.io) — переменные DB_PATH/SETTINGS_PATH/SESSION_PATH/BOT_LOG_FILE
#   можно выставить на /data/* в окружении деплоя, и состояние выживает рестарт.
# - Если volume нет (Koyeb free, Render) — оставляем дефолтные пути внутри /app:
#   * Telethon-сессия читается из env TELEGRAM_STRING_SESSION (см. main.py)
#   * settings.json подтягивается из Mini App backend на старте
#   * database.sqlite и floor_cache.json эфемерны, восстанавливаются за 1-2 цикла поллинга

CMD ["python", "run.py"]
