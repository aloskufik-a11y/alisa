"""
Daily digest — раз в сутки шлёт пользователю summary за прошедшие 24 часа:
топ-5 сделок по скидке, топ коллекций, статистика по маркетам, биггест-дисконт.

Конфигурация (через settings_store, с дефолтами):
- daily_digest_enabled    : bool (default True)
- daily_digest_hour_utc   : int 0-23 (default 6)  — час отправки в UTC.
                            6 UTC ≈ 09:00 МСК / 18:00 Токио. Сделать конфигурируемым.
- daily_digest_window_hours: int (default 24)

Алгоритм:
- async-задача спит до следующего hh:00 в UTC, проверяет час == daily_digest_hour_utc
- если включено — собирает из database.alerts_log за окно, форматирует и шлёт в TG
- ставит state-маркер last_digest_date в settings, чтобы не слать дважды
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def compute_digest(window_hours: int = 24) -> dict[str, Any]:
    """Считает агрегаты по alerts_log за последние N часов. Pure-функция, тестируема."""
    from database import fetch_alerts_window

    rows = fetch_alerts_window(hours=window_hours)
    total = len(rows)

    by_market: dict[str, int] = {}
    by_collection: dict[str, list[dict]] = {}
    biggest_discount = 0.0
    avg_savings_ton = 0.0
    sum_savings = 0.0
    sum_savings_count = 0

    for r in rows:
        market = r.get("market") or "unknown"
        by_market[market] = by_market.get(market, 0) + 1

        # Имя коллекции часто = первое слово в name (e.g. "Plush Pepe #123" → "Plush Pepe")
        # Если есть model_name — это точнее.
        coll = (r.get("model_name") or r.get("name") or "").strip()
        if coll:
            by_collection.setdefault(coll, []).append(r)

        d_pct = r.get("discount_pct")
        if d_pct and d_pct > biggest_discount:
            biggest_discount = float(d_pct)

        floor = r.get("floor_price")
        price = r.get("price")
        if floor and price and floor > price:
            sum_savings += float(floor) - float(price)
            sum_savings_count += 1

    if sum_savings_count:
        avg_savings_ton = round(sum_savings / sum_savings_count, 2)

    # Топ-5 сделок по discount_pct (только с реальной скидкой)
    top_deals = sorted(
        [r for r in rows if r.get("discount_pct") and r["discount_pct"] > 0],
        key=lambda r: -r["discount_pct"],
    )[:5]

    # Топ-3 hottest коллекций (по числу алертов)
    hottest_collections = sorted(
        ((coll, items) for coll, items in by_collection.items()),
        key=lambda x: -len(x[1]),
    )[:3]
    hottest = [
        {
            "name": coll,
            "count": len(items),
            "avg_discount": round(
                sum(i["discount_pct"] or 0 for i in items) / max(len(items), 1), 1
            ),
        }
        for coll, items in hottest_collections
    ]

    return {
        "total_alerts": total,
        "by_market": by_market,
        "biggest_discount_pct": round(biggest_discount, 2),
        "avg_savings_ton": avg_savings_ton,
        "top_deals": top_deals,
        "hottest_collections": hottest,
        "window_hours": window_hours,
    }


def format_digest_message(stats: dict[str, Any]) -> str:
    """Markdown-форматтер для TG. Без HTML, без линков на маркетплейсы (они ситуативные)."""
    total = stats.get("total_alerts", 0)
    if total == 0:
        return (
            "📊 *Daily Digest*\n\n"
            f"За последние {stats.get('window_hours', 24)}ч алертов не было.\n"
            "_Возможно, рынок остыл или фильтры слишком строгие._"
        )

    lines = [
        "📊 *Daily Digest за последние "
        f"{stats.get('window_hours', 24)}ч*",
        "",
        f"📨 Всего алертов: *{total}*",
    ]

    by_market = stats.get("by_market", {})
    if by_market:
        market_emoji = {"mrkt": "🏪", "portals": "🌀", "fragment": "📜"}
        parts = [
            f"{market_emoji.get(m, '•')} {m.upper()}: {c}"
            for m, c in sorted(by_market.items(), key=lambda x: -x[1])
        ]
        lines.append("По маркетам: " + " · ".join(parts))

    biggest = stats.get("biggest_discount_pct", 0)
    if biggest:
        lines.append(f"🔥 Биггест-дисконт: *{biggest:.1f}%* от floor")

    avg_savings = stats.get("avg_savings_ton", 0)
    if avg_savings:
        lines.append(f"💰 Средняя экономия: *{avg_savings} TON* на алерт")

    top_deals = stats.get("top_deals", [])
    if top_deals:
        lines.append("")
        lines.append("🏆 *Топ сделок дня:*")
        for i, d in enumerate(top_deals, 1):
            num = f"#{d['number']}" if d.get("number") else ""
            mk = (d.get("market") or "").upper()
            lines.append(
                f"{i}. {d['name']} {num} — "
                f"`{d['price']:.1f}` TON ({d['discount_pct']:.1f}% off, {mk})"
            )

    hottest = stats.get("hottest_collections", [])
    if hottest:
        lines.append("")
        lines.append("🌶 *Самые активные коллекции:*")
        for h in hottest:
            lines.append(
                f"• {h['name']} — {h['count']} алертов "
                f"(avg {h['avg_discount']}% off)"
            )

    return "\n".join(lines)


async def send_digest_now(bot, user_id: int, window_hours: int = 24) -> bool:
    """Считает digest и отправляет в TG. Возвращает True если что-то отправили."""
    try:
        stats = compute_digest(window_hours=window_hours)
        text = format_digest_message(stats)

        # Опциональный AI-брифинг сверху сводки. Если AI не настроен или ошибся —
        # просто шлём базовый текст без AI.
        try:
            from settings_store import load_settings
            s = load_settings()
            if s.get("ai_for_digest") and stats.get("total_alerts", 0) > 0:
                from ai_advisor import get_active_provider, analyze_daily
                provider = get_active_provider(s)
                if provider is not None:
                    ai_text = await analyze_daily(provider, stats)
                    if ai_text:
                        provider_emoji = {"groq": "⚡", "gemini": "✨"}.get(
                            (s.get("ai_provider") or "").lower(), "🤖"
                        )
                        text = (
                            f"{provider_emoji} *AI-брифинг:*\n_{ai_text}_\n\n" + text
                        )
        except Exception:
            logger.exception("Daily digest: AI-брифинг провалился, шлём базовый")

        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        logger.info(
            f"Daily digest отправлен: {stats['total_alerts']} алертов за {window_hours}ч"
        )
        return True
    except Exception:
        logger.exception("Daily digest: ошибка отправки")
        return False


async def start_digest_scheduler(bot, user_id: int) -> None:
    """Бесконечный цикл: спит до следующего часа, шлёт digest если час совпал.

    Потокобезопасно завершается через `_shutdown_event` (см. main.py): этот цикл
    использует обычный asyncio.sleep, который правильно отменяется на task.cancel().
    """
    from settings_store import load_settings, save_settings

    logger.info("Daily digest scheduler запущен")
    while True:
        try:
            settings = load_settings()
            enabled = settings.get("daily_digest_enabled", True)
            target_hour = int(settings.get("daily_digest_hour_utc", 6))
            window = int(settings.get("daily_digest_window_hours", 24))

            now = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")
            last_sent = settings.get("last_digest_date")

            should_send = (
                enabled
                and now.hour == target_hour
                and last_sent != today_str
            )

            if should_send:
                ok = await send_digest_now(bot, user_id, window_hours=window)
                if ok:
                    # Записываем дату последней отправки чтобы не дублировать.
                    s = load_settings()
                    s["last_digest_date"] = today_str
                    save_settings(s)

            # Спим до начала следующего часа + 30 секунд buffer.
            next_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=30, microsecond=0
            )
            sleep_for = (next_hour - now).total_seconds()
            await asyncio.sleep(max(60, sleep_for))
        except asyncio.CancelledError:
            logger.info("Daily digest scheduler остановлен")
            raise
        except Exception:
            logger.exception("Daily digest scheduler: исключение в цикле")
            await asyncio.sleep(300)  # ждём 5 мин и продолжаем
