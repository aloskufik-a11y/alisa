"""
In-memory кэш AI-ответов с TTL.

Зачем:
- На MRKT/Portals часто перевыставляют один и тот же лот (`re-listing`).
  Auto-вердикт "BUY/HOLD/SKIP" по нему практически идентичный → имеет смысл
  кэшировать на ~5 минут вместо повторного запроса в LLM (стоит токены и время).
- Ключ кэша строим по сигнатуре: имя, model, backdrop, persona, цена-bucket
  и floor-bucket. Bucket = округление до 5%, чтобы небольшие колебания цены не
  ломали попадание в кэш.

Также собираем статистику для команды /ai_stats:
- requests:    сколько раз вызвана analyze_gift (всего)
- cache_hits:  сколько раз обошлись без LLM
- cache_miss:  сколько раз пошли в LLM
- by_provider: счётчик удачных вызовов по провайдеру
- by_task:     счётчик по типу задачи (auto / on_demand / digest / chat)
- last_token_estimate: rough оценка потраченных токенов (вход+выход)

Все ошибки swallowed — кэш чисто оптимизация, не должен ронять основной поток.
"""
from __future__ import annotations

import time
import threading
from collections import OrderedDict
from typing import Any


# ── Кэш ────────────────────────────────────────────────────────────────────
_lock = threading.RLock()
_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_CACHE_MAX = 1024


def _bucket(value: float | int | None, bucket_pct: float = 0.05) -> str:
    """Округляет число до bucket_pct (5%) для устойчивости к мелким колебаниям."""
    if value is None:
        return "x"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "x"
    if v <= 0:
        return "0"
    # log-scale buckets — 5% шаг
    import math
    step = math.log1p(bucket_pct)
    return f"{round(math.log(max(v, 1e-9)) / step):d}"


def make_signature(gift: dict, market: str, persona: str = "balanced",
                   model: str = "") -> str:
    """Сигнатура лота для кэша AI-вердикта.
    Включает persona+model — иначе разные настройки получили бы один кэш.
    """
    name = (gift.get("name") or "?").lower().strip()
    backdrop = (gift.get("backdrop_name") or "").lower().strip()
    item_model = (gift.get("model_name") or "").lower().strip()
    price = _bucket(gift.get("price"))
    floor = _bucket(gift.get("floor_price"))
    return (
        f"{market}|{name}|{item_model}|{backdrop}|{price}|{floor}"
        f"|{persona}|{model}"
    )


def get(signature: str, ttl_sec: int) -> str | None:
    """Возвращает закэшированный ответ или None."""
    if ttl_sec <= 0 or not signature:
        return None
    with _lock:
        entry = _cache.get(signature)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > ttl_sec:
            # Протух — выкидываем
            _cache.pop(signature, None)
            return None
        # LRU touch
        _cache.move_to_end(signature)
        _stats["cache_hits"] += 1
        return value


def put(signature: str, value: str) -> None:
    """Кладёт ответ в кэш."""
    if not signature or not value:
        return
    with _lock:
        _cache[signature] = (time.time(), value)
        _cache.move_to_end(signature)
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


# ── Статистика ─────────────────────────────────────────────────────────────
_stats: dict[str, Any] = {
    "requests": 0,
    "cache_hits": 0,
    "cache_miss": 0,
    "by_provider": {},   # {"groq": 12, "gemini": 3}
    "by_task": {},       # {"auto": 10, "on_demand": 5}
    "fallbacks": 0,
    "errors": 0,
    "input_chars": 0,    # суммарная длина prompts → грубая оценка токенов
    "output_chars": 0,   # суммарная длина ответов
}


def record_request(task: str = "auto") -> None:
    with _lock:
        _stats["requests"] += 1
        _stats["by_task"][task] = _stats["by_task"].get(task, 0) + 1


def record_miss(provider: str, input_chars: int = 0, output_chars: int = 0) -> None:
    with _lock:
        _stats["cache_miss"] += 1
        _stats["by_provider"][provider] = _stats["by_provider"].get(provider, 0) + 1
        _stats["input_chars"] += int(input_chars)
        _stats["output_chars"] += int(output_chars)


def record_fallback() -> None:
    with _lock:
        _stats["fallbacks"] += 1


def record_error() -> None:
    with _lock:
        _stats["errors"] += 1


def get_stats() -> dict:
    """Снимок статистики для /ai_stats. Включает рассчитанные поля."""
    with _lock:
        snap = dict(_stats)
        snap["by_provider"] = dict(snap.get("by_provider", {}))
        snap["by_task"] = dict(snap.get("by_task", {}))
        total = snap["requests"] or 1
        snap["cache_hit_rate"] = round(snap["cache_hits"] / total * 100, 1)
        # ≈ 1 токен ≈ 4 chars (en) / 2 chars (ru) — берём 3 как компромисс
        snap["est_input_tokens"] = snap["input_chars"] // 3
        snap["est_output_tokens"] = snap["output_chars"] // 3
        snap["cache_size"] = len(_cache)
        return snap


def reset_stats() -> None:
    """Сброс статистики (для unit-тестов и команды /ai_stats reset)."""
    with _lock:
        _stats["requests"] = 0
        _stats["cache_hits"] = 0
        _stats["cache_miss"] = 0
        _stats["by_provider"] = {}
        _stats["by_task"] = {}
        _stats["fallbacks"] = 0
        _stats["errors"] = 0
        _stats["input_chars"] = 0
        _stats["output_chars"] = 0


def reset_cache() -> None:
    """Сброс кэша (для unit-тестов)."""
    with _lock:
        _cache.clear()
