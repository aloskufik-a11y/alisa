"""
Fragment.com scraper — версия с HTML-скрейпингом.

Fragment больше не отдаёт открытый JSON API для каталога подарков, поэтому
парсим HTML напрямую с публичной страницы /gifts. Страница уже сортирует
самые свежие/дешёвые лоты, хеш сессии для просмотра не требуется.

Особенности:
  - Persistent aiohttp.ClientSession (cookies)
  - Курс TON/USD из rate_provider → корректная конвертация Stars→TON
  - Чередование сортировок (price_asc / listed) для максимального охвата
  - Exponential backoff + jitter для 429/403/503
  - Безопасный парсинг HTML (regex по стабильным CSS-классам)
"""
import asyncio
import logging
import random
import aiohttp

from database import is_gift_seen, add_gift
from logic import (
    parse_fragment_html,
    is_profitable,
    format_price,
    format_stars,
    apply_floors,
)
from config import FRAGMENT_POLL_INTERVAL
from rate_provider import rate_provider

logger = logging.getLogger(__name__)

FRAGMENT_BASE = "https://fragment.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Чередуем сортировки: дешёвые → свежие
SORT_ORDERS = ["price_asc", "listed"]

_debug_logged = False


async def _fetch_page(
    session: aiohttp.ClientSession,
    sort: str = "price_asc",
) -> list:
    """
    Получает HTML-страницу /gifts?sort=<sort>&filter=sale и парсит лоты.
    Возвращает список словарей с подарками (price → TON).
    """
    global _debug_logged

    # Получаем актуальный курс TON/USD для конвертации Stars→TON
    await rate_provider.ensure_fresh()
    rate = rate_provider.stars_to_ton(1.0)

    url = f"{FRAGMENT_BASE}/gifts?sort={sort}&filter=sale"

    backoff = 5.0
    for attempt in range(5):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:

                if response.status == 200:
                    html = await response.text()
                    gifts = parse_fragment_html(html, stars_to_ton_rate=rate)

                    if not _debug_logged and gifts:
                        _debug_logged = True
                        sample = gifts[0]
                        logger.info(
                            f"Fragment HTML DEBUG (sort={sort}, всего {len(gifts)}, "
                            f"первый: {sample.get('name')} #{sample.get('number')} "
                            f"@ {sample.get('price')} {sample.get('currency')})"
                        )

                    return gifts

                elif response.status == 429:
                    jitter = random.uniform(0, backoff * 0.4)
                    wait = backoff + jitter
                    logger.warning(
                        f"Fragment: 429 Rate Limit (попытка {attempt + 1}), "
                        f"ждём {wait:.0f}с"
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 300)

                elif response.status in (403, 503, 502):
                    jitter = random.uniform(0, 8)
                    wait = backoff + jitter
                    logger.warning(
                        f"Fragment: {response.status} (попытка {attempt + 1}), "
                        f"ждём {wait:.0f}с"
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 300)

                else:
                    text = await response.text()
                    logger.warning(
                        f"Fragment HTTP {response.status}: {text[:200]}"
                    )
                    return []

        except asyncio.TimeoutError:
            logger.warning(f"Fragment: таймаут (попытка {attempt + 1})")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except aiohttp.ClientError as e:
            logger.error(f"Fragment сеть: {e}")
            return []
        except Exception as e:
            logger.exception(f"Fragment неожиданная ошибка: {e}")
            return []

    logger.error("Fragment: исчерпали попытки")
    return []


async def _fetch_all_orders(session: aiohttp.ClientSession) -> list:
    """Загружает страницы для всех вариантов сортировки и объединяет."""
    all_gifts: dict[str, dict] = {}
    for sort in SORT_ORDERS:
        gifts = await _fetch_page(session, sort=sort)
        for g in gifts:
            uid = f"fragment_{g.get('id')}"
            all_gifts[uid] = g
        # Небольшая пауза между запросами, чтобы не словить 429
        await asyncio.sleep(random.uniform(2, 5))
    return list(all_gifts.values())


async def start_fragment_monitor():
    """Главный цикл мониторинга Fragment.com."""
    logger.info(
        f"Fragment HTML мониторинг запущен (interval={FRAGMENT_POLL_INTERVAL}s)"
    )

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout,
        connector=connector,
    ) as session:

        consecutive_fails = 0

        while True:
            try:
                # Курс может обновиться — лог изменений
                old_rate = rate_provider.stars_to_ton(1.0)
                await rate_provider.ensure_fresh()
                new_rate = rate_provider.stars_to_ton(1.0)
                if abs(new_rate - old_rate) > 0.0001:
                    logger.info(
                        f"Fragment: курс обновлён, "
                        f"1 Star = {new_rate:.5f} TON "
                        f"(TON/USD = ${rate_provider.ton_usd:.2f})"
                    )

                gifts = await _fetch_all_orders(session)

                if not gifts:
                    consecutive_fails += 1
                    if consecutive_fails >= 3:
                        logger.error(
                            f"Fragment: {consecutive_fails} циклов без данных"
                        )
                else:
                    consecutive_fails = 0
                    # Считаем floor по name из текущего batch
                    apply_floors(gifts, key="name")
                    floors_summary = sorted({
                        (g["name"], g.get("floor_price"))
                        for g in gifts if g.get("floor_price")
                    })[:5]
                    logger.info(
                        f"Fragment: получено {len(gifts)} лотов, "
                        f"посчитан floor для {len(floors_summary)}+ коллекций"
                    )

                # Обработка найденных лотов
                from settings_store import load_settings
                _s = load_settings()
                max_per_cycle = int(_s.get("max_alerts_per_cycle", 0) or 0)

                new_count = 0
                alerted_count = 0
                skipped_by_limit = 0
                for gift in gifts:
                    uid = f"fragment_{gift['id']}"

                    if is_gift_seen(uid):
                        continue

                    if not is_profitable(gift, "fragment"):
                        continue

                    add_gift(uid, gift["name"], gift["price"], "fragment")
                    new_count += 1

                    if max_per_cycle > 0 and alerted_count >= max_per_cycle:
                        skipped_by_limit += 1
                        continue

                    stars_info = ""
                    if gift.get("stars_price"):
                        stars_info = f" ({format_stars(gift['stars_price'])})"
                    logger.info(
                        f"Fragment ALERT: {gift['name']} "
                        f"#{gift.get('number', '?')} "
                        f"— {format_price(gift['price'])}{stars_info}"
                    )

                    try:
                        from notifier import send_gift_alert, bot
                        from config import USER_ID
                        await send_gift_alert(bot, USER_ID, gift, market="fragment")
                        alerted_count += 1
                    except Exception as e:
                        logger.exception(f"Fragment notify error: {e}")

                if new_count:
                    msg = (
                        f"Fragment: новых выгодных лотов: {new_count} "
                        f"(отправлено {alerted_count})"
                    )
                    if skipped_by_limit:
                        msg += f", пропущено {skipped_by_limit} (лимит {max_per_cycle}/цикл)"
                    logger.info(msg)

            except Exception as e:
                logger.exception(f"Fragment цикл: {e}")

            jitter = random.uniform(-5, 5)
            await asyncio.sleep(max(15, FRAGMENT_POLL_INTERVAL + jitter))
