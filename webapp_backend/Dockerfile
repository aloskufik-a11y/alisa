FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY webapp_backend/pyproject.toml /app/webapp_backend/pyproject.toml
RUN pip install --upgrade pip && pip install /app/webapp_backend

COPY webapp_backend/ /app/webapp_backend/

EXPOSE 8080

# Render injects $PORT (10000); Fly uses internal_port from fly.toml (8080).
# Shell form so $PORT expands at runtime.
CMD ["sh", "-c", "uvicorn webapp_backend.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
