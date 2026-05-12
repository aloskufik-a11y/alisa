"""
In-memory кэш AI-ответов с TTL + суточный token budget tracker.

Зачем:
- На MRKT/Portals часто перевыставляют один и тот же лот (`re-listing`).
  Auto-вердикт "BUY/HOLD/SKIP" по нему практически идентичный → имеет смысл
  кэшировать на ~5 минут вместо повторного запроса в LLM (стоит токены и время).
- Ключ кэша строим по сигнатуре: имя, model, backdrop, persona, цена-bucket
  и floor-bucket. Bucket = округление до 5%, чтобы небольшие колебания цены не
  ломали попадание в кэш.

Daily token budget:
- Каждый record_miss(provider, in_chars, out_chars) обновляет суточный счётчик
  токенов в _daily_tokens[YYYY-MM-DD]. Сброс происходит автоматически при смене UTC-даты.
- is_over_budget(budget) → True если сегодня уже потратили ≥ budget токенов (budget=0 — без лимита).
- record_budget_block() — инкремент счётчика пропущенных из-за budget вызовов.

Также собираем статистику для команды /ai_stats:
- requests:    сколько раз вызвана analyze_gift (всего)
- cache_hits:  сколько раз обошлись без LLM
- cache_miss:  сколько раз пошли в LLM
- by_provider: счётчик удачных вызовов по провайдеру
- by_task:     счётчик по типу задачи (auto / on_demand / digest / chat)
- daily_tokens: {YYYY-MM-DD: tokens} — только последние 7 дней
- budget_blocks: сколько LLM-вызовов отклонены из-за исчерпанного бюджета за сегодня

Все ошибки swallowed — кэш чисто оптимизация, не должен ронять основной поток.
"""
from __future__ import annotations

import time
import threading
from collections import OrderedDict
from datetime import datetime, timezone
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
    "budget_blocks": 0,  # отклонено из-за дневного бюджета токенов
    "input_chars": 0,    # суммарная длина prompts → грубая оценка токенов
    "output_chars": 0,   # суммарная длина ответов
}

# Daily token tracker: {YYYY-MM-DD: tokens}. Обрезается до последних 7 дней.
_daily_tokens: dict[str, int] = {}
_DAILY_HISTORY_DAYS = 7
# 1 токен ≈ 4 chars (en) / 2 chars (ru) — берём 3 как компромисс
# (тот же коэффициент, что и в est_input_tokens/est_output_tokens ниже).
CHARS_PER_TOKEN = 3


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trim_daily() -> None:
    """Оставляем только последние _DAILY_HISTORY_DAYS дней."""
    if len(_daily_tokens) <= _DAILY_HISTORY_DAYS:
        return
    keys_sorted = sorted(_daily_tokens.keys())
    for k in keys_sorted[:-_DAILY_HISTORY_DAYS]:
        _daily_tokens.pop(k, None)


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
        # Daily token tracker — суммируем только реальные походы в LLM.
        # Сброс счётчика происходит автоматически при смене UTC-даты.
        tokens = max(0, (int(input_chars) + int(output_chars)) // CHARS_PER_TOKEN)
        today = _today_utc()
        _daily_tokens[today] = _daily_tokens.get(today, 0) + tokens
        _trim_daily()


def record_fallback() -> None:
    with _lock:
        _stats["fallbacks"] += 1


def record_error() -> None:
    with _lock:
        _stats["errors"] += 1


def record_budget_block() -> None:
    """Регистрирует LLM-вызов, отклонённый из-за исчерпанного дневного бюджета."""
    with _lock:
        _stats["budget_blocks"] += 1


def tokens_used_today() -> int:
    """Сколько токенов (in+out) израсходовано за текущий UTC-день."""
    with _lock:
        return int(_daily_tokens.get(_today_utc(), 0))


def is_over_budget(daily_budget_tokens: int) -> bool:
    """True если бюджет > 0 и сегодня уже выбраны ≥ budget токенов.

    budget = 0 → лимита нет, всегда False.
    """
    try:
        budget = int(daily_budget_tokens or 0)
    except (TypeError, ValueError):
        return False
    if budget <= 0:
        return False
    return tokens_used_today() >= budget


def get_budget_status(daily_budget_tokens: int) -> dict:
    """Snapshot для UI / /ai_stats: used, budget, remaining, over."""
    try:
        budget = int(daily_budget_tokens or 0)
    except (TypeError, ValueError):
        budget = 0
    used = tokens_used_today()
    return {
        "used": used,
        "budget": budget,
        "remaining": max(0, budget - used) if budget > 0 else None,
        "over": bool(budget > 0 and used >= budget),
    }


def get_stats() -> dict:
    """Снимок статистики для /ai_stats. Включает рассчитанные поля."""
    with _lock:
        snap = dict(_stats)
        snap["by_provider"] = dict(snap.get("by_provider", {}))
        snap["by_task"] = dict(snap.get("by_task", {}))
        total = snap["requests"] or 1
        snap["cache_hit_rate"] = round(snap["cache_hits"] / total * 100, 1)
        # ≈ 1 токен ≈ 4 chars (en) / 2 chars (ru) — берём 3 как компромисс
        snap["est_input_tokens"] = snap["input_chars"] // CHARS_PER_TOKEN
        snap["est_output_tokens"] = snap["output_chars"] // CHARS_PER_TOKEN
        snap["cache_size"] = len(_cache)
        snap["daily_tokens"] = dict(_daily_tokens)
        snap["tokens_today"] = int(_daily_tokens.get(_today_utc(), 0))
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
        _stats["budget_blocks"] = 0
        _stats["input_chars"] = 0
        _stats["output_chars"] = 0
        _daily_tokens.clear()


def reset_cache() -> None:
    """Сброс кэша (для unit-тестов)."""
    with _lock:
        _cache.clear()
