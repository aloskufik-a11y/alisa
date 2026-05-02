"""
logic.py — Парсинг API-ответов и фильтрация выгодных лотов.

ВСЕ ЦЕНЫ УНИФИЦИРОВАНЫ В TON:
  MRKT (tgmrkt.io)  → цены в TON (нативно)
  Fragment.com      → цены в Stars → конвертируем в TON через rate_provider
  GetGems/Portals   → цены в наноТОН → делим на 1e9
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _safe_float(val, default: float = 0.0) -> float:
    """Безопасно конвертирует в float. Поддерживает dict с полем 'amount'."""
    if val is None:
        return default
    if isinstance(val, dict):
        val = val.get("amount", 0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_str(val, default: str = "") -> str:
    """Безопасно конвертирует в str, возвращает default если пусто."""
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _extract_price(item: dict, *keys) -> Optional[float]:
    """
    Ищет цену по нескольким ключам. Пропускает нулевые значения.
    Важно: НЕ использует `or`, чтобы не пропустить легитимный 0.
    """
    for key in keys:
        val = item.get(key)
        if val is not None:
            price = _safe_float(val)
            if price > 0:
                return price
    return None


def _extract_str(item: dict, *keys, default: str = "") -> str:
    """Ищет строку по нескольким ключам, пропускает пустые."""
    for key in keys:
        val = item.get(key)
        if val:
            s = str(val).strip()
            if s:
                return s
    return default


def _extract_image(item: dict) -> str:
    """Извлекает URL изображения из разных возможных полей."""
    candidates = [
        item.get("image_url"),
        item.get("imageUrl"),
        item.get("image"),
        item.get("thumb"),
        item.get("thumbnail"),
        item.get("preview"),
        item.get("photo"),
        item.get("media"),
        (item.get("gift") or {}).get("image_url"),
        (item.get("collection") or {}).get("image_url"),
        (item.get("model") or {}).get("image_url"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and c.startswith("http"):
            return c
    return ""


def _fragment_name_to_slug(name: str) -> str:
    """
    'Eternal Rose' → 'eternalrose'
    'Plush Pepe'   → 'plushpepe'
    'B-Day Candle' → 'bdaycandle'
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def re_has_number_suffix(slug: str) -> bool:
    """Проверяет, заканчивается ли slug на -ЧИСЛО ('eternalrose-42' → True)."""
    return bool(re.search(r"-\d+$", slug))


# ─── MRKT (TON, нативно) ──────────────────────────────────────────────────────

def parse_mrkt_json(json_data: dict) -> list:
    """
    Парсит ответ MRKT API /api/v1/gifts/saling.
    Цены нативно в TON.

    Реальная структура item из MRKT API:
      {
        "id": "abc123",
        "slug": "eternal-rose-42",   ← используется в startapp
        "name": "Eternal Rose",
        "number": 42,                ← номер экземпляра в коллекции
        "price": 5.5,                ← цена в TON
        "floor_price": 7.0,          ← минимальная цена коллекции в TON
        "rarity": "Rare",
        "image_url": "https://...",
      }
    """
    results = []
    try:
        items = None
        if isinstance(json_data, list):
            items = json_data
        elif isinstance(json_data, dict):
            for key in ("items", "data", "gifts", "results", "list"):
                candidate = json_data.get(key)
                if isinstance(candidate, list) and candidate:
                    items = candidate
                    break

        if not items:
            logger.debug("MRKT: пустой список или неизвестная структура")
            return results

        for item in items:
            if not isinstance(item, dict):
                continue

            gift_id = _extract_str(item, "id", "gift_id", "_id", "uuid")
            if not gift_id:
                continue

            gift_name = _extract_str(item, "name", "title", "gift_name", default="Unknown")

            api_slug = _extract_str(item, "slug", "alias")
            if not api_slug:
                api_slug = gift_name.lower().replace(" ", "-").replace("_", "-")

            number = _extract_str(item, "number", "num", "edition", "index")

            # Цена в TON
            price = _extract_price(item, "price", "price_ton", "priceTon", "amount")
            if price is None:
                logger.debug(f"MRKT: пропускаем {gift_id} — нет цены")
                continue

            # Floor в TON
            floor_raw = None
            for fk in ("floor_price", "floorPrice", "floor", "min_price", "minPrice"):
                v = item.get(fk)
                if v is not None:
                    floor_raw = v
                    break
            floor = round(_safe_float(floor_raw), 6) if floor_raw is not None else None
            if floor is not None and floor <= 0:
                floor = None

            rarity_raw = _extract_str(item, "rarity", "rarityLevel", "rarity_name", "tier")
            rarity = rarity_raw.capitalize() if rarity_raw else ""

            image_url = _extract_image(item)
            total_count = item.get("total_count") or item.get("totalCount") or 0

            # Сохраняем оригинальные Stars если есть (для справки)
            stars_price_raw = item.get("stars_price") or item.get("starsPrice")
            stars_price = _safe_float(stars_price_raw) if stars_price_raw else None

            results.append({
                "id": gift_id,
                "name": gift_name,
                "slug": api_slug,
                "number": number,
                "price": round(price, 6),         # TON
                "price_ton": round(price, 6),      # TON (алиас для ясности)
                "stars_price": stars_price,         # Stars (если есть в API)
                "floor_price": floor,              # TON
                "rarity": rarity,
                "currency": "TON",
                "image_url": image_url,
                "total_count": int(total_count) if total_count else 0,
                "market": "mrkt",
            })

    except Exception as e:
        logger.error(f"Ошибка при парсинге MRKT JSON: {e}", exc_info=True)

    return results


# ─── Fragment.com (Stars → конвертируем в TON) ───────────────────────────────

def parse_fragment_json(json_data: dict, stars_to_ton_rate: float = 0.004) -> list:
    """
    Парсит ответ Fragment API (POST /api?method=searchGifts).
    Цены в Stars — конвертируются в TON через stars_to_ton_rate.

    stars_to_ton_rate = STAR_USD / TON_USD
    При TON = $5: rate = 0.02 / 5.0 = 0.004 TON/Star → 250 Stars = 1 TON
    При TON = $3: rate = 0.02 / 3.0 ≈ 0.00667 TON/Star → 150 Stars = 1 TON

    Структура ответа Fragment:
      {
        "ok": true,
        "found": 1234,
        "gifts": [
          {
            "id": 12345,
            "name": "Eternal Rose",
            "num": 42,
            "slug": "eternalrose-42",
            "price": 1500,        ← Stars
            "floor_price": 2000,  ← Stars
            "rarity": "Rare",
            "image_url": "...",
          }
        ]
      }
    """
    results = []
    try:
        if json_data.get("ok") is False:
            error = json_data.get("error", "неизвестная ошибка")
            logger.warning(f"Fragment API: ok=false, error={error}")
            return results

        gifts = json_data.get("gifts", [])
        if not isinstance(gifts, list):
            logger.warning(f"Fragment API: поле 'gifts' не список: {type(gifts)}")
            return results

        for g in gifts:
            if not isinstance(g, dict):
                continue

            gift_id = _extract_str(g, "id", "gift_id")
            if not gift_id:
                continue

            name   = _extract_str(g, "name", "title", default="Unknown")
            number = _extract_str(g, "num", "number", "edition")

            # Slug для URL
            api_slug = _extract_str(g, "slug")
            if api_slug and re_has_number_suffix(api_slug):
                full_slug = api_slug
            elif name and number:
                full_slug = f"{_fragment_name_to_slug(name)}-{number}"
            elif api_slug:
                full_slug = api_slug
            else:
                full_slug = _fragment_name_to_slug(name)

            fragment_url = f"https://fragment.com/gift/{full_slug}"

            # Цена в Stars (оригинал)
            stars_price = _extract_price(g, "price", "amount")
            if stars_price is None:
                continue

            # Конвертация Stars → TON
            price_ton = round(stars_price * stars_to_ton_rate, 6)

            # Floor в Stars → TON
            floor_stars = None
            for fk in ("floor_price", "floorPrice", "floor"):
                v = g.get(fk)
                if v is not None:
                    floor_stars = _safe_float(v)
                    break
            floor_ton = round(floor_stars * stars_to_ton_rate, 6) if (floor_stars and floor_stars > 0) else None

            rarity_raw = _extract_str(g, "rarity", "rarityLevel")
            rarity = rarity_raw.capitalize() if rarity_raw else ""

            image_url = _extract_image(g)

            results.append({
                "id": gift_id,
                "name": name,
                "slug": full_slug,
                "number": number,
                "price": price_ton,            # TON (сконвертировано)
                "price_ton": price_ton,         # TON (алиас)
                "stars_price": stars_price,     # Stars (оригинал для справки)
                "floor_price": floor_ton,       # TON (сконвертировано)
                "floor_stars": floor_stars,     # Stars (оригинал)
                "rarity": rarity,
                "currency": "TON",              # Всегда TON после конвертации
                "url": fragment_url,
                "image_url": image_url,
                "market": "fragment",
            })

    except Exception as e:
        logger.error(f"Ошибка при парсинге Fragment JSON: {e}", exc_info=True)

    return results


# ─── Fragment HTML scraping (актуальный способ) ─────────────────────────────

# Regex для извлечения отдельных gift-блоков с публичной HTML-страницы.
# Структура (типичная):
#   <a href="/gift/<slug>?...">
#     <span class="item-name">Eternal Rose</span>
#     <span class="item-num">&nbsp;#42</span>
#     <div class="tm-grid-item-value tm-value icon-before icon-(ton|star)">25</div>
#     <div class="tm-grid-item-status tm-status-avail">For sale</div>
#   </a>
_RE_FRAGMENT_BLOCK = re.compile(
    r'<a\s+href="/gift/([a-z0-9-]+)[^"]*"\s+class="tm-grid-item"\s*>'
    r'(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_RE_FRAGMENT_NAME   = re.compile(r'class="item-name"\s*>([^<]+)<', re.IGNORECASE)
_RE_FRAGMENT_NUM    = re.compile(r'class="item-num"\s*>[^#]*#?([\d]+)<', re.IGNORECASE)
_RE_FRAGMENT_PRICE  = re.compile(
    r'class="tm-grid-item-value[^"]*\bicon-(ton|star)\b[^"]*"\s*>\s*([\d.,\u00a0\s]+)\s*<',
    re.IGNORECASE,
)
_RE_FRAGMENT_STATUS = re.compile(
    r'class="tm-grid-item-status\s+tm-status-(\w+)"\s*>([^<]+)<',
    re.IGNORECASE,
)
_RE_FRAGMENT_THUMB  = re.compile(
    r'src="(https?://nft\.fragment\.com/[^"]+)"',
    re.IGNORECASE,
)


def parse_fragment_html(html: str, stars_to_ton_rate: float = 0.004) -> list:
    """
    Парсит публичную HTML-страницу Fragment.com (https://fragment.com/gifts).
    Возвращает список словарей с ключами совместимыми с parse_fragment_json.

    Цены могут быть в TON (icon-ton) или Stars (icon-star).
    Stars конвертируются в TON через stars_to_ton_rate.
    """
    results: list = []
    if not html or not isinstance(html, str):
        return results

    try:
        for match in _RE_FRAGMENT_BLOCK.finditer(html):
            slug = match.group(1)
            block = match.group(2)

            name_m = _RE_FRAGMENT_NAME.search(block)
            num_m = _RE_FRAGMENT_NUM.search(block)
            price_m = _RE_FRAGMENT_PRICE.search(block)
            status_m = _RE_FRAGMENT_STATUS.search(block)
            thumb_m = _RE_FRAGMENT_THUMB.search(block)

            # Принимаем только лоты в продаже
            status_class = (status_m.group(1).lower() if status_m else "")
            status_text = (status_m.group(2).strip() if status_m else "")
            is_for_sale = (
                status_class in {"avail", "available", "sale", "on_sale"}
                or "sale" in status_text.lower()
            )
            if not is_for_sale:
                continue

            if not price_m:
                continue

            currency_class = price_m.group(1).lower()  # "ton" | "star"
            raw_price_str = price_m.group(2).strip()
            price_val = _parse_html_number(raw_price_str)
            if price_val is None or price_val <= 0:
                continue

            name = (name_m.group(1).strip() if name_m else None) or _slug_to_name(slug)
            number = num_m.group(1).strip() if num_m else ""

            # Извлекаем номер из slug если не нашли в HTML
            if not number:
                slug_num_m = re.search(r"-(\d+)$", slug)
                if slug_num_m:
                    number = slug_num_m.group(1)

            # gift_id: используем slug как уникальный ID
            gift_id = slug

            stars_price: float | None = None
            floor_stars: float | None = None
            if currency_class == "ton":
                price_ton = round(price_val, 6)
            else:
                # Stars → TON
                stars_price = float(price_val)
                price_ton = round(stars_price * stars_to_ton_rate, 6)

            results.append({
                "id": gift_id,
                "name": name,
                "slug": slug,
                "number": number,
                "price": price_ton,
                "price_ton": price_ton,
                "stars_price": stars_price,
                "floor_price": None,
                "floor_stars": floor_stars,
                "rarity": "",
                "currency": "TON",
                "url": f"https://fragment.com/gift/{slug}",
                "image_url": thumb_m.group(1) if thumb_m else "",
                "market": "fragment",
            })

    except Exception as e:
        logger.error(f"Ошибка при парсинге Fragment HTML: {e}", exc_info=True)

    return results


def _parse_html_number(s: str) -> Optional[float]:
    """Парсит число из HTML (поддерживает пробелы, NBSP, запятые)."""
    if not s:
        return None
    cleaned = re.sub(r"[\s\u00a0',]", "", s.strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _slug_to_name(slug: str) -> str:
    """Eternal-rose-42 → Eternal Rose."""
    if not slug:
        return ""
    parts = slug.split("-")
    # Убираем последний сегмент, если это номер
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts) if parts else slug


# ─── GetGems / Portals (наноТОН → TON) ──────────────────────────────────────

def parse_portals_graphql(json_data: dict) -> list:
    """
    Парсит ответ GraphQL API GetGems (Portals).
    Цены в наноТОН (nanoton) → делим на 1e9 → TON.
    """
    results = []
    try:
        items = (
            json_data.get("data", {})
            .get("alphaSearch", {})
            .get("items", [])
        )
        if not isinstance(items, list):
            return results

        for item in items:
            if not isinstance(item, dict):
                continue

            address = _extract_str(item, "address")
            if not address:
                continue

            gift_name = _extract_str(item, "name", default="Unknown")

            sale = item.get("sale") or {}
            full_price_raw = sale.get("fullPrice") or sale.get("amount") or sale.get("price")
            if full_price_raw is None:
                continue

            price_nanoton = _safe_float(full_price_raw)
            if price_nanoton <= 0:
                continue
            price_ton = round(price_nanoton / 1e9, 6)

            floor_raw = sale.get("minPrice") or sale.get("floor_price")
            floor_ton = round(_safe_float(floor_raw) / 1e9, 6) if floor_raw else None
            if floor_ton is not None and floor_ton <= 0:
                floor_ton = None

            image_url = _extract_image(item)
            rarity_raw = _extract_str(item, "rarity", "rarityScore")

            results.append({
                "id": address,
                "name": gift_name,
                "slug": _extract_str(item, "slug"),
                "number": _extract_str(item, "index", "number"),
                "price": price_ton,
                "price_ton": price_ton,
                "stars_price": None,
                "floor_price": floor_ton,
                "rarity": rarity_raw.capitalize() if rarity_raw else "",
                "currency": "TON",
                "image_url": image_url,
                "market": "portals",
            })

    except Exception as e:
        logger.error(f"Ошибка при парсинге Portals GraphQL: {e}", exc_info=True)
    return results


# ─── Нормализация маркетов ────────────────────────────────────────────────────

_MARKET_ALIASES: dict[str, str] = {
    "mrkt":          "mrkt",
    "main_mrkt_bot": "mrkt",
    "fragment":      "fragment",
    "portals":       "portals",
    "getgems":       "portals",
}


def normalize_market(market: str) -> str:
    """Нормализует алиасы маркетов к каноническому виду."""
    return _MARKET_ALIASES.get(market.lower(), market)


# ─── Фильтр выгодности (всё в TON) ──────────────────────────────────────────

DEFAULT_S: dict = {
    "max_price_ton": 50.0,      # Макс. цена в TON (для ВСЕХ маркетов)
    "min_discount_pct": 0,      # Мин. скидка от Floor (%)
    "filter_rarity": [],        # Белый список редкостей
    "filter_markets": ["mrkt", "fragment"],  # Активные маркеты
    "notifications_on": True,
}


def is_profitable(gift_data: dict, market: str = "") -> bool:
    """
    Проверяет выгодность лота.
    ВСЕ ЦЕНЫ В TON.
      1. Маркет в белом списке?
      2. Цена в TON ниже лимита?
      3. Скидка от Floor достаточная?
      4. Редкость подходит?
    """
    try:
        from settings_store import load_settings
        s = load_settings()
    except Exception:
        s = DEFAULT_S.copy()

    # 1. Маркет активен?
    filter_markets = s.get("filter_markets", [])
    if filter_markets and market:
        market_norm = normalize_market(market)
        if not market.startswith("tg:") and market_norm not in filter_markets:
            return False

    # 2. Цена валидна?
    price = gift_data.get("price")
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        return False

    # 3. Цена ниже лимита? (всё в TON)
    max_price_ton = float(s.get("max_price_ton", 50.0))
    if price > max_price_ton:
        return False

    # 4. Скидка от Floor?
    floor = gift_data.get("floor_price")
    min_discount = int(s.get("min_discount_pct", 0))
    if min_discount > 0 and isinstance(floor, (int, float)) and floor > price:
        discount_pct = (floor - price) / floor * 100
        if discount_pct < min_discount:
            return False

    # 5. Редкость?
    rarity_filter = s.get("filter_rarity", [])
    if rarity_filter:
        rarity = gift_data.get("rarity", "")
        if not rarity:
            return False
        if rarity not in rarity_filter:
            return False

    return True


# ─── Форматирование цены ──────────────────────────────────────────────────────

def format_price(price: float, currency: str = "TON") -> str:
    """
    Форматирует цену в TON красиво.
    Все цены теперь в TON, currency оставлен для совместимости.

    Примеры:
      5.5 TON     → '5.50 💎'
      10.0 TON    → '10 💎'
      0.00123 TON → '0.001230 💎'
      0.5 TON     → '0.50 💎'
    """
    if price is None:
        return "? 💎"
    try:
        price = float(price)
    except (TypeError, ValueError):
        return "? 💎"

    if price >= 100:
        return f"{price:.1f} 💎"
    elif price >= 1:
        # Убираем лишние нули: 5.50, не 5.500
        s = f"{price:.2f}"
        return f"{s} 💎"
    elif price > 0:
        # Маленькие значения — 6 знаков
        s = f"{price:.6f}".rstrip("0")
        return f"{s} 💎"
    else:
        return "0 💎"


def format_stars(stars: float) -> str:
    """Форматирует Stars для справочного отображения."""
    if stars is None:
        return ""
    return f"{int(stars):,} ⭐".replace(",", " ")
