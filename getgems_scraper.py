"""
Getgems scraper — публичный API api.getgems.io/public-api.

Endpoint: GET /v1/nfts/offchain/on-sale/gifts
Возвращает все listed-офчейн-гифты (Plush Pepe, Durov's Caps, Pretty Posies,
Vice Cream и т.д.) одним списком, ≤100 на запрос. Для нашей задачи
(сниппинг свежего floor-deal'а) этого достаточно: API отдаёт самые
актуальные листинги первыми.

Auth: API-ключ через TON Connect на https://getgems.io/public-api,
прокидывается в HTTP-заголовок Authorization. Лимит 400 req / 5 мин с IP.

Floor расчёт: getgems не отдаёт per-collection floor в выдаче — считаем
на лету по name (как fragment_scraper делает).
"""
import asyncio
import logging
import random
import re
import aiohttp

from database import is_gift_seen, add_gift  # noqa: F401
import dedup_cache
from logic import is_profitable, format_price, apply_floors
from config import (
    GETGEMS_POLL_INTERVAL,
    GETGEMS_API_KEY,
    ALERT_DISPATCH_CONCURRENCY,
)

logger = logging.getLogger(__name__)

GETGEMS_API_BASE = "https://api.getgems.io/public-api"
LISTED_GIFTS_PATH = "/v1/nfts/offchain/on-sale/gifts"

HEADERS_BASE = {
    "accept": "application/json",
    "User-Agent": "gift-monitor-bot/1.0 (+aiogram)",
}

NAME_NUMBER_RE = re.compile(r"^(.+?)\s*#\s*(\d+)\s*$")


def _normalize_item(raw: dict) -> dict | None:
    """Конвертирует item из getgems в наш стандартный формат.

    Возвращает None если элемент непригоден для алёрта (USDT, нет цены,
    auction, banned currency).
    """
    sale = raw.get("sale") or {}

    # Поддерживаем только TON с фикс-ценой. USDT, аукционы, falling-price пока
    # пропускаем — для таких лотов невозможно сравнить с floor по тем же
    # правилам, что и Mrkt/Portals/Fragment.
    if (sale.get("currency") or "").upper() != "TON":
        return None
    if sale.get("type") not in ("FixPriceSale", "OffchainFixPriceSale"):
        return None

    full_price_str = sale.get("fullPrice") or "0"
    try:
        # fullPrice — в нанотонах (1 TON = 10^9 nanoTON).
        price_ton = int(full_price_str) / 1_000_000_000.0
    except (TypeError, ValueError):
        return None
    if price_ton <= 0:
        return None

    raw_name = (raw.get("name") or "").strip()
    collection_name = raw_name
    number: int | None = None
    m = NAME_NUMBER_RE.match(raw_name)
    if m:
        collection_name = m.group(1).strip()
        try:
            number = int(m.group(2))
        except ValueError:
            number = None

    # Атрибуты: Model / Backdrop / Symbol → имя + rarity_per_mille.
    # rarityPercent у getgems в процентах (e.g. "1.5"); конвертируем в ‰
    # (per-mille), как у Portals/MRKT.
    model_name = backdrop_name = symbol_name = None
    rar_pm: dict[str, float] = {}
    for attr in raw.get("attributes") or []:
        t = (attr.get("traitType") or "").lower()
        v = attr.get("value")
        rp = attr.get("rarityPercent")
        rp_pm: float | None = None
        if rp is not None:
            try:
                rp_pm = float(rp) * 10.0
            except (TypeError, ValueError):
                rp_pm = None
        if t == "model":
            model_name = v
            if rp_pm is not None:
                rar_pm["model"] = rp_pm
        elif t == "backdrop":
            backdrop_name = v
            if rp_pm is not None:
                rar_pm["backdrop"] = rp_pm
        elif t == "symbol":
            symbol_name = v
            if rp_pm is not None:
                rar_pm["symbol"] = rp_pm

    address = raw.get("address") or ""
    coll_addr = raw.get("collectionAddress") or ""
    # У отчейн-гифтов address — placeholder вида "EQf_tg_gift_..." и
    # сам по себе не уникален в межпарсе. Используем (collectionAddress, name).
    uid_seed = f"{coll_addr}|{address}|{raw_name}"

    image_url = raw.get("image") or (raw.get("imageSizes") or {}).get("352")

    # Прямая ссылка на конкретный подарок.
    # Getgems offchain-гифт это телеграм-NFT (Plush Pepe, Durov's Cap, …).
    # Универсальная рабочая ссылка — t.me/nft/{NameNoSpaces}-{Number}: открывает
    # тот же подарок и в Telegram, и в браузере через нативный TG NFT-просмотр.
    # Старый вариант getgems.io/collection/{coll_addr} вёл на список всех лотов
    # коллекции, а не на конкретный подарок, что ломало UX «открыть подарок».
    is_offchain_gift = address.startswith("EQf_tg_gift") or (
        raw.get("kind") == "OffchainNft"
    )
    if is_offchain_gift and collection_name and number is not None:
        name_camel = re.sub(r"\s+", "", collection_name.strip())
        url = f"https://t.me/nft/{name_camel}-{number}"
    elif address and not is_offchain_gift:
        url = f"https://getgems.io/nft/{address}"
    elif coll_addr:
        # Последний fallback: коллекция (если нет имени/номера для NFT-ссылки).
        url = f"https://getgems.io/collection/{coll_addr}"
    else:
        url = "https://getgems.io"

    return {
        "id": uid_seed,
        "name": collection_name,
        "number": number,
        "slug": coll_addr,
        "price": price_ton,
        "floor_price": None,  # рассчитаем через apply_floors() по name
        "currency": "TON",
        "stars_price": None,
        "rarity": None,
        "model_name": model_name,
        "backdrop_name": backdrop_name,
        "symbol_name": symbol_name,
        "rarities_pm": rar_pm,
        "colors": [],
        "image_url": image_url,
        "url": url,
        "market": "getgems",
    }


async def _fetch_page(
    session: aiohttp.ClientSession,
    api_key: str,
    limit: int = 100,
    cursor: str | None = None,
) -> list[dict]:
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    headers = {**HEADERS_BASE, "Authorization": api_key}

    backoff = 5.0
    for attempt in range(5):
        try:
            async with session.get(
                GETGEMS_API_BASE + LISTED_GIFTS_PATH,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if not data.get("success"):
                        logger.warning(
                            f"Getgems API: success=false ({data.get('name', '?')})"
                        )
                        return []
                    return list((data.get("response") or {}).get("items") or [])
                if r.status in (429, 503, 502, 504):
                    wait = backoff + random.uniform(0, backoff * 0.4)
                    logger.warning(
                        f"Getgems: HTTP {r.status} (попытка {attempt + 1}), "
                        f"ждём {wait:.0f}с"
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 300)
                    continue
                if r.status in (401, 403):
                    body = await r.text()
                    logger.error(
                        f"Getgems: HTTP {r.status} — проверьте GETGEMS_API_KEY. "
                        f"Body[:200]: {body[:200]}"
                    )
                    return []
                body = await r.text()
                logger.warning(f"Getgems HTTP {r.status}: {body[:200]}")
                return []
        except asyncio.TimeoutError:
            logger.warning(f"Getgems: таймаут (попытка {attempt + 1})")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except aiohttp.ClientError as e:
            logger.error(f"Getgems сеть: {e}")
            return []
        except Exception as e:
            logger.exception(f"Getgems неожиданная ошибка: {e}")
            return []

    logger.error("Getgems: исчерпали попытки")
    return []


async def _fetch_listed(
    session: aiohttp.ClientSession,
    api_key: str,
    pages: int = 1,
    page_size: int = 100,
) -> list[dict]:
    """Тянет несколько страниц listed-гифтов и нормализует.

    pages=1 → fast-lane (свежие, ~100 свежайших листингов).
    pages=3 → full-lane (300 лотов, для floor аккуратнее).
    """
    seen_ids: set[str] = set()
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(max(1, pages)):
        raw_items = await _fetch_page(session, api_key, limit=page_size, cursor=cursor)
        if not raw_items:
            break
        for raw in raw_items:
            it = _normalize_item(raw)
            if it is None:
                continue
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            out.append(it)
        # Cursor для следующей страницы — getgems API возвращает 'cursor' в response
        # (но _fetch_page вернул только items). Простоты ради — для pages>1 нужно
        # получать cursor отдельно. Пока поддерживаем только одну страницу 100шт,
        # этого хватает.
        break
    return out


async def start_getgems_monitor(
    interval: int | None = None,
    page_size: int = 100,
):
    """Главный цикл мониторинга Getgems offchain-gifts.

    interval=None → GETGEMS_POLL_INTERVAL (60 сек по умолч., full lane).
    interval=10  → fast lane (свежие листинги).

    Если GETGEMS_API_KEY пустой — мониторинг не стартует, выводится warning.
    """
    if not GETGEMS_API_KEY:
        logger.warning("Getgems: GETGEMS_API_KEY не задан, скрейпер не запускается")
        return

    eff_interval = interval if interval is not None else GETGEMS_POLL_INTERVAL
    lane = "fast" if eff_interval < GETGEMS_POLL_INTERVAL else "full"
    logger.info(
        f"Getgems[{lane}] мониторинг запущен (interval={eff_interval}s, "
        f"page_size={page_size})"
    )

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:

        consecutive_fails = 0

        while True:
            try:
                gifts = await _fetch_listed(
                    session, GETGEMS_API_KEY, pages=1, page_size=page_size
                )

                if not gifts:
                    consecutive_fails += 1
                    if consecutive_fails >= 3:
                        logger.error(
                            f"Getgems: {consecutive_fails} циклов без данных"
                        )
                else:
                    consecutive_fails = 0
                    apply_floors(gifts, key="name")
                    logger.info(
                        f"Getgems[{lane}]: получено {len(gifts)} лотов, "
                        f"floor рассчитан"
                    )
                    try:
                        from feed_store import push_batch
                        asyncio.create_task(push_batch(gifts, "getgems"))
                    except Exception:
                        pass

                from settings_store import load_settings
                _s = load_settings()
                max_per_cycle = int(_s.get("max_alerts_per_cycle", 0) or 0)

                new_count = 0
                alerted_count = 0
                skipped_by_limit = 0
                consecutive_seen = 0
                EARLY_EXIT_THRESHOLD = 5
                pending_alerts: list[dict] = []
                for gift in gifts:
                    uid = f"getgems_{gift['id']}"

                    if dedup_cache.contains(uid):
                        consecutive_seen += 1
                        if consecutive_seen >= EARLY_EXIT_THRESHOLD:
                            break
                        continue

                    consecutive_seen = 0

                    if not is_profitable(gift, "getgems"):
                        dedup_cache.mark_seen(uid)
                        continue

                    is_new = add_gift(uid, gift["name"], gift["price"], "getgems")
                    dedup_cache.mark_seen(uid)
                    if not is_new:
                        continue
                    new_count += 1

                    if max_per_cycle > 0 and alerted_count >= max_per_cycle:
                        skipped_by_limit += 1
                        continue

                    logger.info(
                        f"Getgems ALERT: {gift['name']} #{gift.get('number', '?')} "
                        f"— {format_price(gift['price'])}"
                    )
                    pending_alerts.append(gift)
                    alerted_count += 1

                if pending_alerts:
                    from notifier import send_gift_alert, bot
                    from config import USER_ID
                    sem = asyncio.Semaphore(ALERT_DISPATCH_CONCURRENCY)

                    async def _dispatch(g: dict) -> None:
                        async with sem:
                            try:
                                await send_gift_alert(bot, USER_ID, g, market="getgems")
                            except Exception:
                                logger.exception(f"Getgems notify error for {g.get('id')}")
                            try:
                                from feed_store import push as feed_push
                                feed_push(g, "getgems")
                            except Exception:
                                pass

                    await asyncio.gather(
                        *(_dispatch(g) for g in pending_alerts),
                        return_exceptions=True,
                    )

                if new_count:
                    msg = (
                        f"Getgems: новых выгодных лотов: {new_count} "
                        f"(отправлено {alerted_count})"
                    )
                    if skipped_by_limit:
                        msg += (
                            f", пропущено {skipped_by_limit} "
                            f"(лимит {max_per_cycle}/цикл)"
                        )
                    logger.info(msg)

            except Exception as e:
                logger.exception(f"Getgems цикл: {e}")

            if eff_interval <= 15:
                # Fast lane — поджимаем интервал ниже 5s в случае ENV-override.
                jitter = random.uniform(-1, 1)
                await asyncio.sleep(max(3, eff_interval + jitter))
            else:
                jitter = random.uniform(-5, 5)
                await asyncio.sleep(max(15, eff_interval + jitter))
