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
    """Потокобезопасный кэш {key: price_ton} + история для floor-drop алертов."""

    def __init__(self):
        self._data: dict[str, float] = {}
        self._previous: dict[str, float] = {}  # для floor-drop сравнения
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def update(self, data: dict[str, float]) -> dict[str, tuple[float, float]]:
        """
        Обновляет данные. Возвращает dict {key: (old_floor, new_floor)} для тех ключей,
        где значение УПАЛО (new < old), чтобы вызывающий мог послать алерты.
        """
        drops: dict[str, tuple[float, float]] = {}
        async with self._lock:
            for k, new in data.items():
                old = self._data.get(k)
                if old is not None and new < old:
                    drops[k] = (old, new)
            self._previous = dict(self._data)
            self._data = data
            self._fetched_at = time.time()
        return drops

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
                    drops = await _mrkt.update(floors)
                    logger.info(f"MRKT floors: обновлено {len(floors)} коллекций")
                    if drops:
                        await _emit_floor_drops("MRKT", drops)
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
                    drops = await _portals.update(floors)
                    logger.info(f"Portals floors: обновлено {len(floors)} коллекций")
                    if drops:
                        await _emit_floor_drops("Portals", drops)
        except Exception as e:
            logger.warning(f"Portals floors refresh: {e}")
        await asyncio.sleep(interval)


# ─── Floor-drop alerts ───────────────────────────────────────────────────────

async def _emit_floor_drops(market_label: str, drops: dict[str, tuple[float, float]]):
    """
    Отправляет уведомление при падении floor больше порога.
    Алертит только самые крупные падения (топ-3 за цикл) чтобы не спамить.
    """
    try:
        from settings_store import load_settings
        from notifier import send_alert
    except Exception:
        return
    s = load_settings()
    if not s.get("floor_drop_alert"):
        return
    pct = float(s.get("floor_drop_pct", 5.0) or 5.0)
    if pct <= 0:
        return

    significant: list[tuple[str, float, float, float]] = []
    for key, (old, new) in drops.items():
        if old <= 0:
            continue
        drop_pct = (old - new) / old * 100.0
        if drop_pct >= pct:
            significant.append((key, old, new, drop_pct))
    if not significant:
        return

    # Топ-3 самых крупных
    significant.sort(key=lambda x: x[3], reverse=True)
    significant = significant[:3]

    lines = [f"📉 <b>Floor упал на {market_label}</b>"]
    for key, old, new, drop_pct in significant:
        lines.append(
            f"• <b>{key}</b>: {old:.2f} → {new:.2f} 💎 (−{drop_pct:.1f}%)"
        )
    msg = "\n".join(lines)
    try:
        await send_alert(msg)
        logger.info(f"Floor-drop alert sent ({market_label}, {len(significant)} коллекций)")
    except Exception:
        logger.exception("send floor-drop alert failed")


# ─── Getgems: per-collection min-cache (multi-batch tracker) ────────────────
#
# Getgems API не отдаёт авторитетный floor по коллекциям, поэтому копим
# минимальную цену по collection name через несколько циклов опроса. Это
# существенно точнее чем `apply_floors` на одном батче из 100 лотов, потому
# что свежие 100 лотов могут случайно не включать самые дешёвые из коллекций
# с большим оборотом.
#
# Структура: {name: (min_price_ton, observed_at)}. observed_at — момент
# когда мы ПОСЛЕДНИЙ раз ВИДЕЛИ лот этой коллекции (по любой цене). Это
# защита от устаревших значений: если 30+ минут не видели коллекцию,
# floor сбрасывается (старый продан или коллекция мертва).

_GETGEMS_FLOOR_TTL_SEC = 30 * 60  # 30 минут
_getgems_floors: dict[str, tuple[float, float]] = {}
_getgems_lock = asyncio.Lock()


def getgems_floor(name: str) -> Optional[float]:
    """Возвращает min-observed price для коллекции, если ещё свежий."""
    if not name:
        return None
    entry = _getgems_floors.get(name)
    if not entry:
        return None
    price, ts = entry
    if time.time() - ts > _GETGEMS_FLOOR_TTL_SEC:
        return None
    return price


def update_getgems_floors_from_batch(gifts: list[dict]) -> int:
    """
    Обновляет min-price кэш из текущего батча Getgems-лотов.
    Возвращает количество коллекций, у которых обновился минимум.

    Должен вызываться после каждого fetch (fast+full lane). Безопасно
    для concurrent вызова — операция чтения dict в Python атомарна, а
    запись через короткий критический участок. Хотим избежать `await`
    внутри hot path, поэтому НЕ используем _getgems_lock здесь — мутации
    одиночные и в худшем случае на гонке мы запишем чуть более высокий
    минимум, который перетрётся следующим циклом.
    """
    if not gifts:
        return 0
    now = time.time()
    updated = 0
    for g in gifts:
        if not isinstance(g, dict):
            continue
        name = g.get("name")
        price = g.get("price")
        if not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        cur = _getgems_floors.get(name)
        if cur is None or price < cur[0]:
            _getgems_floors[name] = (float(price), now)
            updated += 1
        else:
            # Сбрасываем TTL — мы видели коллекцию, значит она живая.
            _getgems_floors[name] = (cur[0], now)
    # Чистим протухшие, чтобы dict не пух
    if len(_getgems_floors) > 500:
        cutoff = now - _GETGEMS_FLOOR_TTL_SEC
        stale = [k for k, (_p, t) in _getgems_floors.items() if t < cutoff]
        for k in stale:
            _getgems_floors.pop(k, None)
    return updated


def _getgems_cache_size() -> int:
    return len(_getgems_floors)


def _clear_getgems_floors_for_test() -> None:
    _getgems_floors.clear()


# ─── Применение к лотам ──────────────────────────────────────────────────────

def apply_authoritative_floors(gifts: list[dict], market: str) -> int:
    """
    Перетирает gift['floor_price'] авторитетным значением из соответствующего
    кэша (если оно свежее). Возвращает кол-во обновлённых лотов.

    Для MRKT ключ в кэше — `name` (collectionTitle).
    Для Portals — `short_name` (производный от `name`).
    Для Getgems — `name` из multi-batch min-кэша.
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
    elif market == "getgems":
        for g in gifts:
            if not isinstance(g, dict):
                continue
            f = getgems_floor(g.get("name", ""))
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
