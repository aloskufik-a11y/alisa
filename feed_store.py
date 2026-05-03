"""
feed_store.py — Кольцевой буфер последних выгодных лотов для Web App.

Когда scraper отправляет алерт, он также пушит лот сюда. Web App
запрашивает этот список через /api/feed. БД (gifts.db) не меняем —
там лежат только UID для дедупликации.

Дополнительно: если в окружении заданы `WEBAPP_BACKEND_URL` и
`WEBAPP_BACKEND_KEY`, лот форвардится на публичный backend через
`POST /api/push` (для отображения в Mini App, который доступен извне).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from typing import Deque

logger = logging.getLogger(__name__)

_MAX_FEED = 500
_lock = threading.Lock()
_feed: Deque[dict] = deque(maxlen=_MAX_FEED)

_BACKEND_URL = os.getenv("WEBAPP_BACKEND_URL", "").strip().rstrip("/")
_BACKEND_KEY = os.getenv("WEBAPP_BACKEND_KEY", "").strip()
_session = None


async def _post_to_backend(payload: dict) -> None:
    if not _BACKEND_URL:
        return
    global _session
    try:
        import aiohttp
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        headers = {"Content-Type": "application/json"}
        if _BACKEND_KEY:
            headers["X-API-Key"] = _BACKEND_KEY
        async with _session.post(
            f"{_BACKEND_URL}/api/push", json=payload, headers=headers
        ) as r:
            if r.status >= 300:
                logger.warning(f"Backend push status={r.status}")
    except Exception as e:
        logger.debug(f"Backend push failed: {e}")


def _build_payload(gift: dict, market: str) -> dict:
    return {
        "ts":           int(time.time()),
        "market":       (market or "").lower(),
        "id":           gift.get("id"),
        "name":         gift.get("name"),
        "number":       gift.get("number"),
        "slug":         gift.get("slug"),
        "price":        gift.get("price"),
        "floor_price":  gift.get("floor_price"),
        "rarity":       gift.get("rarity"),
        "model_name":   gift.get("model_name"),
        "backdrop_name": gift.get("backdrop_name"),
        "symbol_name":  gift.get("symbol_name"),
        "rarities_pm":  gift.get("rarities_pm") or {},
        "colors":       gift.get("colors") or [],
        "image_url":    gift.get("image_url"),
        "url":          gift.get("url"),
    }


def push(gift: dict, market: str) -> None:
    """Добавляет лот в начало ленты + форвардит на backend (если настроен)."""
    if not isinstance(gift, dict):
        return
    payload = _build_payload(gift, market)
    with _lock:
        _feed.appendleft(payload)

    if _BACKEND_URL:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_post_to_backend({"items": [payload]}))
        except RuntimeError:
            pass


async def push_settings(settings: dict) -> None:
    """Форвардит текущий snapshot настроек на backend (показ в Mini App)."""
    if not _BACKEND_URL or not isinstance(settings, dict):
        return
    await _post_to_backend({"settings": settings})


def snapshot(limit: int = 200) -> list[dict]:
    with _lock:
        items = list(_feed)
    return items[: max(0, min(limit, _MAX_FEED))]


def clear() -> None:
    with _lock:
        _feed.clear()


def size() -> int:
    with _lock:
        return len(_feed)
