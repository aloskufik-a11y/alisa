"""
floor_cache.py — Авторитетный кэш Floor-цен по коллекциям для MRKT и Portals.

MRKT  : GET /api/v1/gifts/collections          → list[{title, floorPriceNanoTons}]
Portals: GET /api/collections/floors            → {floorPrices: {short_name: TON_str}}

Каждый кэш обновляется в фоне раз в N секунд; в коде маркетов вместо batch-derived
floor берём авторитетный из этого кэша.

Это решает баг "цена и floor одинаковые, хотя на маркете floor ниже" — раньше
floor считался как min(price) по нашему батчу из 60 лотов; теперь берётся
с самого маркета.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Период обновления (60s — баланс между свежестью и нагрузкой)
FLOOR_REFRESH_SEC = 60

# В секундах: считать данные свежими в течение TTL после последнего успешного фетча
FLOOR_TTL_SEC = 180

UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)


class _SimpleCache:
    """Потокобезопасный кэш {key: (price_ton, fetched_at)}."""

    def __init__(self):
        self._data: dict[str, float] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def update(self, data: dict[str, float]):
        async with self._lock:
            self._data = data
            self._fetched_at = time.time()

    def get(self, key: str) -> Optional[float]:
        if not self._data:
            return None
        if time.time() - self._fetched_at > FLOOR_TTL_SEC:
            return None  # данные протухли
        return self._data.get(key)

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def is_fresh(self) -> bool:
        return self._data and (time.time() - self._fetched_at <= FLOOR_TTL_SEC)


# ─── MRKT ─────────────────────────────────────────────────────────────────────

_mrkt = _SimpleCache()


def mrkt_floor(name: str) -> Optional[float]:
    """
    Возвращает авторитетный floor для MRKT-коллекции по её названию ('Vice Cream').
    Если кэш пуст или протух — None.
    """
    return _mrkt.get(name)


async def _fetch_mrkt_floors(session: aiohttp.ClientSession, token: str) -> dict[str, float]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Origin": "https://mrkt.fun",
        "Referer": "https://mrkt.fun/",
    }
    async with session.get(
        "https://api.tgmrkt.io/api/v1/gifts/collections",
        headers=headers, timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"MRKT collections HTTP {r.status}: {text[:200]}")
        data = await r.json(content_type=None)

    floors: dict[str, float] = {}
    if not isinstance(data, list):
        return floors
    for c in data:
        if not isinstance(c, dict):
            continue
        title = c.get("title") or c.get("name")
        nano = c.get("floorPriceNanoTons") or c.get("floorPriceNanoTONs")
        if not title or not nano:
            continue
        try:
            ton = float(nano) / 1e9
            if ton > 0:
                floors[title] = round(ton, 6)
        except (TypeError, ValueError):
            continue
    return floors


async def refresh_mrkt_loop(get_session, get_token, interval: int = FLOOR_REFRESH_SEC):
    """
    Периодически обновляет MRKT floors. Принимает фабрики, которые возвращают
    свежие session и token (так избегаем цикла зависимостей с poll_mrkt).
    """
    while True:
        try:
            session = get_session()
            token = get_token()
            if session is not None and token:
                floors = await _fetch_mrkt_floors(session, token)
                if floors:
                    await _mrkt.update(floors)
                    logger.info(f"MRKT floors: обновлено {len(floors)} коллекций")
        except Exception as e:
            logger.warning(f"MRKT floors refresh: {e}")
        await asyncio.sleep(interval)


# ─── Portals ──────────────────────────────────────────────────────────────────

_portals = _SimpleCache()


def portals_floor_by_short_name(short_name: str) -> Optional[float]:
    """short_name как у Portals API: 'plushpepe', 'icecream', 'bdaycandle'."""
    return _portals.get(short_name)


def portals_floor_by_display_name(name: str) -> Optional[float]:
    """
    'Plush Pepe' → попробовать через 'plushpepe'.
    """
    if not name:
        return None
    short = "".join(ch for ch in name.lower() if ch.isalnum())
    return _portals.get(short)


async def _fetch_portals_floors(session: aiohttp.ClientSession, init_data: str) -> dict[str, float]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Authorization": f"tma {init_data}",
        "Origin": "https://portals-market.com",
        "Referer": "https://portals-market.com/",
    }
    async with session.get(
        "https://portal-market.com/api/collections/floors",
        headers=headers, timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"Portals floors HTTP {r.status}: {text[:200]}")
        data = await r.json(content_type=None)

    floors: dict[str, float] = {}
    if not isinstance(data, dict):
        return floors
    raw = data.get("floorPrices") or data.get("floor_prices") or {}
    if not isinstance(raw, dict):
        return floors
    for short, val in raw.items():
        try:
            ton = float(val)
            if ton > 0:
                floors[short] = round(ton, 6)
        except (TypeError, ValueError):
            continue
    return floors


async def refresh_portals_loop(get_session, get_init_data, interval: int = FLOOR_REFRESH_SEC):
    while True:
        try:
            session = get_session()
            init_data = get_init_data()
            if session is not None and init_data:
                floors = await _fetch_portals_floors(session, init_data)
                if floors:
                    await _portals.update(floors)
                    logger.info(f"Portals floors: обновлено {len(floors)} коллекций")
        except Exception as e:
            logger.warning(f"Portals floors refresh: {e}")
        await asyncio.sleep(interval)


# ─── Применение к лотам ──────────────────────────────────────────────────────

def apply_authoritative_floors(gifts: list[dict], market: str) -> int:
    """
    Перетирает gift['floor_price'] авторитетным значением из соответствующего
    кэша (если оно свежее). Возвращает кол-во обновлённых лотов.

    Для MRKT ключ в кэше — `name` (collectionTitle).
    Для Portals — `short_name` (производный от `name`).
    """
    if not gifts:
        return 0
    n = 0
    if market == "mrkt":
        for g in gifts:
            if not isinstance(g, dict):
                continue
            f = mrkt_floor(g.get("name", ""))
            if f is not None:
                g["floor_price"] = f
                n += 1
    elif market == "portals":
        for g in gifts:
            if not isinstance(g, dict):
                continue
            f = portals_floor_by_display_name(g.get("name", ""))
            if f is not None:
                g["floor_price"] = f
                n += 1
    return n


# ─── Тестовые фасады (для unit-тестов) ───────────────────────────────────────

def _set_mrkt_floors_for_test(floors: dict[str, float]):
    """Только для тестов: подменяем кэш напрямую."""
    _mrkt._data = dict(floors)
    _mrkt._fetched_at = time.time()


def _set_portals_floors_for_test(floors: dict[str, float]):
    _portals._data = dict(floors)
    _portals._fetched_at = time.time()


def _clear_caches_for_test():
    _mrkt._data = {}
    _mrkt._fetched_at = 0.0
    _portals._data = {}
    _portals._fetched_at = 0.0
