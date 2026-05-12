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
    market_norm = (market or "").lower()
    url = gift.get("url")
    if not url:
        # Дополним URL из url_builder, если scraper его не записал
        try:
            from url_builder import (
                build_mrkt_web_link,
                build_portals_gift_link,
                build_fragment_gift_link,
                build_telegram_nft_link,
            )
            gift_id = str(gift.get("id") or "")
            slug = str(gift.get("slug") or "")
            name = str(gift.get("name") or "")
            number = str(gift.get("number") or "")
            if market_norm == "mrkt":
                url = build_mrkt_web_link(slug=slug, gift_id=gift_id)
            elif market_norm == "portals":
                url = build_portals_gift_link(slug=slug, gift_id=gift_id)
            elif market_norm == "fragment":
                url = build_fragment_gift_link(
                    gift_id=gift_id, slug=slug, name=name, number=number,
                )
            elif market_norm == "getgems":
                # Getgems offchain — t.me/nft/{Name}-{Number}; fallback —
                # коллекция.
                tg_link = build_telegram_nft_link(name, number)
                if tg_link:
                    url = tg_link
                elif slug:
                    url = f"https://getgems.io/collection/{slug}"
        except Exception:
            url = None
    return {
        "ts":           int(time.time()),
        "market":       market_norm,
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
        "url":          url,
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
    """Форвардит текущий snapshot настроек + AI-метрики на backend.

    AI-метрики (cache stats + дневной бюджет) живут в `ai_cache` локально
    в процессе бота — Mini App backend никогда сам LLM не вызывает.
    Приклеиваем снапшот к settings-push'у, чтобы /api/ai/stats на
    публичном backend отдавал актуальные числа.
    """
    if not _BACKEND_URL or not isinstance(settings, dict):
        return
    payload: dict = {"settings": settings}
    try:
        import ai_cache  # local import: ai_cache живёт только на стороне бота
        budget = int((settings or {}).get("ai_daily_token_budget") or 0)
        snap = ai_cache.get_stats()
        snap["budget"] = ai_cache.get_budget_status(budget)
        snap["primary_provider"] = (settings.get("ai_provider") or "off").lower()
        snap["primary_model"] = settings.get(
            f"{snap['primary_provider']}_model"
        ) or ""
        snap["fast_model"] = settings.get("ai_fast_model") or ""
        snap["fallback_provider"] = (
            settings.get("ai_fallback_provider") or "off"
        ).lower()
        snap["fallback_model"] = settings.get("ai_fallback_model") or ""
        payload["ai_stats"] = snap
    except Exception:
        # Не блочим push настроек, если ai_cache недоступен (например, в тестах).
        pass
    await _post_to_backend(payload)


async def push_batch(items: list, market: str, mode: str = "replace") -> None:
    """
    Отправляет полный batch лотов одного маркета на backend.

    mode='replace' — backend заменит свой кэш этого маркета этим списком.
    mode='append'  — backend дополнит существующий кэш (но дедуп по id).

    Шлём 4 раза подряд, чтобы Fly.io load balancer разнёс batch по всем
    активным machines (иначе одна реплика получит, другие — нет).
    """
    if not _BACKEND_URL or not isinstance(items, list):
        return
    payloads = [_build_payload(g, market) for g in items if isinstance(g, dict)]
    body = {
        "batch": payloads,
        "market": (market or "").lower(),
        "mode": mode,
    }
    # Один POST — backend сам разнесёт batch на все peer-machines через
    # Fly.io internal DNS (`<app>.internal`).
    await _post_to_backend(body)


async def pull_pending_test_alert(since_ts: int = 0) -> int:
    """
    Возвращает ts последнего запроса теста, если он новее since_ts.
    Иначе 0.
    """
    if not _BACKEND_URL:
        return 0
    global _session
    try:
        import aiohttp
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        params = {"since": str(int(since_ts or 0))}
        headers = {}
        if _BACKEND_KEY:
            headers["X-API-Key"] = _BACKEND_KEY
        async with _session.get(
            f"{_BACKEND_URL}/api/pending_test_alert",
            params=params, headers=headers,
        ) as r:
            if r.status != 200:
                return 0
            data = await r.json()
            if data.get("ok") and data.get("changed"):
                return int(data.get("ts", 0))
            return 0
    except Exception as e:
        logger.debug(f"Backend pull test alert failed: {e}")
        return 0


async def pull_pending_settings(since_ts: int = 0) -> dict | None:
    """
    Тянет с backend настройки, которые пользователь поменял в Mini App.
    Возвращает dict {settings, ts} или None, если нет изменений с since_ts.
    """
    if not _BACKEND_URL:
        return None
    global _session
    try:
        import aiohttp
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        params = {"since": str(int(since_ts or 0))}
        headers = {}
        if _BACKEND_KEY:
            headers["X-API-Key"] = _BACKEND_KEY
        async with _session.get(
            f"{_BACKEND_URL}/api/pending_settings",
            params=params, headers=headers,
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data.get("ok") or not data.get("changed"):
                return None
            return data
    except Exception as e:
        logger.debug(f"Backend pull settings failed: {e}")
        return None


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
