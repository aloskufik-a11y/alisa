"""
In-memory LRU дедуп-кэш — горячий уровень перед SQLite-таблицей `gifts`.

Зачем:
- На fast-lane (поллинг 8s) большинство лотов в первой странице — уже виденные.
  Каждый `is_gift_seen()` это sqlite-roundtrip ~0.3-2мс. На 50 лотах × 4 маркета ×
  каждые 8с — это лишние ~150-1000мс/мин впустую крутящих диск.
- LRU set в памяти отвечает за O(1) lookup без диска.
- Источник истины остаётся SQLite (для перезапуска / digest-аналитики).
  При промахе fast-cache → проверяем DB; затем `add_gift` всё равно идёт в DB.

Использование:
    from dedup_cache import seen_recently, mark_seen, contains
    if contains(uid):
        continue                    # 100% видели
    if is_gift_seen(uid):           # промах cache → проверка DB
        mark_seen(uid)
        continue
    if add_gift(...):
        mark_seen(uid)
        # обработать как новый

Также экспортируем `early_exit_if_first_seen()` — оптимизация fast-lane:
если первый item в page уже в дедуп-кэше, то и остальные скорее всего тоже
(API возвращает в порядке listed_at desc); пропускаем парсинг хвоста страницы.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

try:
    from config import DEDUP_CACHE_SIZE as _CACHE_MAX
except Exception:
    _CACHE_MAX = 50000


# OrderedDict как LRU: при доступе move_to_end, при переполнении popitem(last=False).
# Сам set "ключи" — uid вида f"{market}_{id}". value=None (просто маркер).
_lock = threading.RLock()
_cache: "OrderedDict[str, None]" = OrderedDict()


def contains(uid: str) -> bool:
    """True если uid уже есть в hot-cache. O(1). Touch-обновление LRU позиции."""
    if not uid:
        return False
    with _lock:
        if uid in _cache:
            _cache.move_to_end(uid)
            return True
        return False


def mark_seen(uid: str) -> None:
    """Добавляет uid в hot-cache. Уже виденный — обновит LRU позицию."""
    if not uid:
        return
    with _lock:
        if uid in _cache:
            _cache.move_to_end(uid)
            return
        _cache[uid] = None
        if len(_cache) > _CACHE_MAX:
            # Выкидываем самый старый
            _cache.popitem(last=False)


def mark_many(uids) -> None:
    """Батч-вариант. Используется при первом старте чтобы прогреть cache из БД."""
    with _lock:
        for u in uids:
            if not u:
                continue
            if u in _cache:
                _cache.move_to_end(u)
                continue
            _cache[u] = None
        # Тримим за один проход
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def size() -> int:
    """Текущее количество элементов в кэше."""
    with _lock:
        return len(_cache)


def reset() -> None:
    """Полная очистка. Используется в юнит-тестах."""
    with _lock:
        _cache.clear()


def warm_from_db(limit: int = 5000) -> int:
    """Прогрев кэша последними N записями из БД. Вызывать один раз при старте.

    Возвращает количество подтянутых uid. Errors swallowed —
    отсутствие прогрева ≠ фатально, fast-lane сам наполнит cache по ходу работы.
    """
    try:
        from database import _get_conn
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT id, source FROM gifts ORDER BY timestamp DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        # uid в хранилище — только id; в hot-cache мы держим f"{market}_{id}".
        uids = []
        for r in rows:
            gid = r[0]
            src = r[1] or ""
            # gid в БД уже хранится как есть (без префикса) — добавляем market_
            if not gid:
                continue
            if gid.startswith(f"{src}_"):
                uids.append(gid)
            else:
                uids.append(f"{src}_{gid}")
        mark_many(uids)
        return len(uids)
    except Exception:
        return 0
