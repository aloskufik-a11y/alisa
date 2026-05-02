"""
MRKT Mini App scraper.
- Singleton aiohttp.ClientSession с переиспользованием токена
- Пагинация через cursor (не пропускаем лоты после 30-го)
- Exponential backoff с jitter для 429/502/503
- asyncio.create_task() с именем задачи
- Авто-обновление токена при 401 без перезапуска всей сессии
"""
import asyncio
import logging
import random
import urllib.parse
import json as _json
from typing import Optional

import aiohttp
from telethon.tl import functions

from database import is_gift_seen, add_gift
from logic import parse_mrkt_json, is_profitable, format_price
from notifier import bot
from config import MRKT_POLL_INTERVAL

logger = logging.getLogger(__name__)

# Telethon клиент — устанавливается из main.py
client = None


def set_client(tg_client):
    global client
    client = tg_client


# ─── Получение tgWebAppData ───────────────────────────────────────────────────

async def get_tg_web_data(bot_username: str, web_url: str) -> Optional[str]:
    """Получает tgWebAppData через Telethon RequestWebView."""
    if client is None:
        logger.error("Telethon client не установлен!")
        return None
    try:
        bot_entity = await client.get_entity(bot_username)
        result = await client(functions.messages.RequestWebViewRequest(
            peer=bot_entity,
            bot=bot_entity,
            platform="android",
            from_bot_menu=False,
            url=web_url,
        ))
        # tgWebAppData может быть во фрагменте URL или в query string
        parsed = urllib.parse.urlparse(result.url)
        for part in (parsed.fragment, parsed.query):
            if not part:
                continue
            params = urllib.parse.parse_qs(part, keep_blank_values=False)
            init_data = params.get("tgWebAppData", [None])[0]
            if init_data:
                logger.info(f"✅ tgWebAppData получен для {bot_username}")
                return init_data

        logger.warning(f"tgWebAppData не найден в URL: {result.url[:150]}")
    except Exception as e:
        logger.error(f"Ошибка WebView от {bot_username}: {e}", exc_info=True)
    return None


# ─── MRKT API константы ───────────────────────────────────────────────────────

MRKT_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 "
    "Telegram-Android/11.4.1 (Samsung SM-S928B; Android 14; SDK 34; AVERAGE)"
)

MRKT_WEB_URL   = "https://mrkt.fun"
MRKT_AUTH_URL  = "https://api.tgmrkt.io/api/v1/auth"
MRKT_GIFTS_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"
MRKT_BOT_NAME  = "mrkt"

# Максимум лотов за одну итерацию (пагинация)
MRKT_PAGE_SIZE = 48
MRKT_MAX_PAGES = 3


# ─── Авторизация ──────────────────────────────────────────────────────────────

async def _mrkt_auth(session: aiohttp.ClientSession, init_data: str) -> Optional[str]:
    """Авторизуется в MRKT API, возвращает JWT-токен или None."""
    payload = {"appId": None, "data": init_data, "photo": None}
    headers = {
        "User-Agent": MRKT_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://mrkt.fun",
        "Referer": "https://mrkt.fun/",
        "X-Telegram-Init-Data": init_data,
    }
    try:
        async with session.post(
            MRKT_AUTH_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                # Пробуем разные ключи токена
                token = (
                    data.get("token")
                    or data.get("access_token")
                    or data.get("accessToken")
                    or data.get("jwt")
                    or (data.get("data") or {}).get("token")
                )
                if token:
                    logger.info(f"MRKT: ✅ авторизован (токен {len(token)} символов)")
                    return str(token)
                logger.warning(f"MRKT: токен не найден в ответе: {list(data.keys())}")
            elif resp.status == 401:
                logger.warning("MRKT Auth: 401 — tgWebAppData устарел")
            else:
                text = await resp.text()
                logger.warning(f"MRKT Auth {resp.status}: {text[:200]}")
    except asyncio.TimeoutError:
        logger.warning("MRKT Auth: таймаут")
    except Exception as e:
        logger.warning(f"MRKT Auth ошибка: {e}")
    return None


# ─── Запрос лотов ─────────────────────────────────────────────────────────────

def _make_headers(token: Optional[str], init_data: str) -> dict:
    """Строит заголовки авторизации для запросов к MRKT API."""
    base = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": MRKT_UA,
        "Origin": "https://mrkt.fun",
        "Referer": "https://mrkt.fun/",
        "X-Telegram-Init-Data": init_data,
    }
    if token:
        base["Authorization"] = f"Bearer {token}"
    else:
        # Fallback: tma авторизация
        base["Authorization"] = f"tma {init_data}"
    return base


def _make_payload(cursor: str = "", count: int = MRKT_PAGE_SIZE) -> dict:
    """Строит payload для запроса лотов MRKT."""
    return {
        "backdropNames": [],
        "collectionNames": [],
        "count": count,
        "craftable": None,
        "cursor": cursor,
        "giftType": None,
        "isCrafted": None,
        "isNew": True,           # Только новые листинги
        "isPremarket": None,
        "isTransferable": None,
        "lowToHigh": True,       # Сначала дешёвые
        "luckyBuy": None,
        "maxPrice": None,
        "minPrice": None,
        "modelNames": [],
        "number": None,
        "ordering": "price",
        "query": None,
        "removeSelfSales": True,  # Убираем свои лоты
        "symbolNames": [],
        "tgCanBeCraftedFrom": None,
    }


# ─── Основной цикл MRKT ───────────────────────────────────────────────────────

async def poll_mrkt(interval: int = MRKT_POLL_INTERVAL):
    """
    Мониторинг MRKT (mrkt.fun) с пагинацией и авто-обновлением токена.
    Цены — в TON.
    """
    logger.info("MRKT: получаем tgWebAppData...")
    init_data = await get_tg_web_data(MRKT_BOT_NAME, MRKT_WEB_URL)
    if not init_data:
        logger.error("MRKT: не удалось получить tgWebAppData")
        return

    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        token = await _mrkt_auth(session, init_data)
        _debug_logged = False
        backoff = 10.0
        consecutive_errors = 0
        token_refresh_count = 0

        while True:
            try:
                # ── Пагинация: собираем все страницы за одну итерацию ──
                all_listings: list = []
                cursor = ""

                for page in range(MRKT_MAX_PAGES):
                    payload = _make_payload(cursor)
                    headers = _make_headers(token, init_data)

                    async with session.post(
                        MRKT_GIFTS_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=25),
                    ) as response:

                        if response.status == 200:
                            data = await response.json(content_type=None)
                            backoff = 10.0
                            consecutive_errors = 0

                            # Debug: один раз печатаем структуру
                            if not _debug_logged:
                                _debug_logged = True
                                items = data.get("items", [])
                                if items:
                                    logger.info(
                                        "MRKT DEBUG (первый item):\n"
                                        + _json.dumps(items[0], ensure_ascii=False, indent=2)
                                    )

                            listings = parse_mrkt_json(data)
                            all_listings.extend(listings)

                            # Курсор для следующей страницы
                            new_cursor = data.get("cursor") or data.get("nextCursor") or ""
                            if not new_cursor or not listings:
                                break  # Больше страниц нет

                            cursor = new_cursor
                            # Маленькая пауза между страницами
                            await asyncio.sleep(random.uniform(1.5, 3.0))

                        elif response.status == 401:
                            logger.warning("MRKT: 401 — пробуем обновить токен...")
                            token_refresh_count += 1
                            if token_refresh_count > 3:
                                logger.error("MRKT: токен не обновляется, перезапуск...")
                                return
                            # Пробуем получить новый tgWebAppData
                            new_init = await get_tg_web_data(MRKT_BOT_NAME, MRKT_WEB_URL)
                            if new_init:
                                init_data = new_init
                                token = await _mrkt_auth(session, init_data)
                            break  # Прерываем пагинацию, попробуем на следующей итерации

                        elif response.status == 429:
                            jitter = random.uniform(0, backoff * 0.3)
                            wait = backoff + jitter
                            logger.warning(f"MRKT: 429 Rate Limit (стр.{page+1}), ждём {wait:.0f}с")
                            await asyncio.sleep(wait)
                            backoff = min(backoff * 2, 600)
                            break  # Прерываем пагинацию

                        elif response.status in (502, 503):
                            jitter = random.uniform(0, 10)
                            wait = backoff + jitter
                            logger.warning(f"MRKT: {response.status} Server Error, ждём {wait:.0f}с")
                            await asyncio.sleep(wait)
                            backoff = min(backoff * 2, 300)
                            break

                        else:
                            text = await response.text()
                            logger.warning(f"MRKT API {response.status}: {text[:300]}")
                            consecutive_errors += 1
                            break

                if all_listings:
                    logger.info(f"MRKT: итого {len(all_listings)} лотов")
                    await process_listings("mrkt", all_listings)

            except asyncio.TimeoutError:
                logger.warning("MRKT: таймаут запроса")
                consecutive_errors += 1
                await asyncio.sleep(min(backoff, 60))
            except aiohttp.ClientError as e:
                logger.error(f"MRKT сеть: {e}")
                consecutive_errors += 1
                await asyncio.sleep(min(backoff, 60))
            except Exception as e:
                logger.error(f"MRKT неожиданная ошибка: {e}", exc_info=True)
                consecutive_errors += 1

            if consecutive_errors >= 5:
                logger.warning("MRKT: 5 ошибок подряд, перезапуск сессии")
                return

            await asyncio.sleep(interval)


# ─── Обработка листингов ─────────────────────────────────────────────────────

async def process_listings(market: str, listings: list):
    """Дедупликация, фильтрация и отправка уведомлений."""
    from settings_store import load_settings
    from config import USER_ID
    from notifier import send_gift_alert

    if not listings:
        return

    s = load_settings()
    if not s.get("notifications_on", True):
        return

    new_count = 0
    alerted_count = 0

    for item in listings:
        uid = f"{market}_{item['id']}"

        # Быстрая проверка — сначала in-memory, потом БД
        if is_gift_seen(uid):
            continue

        is_new = add_gift(uid, item["name"], item["price"], market)
        if not is_new:
            continue

        new_count += 1

        if not is_profitable(item, market=market):
            continue

        price_str = format_price(item["price"], item.get("currency", "TON"))
        logger.info(
            f"ALERT [{market.upper()}]: {item['name']} #{item.get('number', '?')} "
            f"— {price_str}"
            + (f" (скидка {round((item['floor_price']-item['price'])/item['floor_price']*100, 1)}% от Floor)"
               if item.get("floor_price") and item["floor_price"] > item["price"] else "")
        )

        await send_gift_alert(bot, USER_ID, item, market=market)
        alerted_count += 1
        await asyncio.sleep(0.3)  # Антиспам

    if new_count:
        logger.info(f"{market.upper()}: {new_count} новых лотов, {alerted_count} алертов отправлено")


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def start_mini_app_scrapers():
    """Запускает все Mini App парсеры как фоновые задачи."""
    logger.info("Запуск MRKT парсера...")
    asyncio.create_task(_mrkt_with_retry(), name="mrkt_scraper")


async def _mrkt_with_retry():
    """MRKT с авто-перезапуском при истечении токена или превышении ошибок."""
    attempt = 0
    while True:
        attempt += 1
        logger.info(f"MRKT: старт сессии #{attempt}")
        try:
            await poll_mrkt(interval=MRKT_POLL_INTERVAL)
        except Exception as e:
            logger.error(f"MRKT парсер упал: {e}", exc_info=True)

        # Нарастающая пауза (60, 120, 180, 240, 300, 300, 300...)
        wait = min(60 * attempt, 300)
        logger.info(f"MRKT: перезапуск через {wait}с...")
        await asyncio.sleep(wait)
