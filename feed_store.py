"""
feed_store.py — Кольцевой буфер последних выгодных лотов для Web App.

Когда scraper отправляет алерт, он также пушит лот сюда. Web App
запрашивает этот список через /api/feed. БД (gifts.db) не меняем —
там лежат только UID для дедупликации.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Optional

_MAX_FEED = 500          # ёмкость кольцевого буфера
_lock = threading.Lock()
_feed: Deque[dict] = deque(maxlen=_MAX_FEED)


def push(gift: dict, market: str) -> None:
    """Добавляет лот в начало ленты. gift — то же, что отдают парсеры."""
    if not isinstance(gift, dict):
        return
    payload = {
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
    with _lock:
        _feed.appendleft(payload)


def snapshot(limit: int = 200) -> list[dict]:
    """Снимок (копия) текущих записей, не более limit штук."""
    with _lock:
        items = list(_feed)
    return items[: max(0, min(limit, _MAX_FEED))]


def clear() -> None:
    """Очищает ленту (для тестов)."""
    with _lock:
        _feed.clear()


def size() -> int:
    with _lock:
        return len(_feed)
