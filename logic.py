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

_NANO = 1_000_000_000  # 1 TON = 1e9 nanoTON


def _nano_to_ton(value) -> Optional[float]:
    """Конвертирует nanoTON → TON. None или 0 → None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return round(v / _NANO, 6)


# ─── Цвета: преобразование RGB-int → монохромность ───────────────────────────

def _rgb_int_to_hsl(rgb_int: int) -> tuple[float, float, float]:
    """
    MRKT хранит цвета как одно 24-битное int значение (например 9015944 = 0x898E08).
    Возвращает (H в [0..360), S в [0..1], L в [0..1]).
    """
    r = ((rgb_int >> 16) & 0xFF) / 255.0
    g = ((rgb_int >> 8) & 0xFF) / 255.0
    b = (rgb_int & 0xFF) / 255.0
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    delta = cmax - cmin
    L = (cmax + cmin) / 2.0
    if delta == 0:
        H = 0.0
    elif cmax == r:
        H = ((g - b) / delta) % 6
    elif cmax == g:
        H = ((b - r) / delta) + 2
    else:
        H = ((r - g) / delta) + 4
    H *= 60.0
    if H < 0:
        H += 360.0
    S = 0.0 if delta == 0 else delta / (1 - abs(2 * L - 1))
    return H, S, L


def _hue_distance(h1: float, h2: float) -> float:
    """Кратчайшее расстояние между двумя hue (0..180)."""
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def is_monochrome(rgb_ints: list[int], hue_threshold: float = 25.0) -> bool:
    """
    Проверяет, что переданные RGB-int цвета лежат в одном цветовом диапазоне.

    Алгоритм: если у всех цветов почти нулевая насыщенность (<0.15) — это
    серая палитра, считаем монохромом. Иначе все hue должны быть в пределах
    `hue_threshold` градусов друг от друга.

    `rgb_ints` — список (обычно 2-4) целых RGB значений.
    """
    if not rgb_ints or len(rgb_ints) < 2:
        return False
    hsl = []
    for v in rgb_ints:
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        hsl.append(_rgb_int_to_hsl(iv))
    if len(hsl) < 2:
        return False
    sats = [s for _, s, _ in hsl]
    # Если все цвета почти серые — монохром (grayscale)
    if max(sats) < 0.15:
        return True
    # Игнорируем серые "акценты" — берём только насыщенные оттенки
    coloured_hues = [h for h, s, _ in hsl if s >= 0.15]
    if not coloured_hues:
        return True
    h0 = coloured_hues[0]
    return all(_hue_distance(h0, h) <= hue_threshold for h in coloured_hues[1:])


# ─── Номера подарков: low/round/repeat/lucky/twin ────────────────────────────

def number_categories(num) -> set[str]:
    """
    Возвращает set категорий, которым соответствует номер подарка.

    Категории:
      'low'       — #1-#999 (трёхзначные включительно)
      'sub100'    — #1-#99
      'round'     — степени 10 (10, 100, 1000, 10000, 100000)
      'pretty100' — кратные 100 (100, 200, 300, 1500, 2000…)
      'repeat'    — все цифры одинаковые (777, 11111, 999999)
      'lucky'     — содержит только 7 (7, 77, 777…)
      'sequential'— цифры идут по возрастанию или убыванию (123, 1234, 9876)
      'palindrome'— читается одинаково в обе стороны (121, 12321)
    """
    cats: set[str] = set()
    try:
        n = int(num)
    except (TypeError, ValueError):
        return cats
    if n <= 0:
        return cats
    s = str(n)
    if n <= 99:
        cats.add("sub100")
    if n <= 999:
        cats.add("low")
    # round = 10**k
    if n in (10, 100, 1000, 10_000, 100_000, 1_000_000):
        cats.add("round")
    if n >= 100 and n % 100 == 0:
        cats.add("pretty100")
    # repeat — все цифры одинаковые. Для одноцифренных номеров ставить repeat
    # бессмысленно (там нет «повтора»), требуем длину ≥2.
    if len(s) >= 2 and len(set(s)) == 1:
        cats.add("repeat")
    if set(s) == {"7"}:
        cats.add("lucky")
    if len(s) >= 3:
        asc = all(int(s[i]) + 1 == int(s[i + 1]) for i in range(len(s) - 1))
        desc = all(int(s[i]) - 1 == int(s[i + 1]) for i in range(len(s) - 1))
        if asc or desc:
            cats.add("sequential")
    # Палиндромом считаем только нетривиальные случаи: длина ≥3 И не все
    # цифры одинаковые. Иначе #777 / #11111 уходили бы и в repeat и в palindrome,
    # хотя пользователю это две разные категории по смыслу.
    if len(s) >= 3 and s == s[::-1] and len(set(s)) > 1:
        cats.add("palindrome")
    return cats


_NUM_FILTER_LABELS = {
    "low":        "💯 Низкий (#1-#999)",
    "sub100":     "🥇 Топ-100 (#1-#99)",
    "round":      "⭕ Круглый (10/100/1000…)",
    "pretty100":  "🔢 Кратный 100",
    "repeat":     "🔁 Повтор (777, 11111…)",
    "lucky":      "🍀 Lucky (777, 7777…)",
    "sequential": "↗️ Последовательный (123, 4321)",
    "palindrome": "🔄 Палиндром (121, 12321)",
}


def number_filter_label(cat: str) -> str:
    """Человекочитаемое имя категории номеров для UI."""
    return _NUM_FILTER_LABELS.get(cat, cat)


def all_number_filter_categories() -> list[str]:
    """Канонический порядок категорий номеров для UI."""
    return list(_NUM_FILTER_LABELS.keys())


def parse_mrkt_json(json_data: dict) -> list:
    """
    Парсит ответ MRKT API /api/v1/gifts/saling.

    Реальная структура item (по факту):
      {
        "id": "uuid",
        "name": "ViceCream-258228",          ← внутренний slug, НЕ показывать
        "title": "Vice Cream",                ← коллекция (cosmetic)
        "collectionName": "Vice Cream",       ← коллекция (canonical)
        "collectionTitle": "Vice Cream",      ← pretty
        "modelName": "Pine Cone",             ← вариант модели
        "number": 258228,
        "salePrice": 2630000000,              ← цена в nanoTON (÷ 1e9 = TON)
        "salePriceWithoutFee": 2630000000,
        "floorPriceNanoTONsByCollection": null,
        "floorPriceNanoTONsByBackdropModel": null,
        "isOnSale": true,
        "modelStickerKey": "...",
        "modelStickerThumbnailKey": "...",
        ...
      }
    """
    results = []
    try:
        items = None
        if isinstance(json_data, list):
            items = json_data
        elif isinstance(json_data, dict):
            for key in ("gifts", "items", "data", "results", "list"):
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

            # Только реально продающиеся
            if item.get("isOnSale") is False:
                continue

            gift_id = _extract_str(item, "id", "gift_id", "_id", "uuid")
            if not gift_id:
                continue

            # Имя коллекции — для группировки и отображения
            gift_name = _extract_str(
                item,
                "collectionTitle", "collectionName", "title",
                "gift_name", default="",
            )
            # Запасной вариант: name это slug вида "ViceCream-258228"
            if not gift_name:
                raw_name = _extract_str(item, "name", default="")
                if raw_name:
                    gift_name = re.sub(r"[-_]\d+$", "", raw_name)
                    # CamelCase → "Camel Case"
                    gift_name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", gift_name).strip()
                else:
                    gift_name = "Unknown"

            model_name = _extract_str(item, "modelName", "modelTitle", default="")

            # Slug / alias — для url_builder
            api_slug = _extract_str(item, "slug", "alias")
            if not api_slug:
                # Реконструируем из name+number
                base = (gift_name or "").lower().replace(" ", "-").replace("_", "-")
                num = _extract_str(item, "number", default="")
                api_slug = f"{base}-{num}" if base and num else (base or gift_id)

            number = _extract_str(item, "number", "num", "edition", "index")

            # Цена salePrice в nanoTON → TON
            sale_price_nano = item.get("salePrice")
            if sale_price_nano is None:
                sale_price_nano = item.get("salePriceWithoutFee")
            price = _nano_to_ton(sale_price_nano)

            if price is None:
                # Запасной путь: вдруг отдают цену в TON напрямую
                price = _extract_price(item, "price", "price_ton", "priceTon", "amount")

            if price is None:
                logger.debug(f"MRKT: пропускаем {gift_id} — нет цены")
                continue

            # Floor в nanoTON → TON. Берём наименьший доступный (по backdrop+model — точнее)
            floor_bm = _nano_to_ton(item.get("floorPriceNanoTONsByBackdropModel"))
            floor_col = _nano_to_ton(item.get("floorPriceNanoTONsByCollection"))
            floor = floor_bm or floor_col

            # Запасной вариант — старые ключи
            if floor is None:
                for fk in ("floor_price", "floorPrice", "floor", "min_price", "minPrice"):
                    v = item.get(fk)
                    if v is not None:
                        floor = round(_safe_float(v), 6)
                        if floor and floor > 0:
                            break
                        floor = None

            rarity_raw = _extract_str(
                item,
                "rarity", "rarityLevel", "rarity_name", "tier",
                "modelRarityName", "backdropRarityName",
            )
            rarity = rarity_raw.capitalize() if rarity_raw else ""

            image_url = _extract_image(item)
            total_count = item.get("total_count") or item.get("totalCount") \
                or item.get("totalUpgradedCount") or item.get("maxUpgradedCount") or 0

            # Stars (если когда-нибудь будет в API)
            stars_price_raw = item.get("stars_price") or item.get("starsPrice")
            stars_price = _safe_float(stars_price_raw) if stars_price_raw else None

            backdrop_name = _extract_str(item, "backdropName", default="")
            symbol_name = _extract_str(item, "symbolName", default="")

            # Цвета backdrop'а — для детекта монохромности
            colors = []
            for ck in (
                "backdropColorsCenterColor",
                "backdropColorsEdgeColor",
                "backdropColorsSymbolColor",
                "backdropColorsTextColor",
            ):
                cv = item.get(ck)
                if cv is not None:
                    try:
                        colors.append(int(cv))
                    except (TypeError, ValueError):
                        pass

            # Per-mille rarities (вес атрибута, чем меньше — тем реже)
            def _pm(v):
                if v is None:
                    return None
                try:
                    fv = float(v)
                    return fv if fv > 0 else None
                except (TypeError, ValueError):
                    return None

            rarities_pm = {
                "model": _pm(item.get("modelRarityPerMille")),
                "backdrop": _pm(item.get("backdropRarityPerMille")),
                "symbol": _pm(item.get("symbolRarityPerMille")),
            }

            results.append({
                "id": gift_id,
                "name": gift_name,
                "model_name": model_name,
                "backdrop_name": backdrop_name,
                "symbol_name": symbol_name,
                "colors": colors,                  # list[int] для is_monochrome()
                "rarities_pm": rarities_pm,        # {model, backdrop, symbol} per-mille
                "slug": api_slug,
                "number": number,
                "price": round(price, 6),          # TON
                "price_ton": round(price, 6),
                "stars_price": stars_price,
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


# ─── Portals Market (portal-market.com) ─────────────────────────────────────

def _portals_attr(attrs: list, attr_type: str) -> str:
    """Извлекает значение атрибута Portals по типу (model/backdrop/symbol)."""
    if not isinstance(attrs, list):
        return ""
    for a in attrs:
        if isinstance(a, dict) and a.get("type") == attr_type:
            return str(a.get("value") or "")
    return ""


def parse_portals_search(json_data: dict) -> list:
    """
    Парсит ответ Portals search API: GET /api/nfts/search

    Реальная структура item (по факту):
      {
        "id": "uuid",
        "tg_id": "IceCream-53559",
        "collection_id": "uuid",
        "external_collection_number": 53559,
        "name": "Ice Cream",                    ← коллекция
        "photo_url": "...",
        "price": "6.29",                          ← string, TON
        "floor_price": "2.94",                    ← string, TON (отдаётся API!)
        "listed_at": "2026-05-02T15:20:46Z",
        "status": "listed",
        "attributes": [
          {"type": "model", "value": "...", "rarity_per_mille": 3},
          {"type": "backdrop", "value": "...", "rarity_per_mille": 1},
          {"type": "symbol", "value": "...", "rarity_per_mille": 0.4}
        ]
      }
    """
    results: list = []
    try:
        items = json_data.get("results", []) if isinstance(json_data, dict) else []
        if not isinstance(items, list):
            return results

        for item in items:
            if not isinstance(item, dict):
                continue

            # Только реально продающиеся
            status = (item.get("status") or "").lower()
            if status and status != "listed":
                continue

            gift_id = _extract_str(item, "id", "uuid")
            if not gift_id:
                continue

            gift_name = _extract_str(item, "name", default="Unknown")
            number = _extract_str(item, "external_collection_number", "number", "index")

            # Цена и floor — строки в TON
            price = _safe_float(item.get("price"))
            if price <= 0:
                continue

            floor_raw = item.get("floor_price")
            floor = _safe_float(floor_raw) if floor_raw is not None else 0.0
            floor_ton = round(floor, 6) if floor > 0 else None

            attrs = item.get("attributes", [])
            model = _portals_attr(attrs, "model")
            backdrop = _portals_attr(attrs, "backdrop")
            symbol = _portals_attr(attrs, "symbol")

            # Редкость: используем минимальный rarity_per_mille как индикатор
            rarities = [
                a.get("rarity_per_mille", 1000)
                for a in attrs if isinstance(a, dict) and a.get("rarity_per_mille") is not None
            ]
            min_rarity_per_mille = min(rarities) if rarities else None
            if min_rarity_per_mille is not None and min_rarity_per_mille < 1:
                rarity = "Legendary"
            elif min_rarity_per_mille is not None and min_rarity_per_mille < 5:
                rarity = "Epic"
            elif min_rarity_per_mille is not None and min_rarity_per_mille < 30:
                rarity = "Rare"
            elif min_rarity_per_mille is not None and min_rarity_per_mille < 100:
                rarity = "Uncommon"
            elif min_rarity_per_mille is not None:
                rarity = "Common"
            else:
                rarity = ""

            # Slug для url_builder
            tg_id = _extract_str(item, "tg_id", default="")
            slug = tg_id.lower().replace(" ", "") if tg_id else \
                f"{gift_name.lower().replace(' ', '')}-{number}" if gift_name and number else gift_id

            # Per-mille rarities раздельно (для UI/фильтров)
            rarities_pm: dict[str, float | None] = {"model": None, "backdrop": None, "symbol": None}
            for a in attrs:
                if not isinstance(a, dict):
                    continue
                t = a.get("type")
                pm = a.get("rarity_per_mille")
                if t in rarities_pm and pm is not None:
                    try:
                        fv = float(pm)
                        if fv > 0:
                            rarities_pm[t] = fv
                    except (TypeError, ValueError):
                        pass

            results.append({
                "id": gift_id,
                "name": gift_name,
                "model_name": model,
                "backdrop_name": backdrop,
                "symbol_name": symbol,
                "colors": [],                       # Portals не отдаёт цвета backdrop'а
                "rarities_pm": rarities_pm,
                "slug": slug,
                "number": number,
                "price": round(price, 6),
                "price_ton": round(price, 6),
                "stars_price": None,
                "floor_price": floor_ton,
                "rarity": rarity,
                "currency": "TON",
                "image_url": _extract_str(item, "photo_url", "image_url"),
                "url": f"https://t.me/portals/market?startapp=gift_{gift_id}",
                "market": "portals",
            })

    except Exception as e:
        logger.error(f"Ошибка при парсинге Portals search: {e}", exc_info=True)
    return results


# ─── GetGems / Portals (наноТОН → TON) — устаревший GraphQL ─────────────────

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
    "max_price_ton": 50.0,         # Макс. цена в TON (абсолютный потолок)
    "min_price_ton": 0.0,          # Мин. цена в TON (нижний порог; 0 = без ограничения)
    "floor_tolerance_pct": 0.0,    # Сколько % сверху от floor допускать (0 = только floor)
    "min_discount_pct": 0,         # Доп. фильтр: мин. скидка от Floor (%)
    "require_floor": True,         # Если True — без known floor лот не выгоден
    "filter_rarity": [],           # Белый список редкостей
    "filter_markets": ["mrkt", "fragment", "portals"],
    "filter_collections": [],      # Если непусто — алертим только эти коллекции (по name)
    "monochrome_only": False,      # Только лоты с монохромным backdrop (одного цвета)
    "number_filters": [],          # ['low','sub100','round','repeat','lucky',...] OR-логика
    "max_rarity_pm": 0,            # Если >0 — атрибут с rarity_per_mille ≤ этого считается редким
    "notifications_on": True,

    # ── Уведомления по маркетам ────────────────────────────────────────────
    "mrkt_alerts_on": True,
    "fragment_alerts_on": True,
    "portals_alerts_on": True,

    # ── Тихие часы (UTC). 0-0 = выключено. Пример: 22-7 = с 22:00 до 07:00 UTC ─
    "quiet_hours_start": 0,
    "quiet_hours_end":   0,

    # ── Ограничение алертов в одном цикле опроса (per market). 0 = без лимита.
    "max_alerts_per_cycle": 0,

    # ── Режим "редкие свежие листинги" — алертит даже если price > floor,
    #    если у лота есть редкий атрибут (≤ recent_rare_pm) и он впервые виден.
    "recent_rare_mode": False,
    "recent_rare_pm":   5.0,

    # Mini App URL (для кнопки в /settings)
    "mini_app_url": "",
}


def compute_floors(gifts: list[dict], key: str = "name") -> dict[str, float]:
    """
    Считает floor (минимальную цену в TON) по группам ключа `key`.
    Каждый gift должен содержать ключи `name` (или то, что указано в `key`)
    и `price` (TON). Возвращает {name: floor_ton}.
    """
    floors: dict[str, float] = {}
    for g in gifts:
        if not isinstance(g, dict):
            continue
        name = g.get(key)
        price = g.get("price")
        if not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        cur = floors.get(name)
        if cur is None or price < cur:
            floors[name] = float(price)
    return floors


def apply_floors(gifts: list[dict], key: str = "name") -> list[dict]:
    """
    Считает floor по batch и записывает в каждый gift как `floor_price`,
    если у gift нет своего floor (None или 0). Возвращает тот же список
    (мутирует элементы in-place).
    """
    floors = compute_floors(gifts, key=key)
    for g in gifts:
        if not isinstance(g, dict):
            continue
        existing = g.get("floor_price")
        if existing is None or (isinstance(existing, (int, float)) and existing <= 0):
            name = g.get(key)
            if name and name in floors:
                g["floor_price"] = floors[name]
    return gifts


def _in_quiet_hours(s: dict) -> bool:
    """
    Возвращает True, если сейчас (UTC) попадает в окно тихих часов.
    Окно [start, end). Если start == end → выключено. Поддерживает переход
    через полночь, например start=22, end=7 → тихо с 22:00 до 07:00 UTC.
    """
    try:
        start = int(s.get("quiet_hours_start", 0) or 0)
        end = int(s.get("quiet_hours_end", 0) or 0)
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    if not (0 <= start <= 23) or not (0 <= end <= 23):
        return False
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if start < end:
        return start <= h < end
    # переход через полночь
    return h >= start or h < end


def is_profitable(gift_data: dict, market: str = "") -> bool:
    """
    Проверяет, выгодный ли лот. Все цены в TON.

    Цепочка проверок:
      1. Маркет в белом списке.
      2. Цена валидна, лежит в [min_price_ton, max_price_ton].
      3. Floor-aware: цена ≤ floor × (1 + floor_tolerance_pct / 100).
         Если require_floor=True и floor неизвестен — НЕ выгодно.
      4. min_discount_pct (опционально, доп. ограничение от floor).
      5. Редкость в белом списке (если задан).
      6. Коллекция в белом списке (если задан).
      7. number_filters (если непустой) — номер должен совпасть хотя бы с одной
         категорией: low / sub100 / round / repeat / lucky / sequential / palindrome.
      8. monochrome_only — backdrop одного цветового семейства.
      9. max_rarity_pm — хотя бы один атрибут (model/backdrop/symbol) ≤ этого порога.
    """
    try:
        from settings_store import load_settings
        s = load_settings()
    except Exception:
        s = DEFAULT_S.copy()

    # 1. Маркет активен?
    filter_markets = s.get("filter_markets", [])
    market_norm = normalize_market(market) if market else ""
    if filter_markets and market:
        if not market.startswith("tg:") and market_norm not in filter_markets:
            return False

    # 1a. Per-market алерты
    per_market_key = {
        "mrkt": "mrkt_alerts_on",
        "fragment": "fragment_alerts_on",
        "portals": "portals_alerts_on",
    }.get(market_norm)
    if per_market_key and not bool(s.get(per_market_key, True)):
        return False

    # 1b. Глобальные уведомления
    if not bool(s.get("notifications_on", True)):
        return False

    # 1c. Тихие часы (UTC)
    if _in_quiet_hours(s):
        return False

    # 2. Цена валидна?
    price = gift_data.get("price")
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        return False

    # 2a. Цена в диапазоне [min_price_ton, max_price_ton]
    min_price_ton = float(s.get("min_price_ton", 0.0) or 0.0)
    if min_price_ton > 0 and price < min_price_ton:
        return False

    max_price_ton = float(s.get("max_price_ton", DEFAULT_S["max_price_ton"]))
    if max_price_ton > 0 and price > max_price_ton:
        return False

    # 3. Floor-aware: лот должен быть на полу или у пола
    floor = gift_data.get("floor_price")
    floor_valid = isinstance(floor, (int, float)) and floor > 0
    require_floor = bool(s.get("require_floor", DEFAULT_S["require_floor"]))
    floor_tolerance_pct = float(s.get("floor_tolerance_pct", DEFAULT_S["floor_tolerance_pct"]))

    # Режим "редкие свежие листинги": если активен И у лота есть редкий
    # атрибут — мы пропускаем floor-проверку (это альтернативный путь к alert).
    recent_rare_mode = bool(s.get("recent_rare_mode", False))
    recent_rare_pm = float(s.get("recent_rare_pm", 5.0))
    is_rare_listing = False
    if recent_rare_mode and recent_rare_pm > 0:
        rar = gift_data.get("rarities_pm") or {}
        for v in rar.values():
            if isinstance(v, (int, float)) and 0 < v <= recent_rare_pm:
                is_rare_listing = True
                break

    if not is_rare_listing:
        if floor_valid:
            max_allowed = float(floor) * (1.0 + floor_tolerance_pct / 100.0)
            # +0.000001 запас на ошибки округления
            if price > max_allowed + 1e-6:
                return False
        elif require_floor:
            # Без known floor мы не можем гарантировать выгодность
            return False

    # 4. Минимальная скидка от floor (если требуется)
    min_discount = int(s.get("min_discount_pct", 0))
    if min_discount > 0:
        if not floor_valid:
            return False
        discount_pct = (float(floor) - price) / float(floor) * 100.0
        if discount_pct < min_discount:
            return False

    # 5. Редкость (текстовая)
    rarity_filter = s.get("filter_rarity", [])
    if rarity_filter:
        rarity = gift_data.get("rarity", "")
        if not rarity or rarity not in rarity_filter:
            return False

    # 6. Коллекция (если задан белый список)
    col_filter = s.get("filter_collections", [])
    if col_filter:
        gift_name = (gift_data.get("name") or "").strip()
        if not gift_name or gift_name not in col_filter:
            return False

    # 7. Фильтры по номеру подарка (OR)
    number_filters = s.get("number_filters", [])
    if number_filters:
        cats = number_categories(gift_data.get("number"))
        if not cats.intersection(number_filters):
            return False

    # 8. Монохромный backdrop
    if bool(s.get("monochrome_only", False)):
        colors = gift_data.get("colors") or []
        if not is_monochrome(colors):
            return False

    # 9. Редкий атрибут (минимальный per-mille)
    max_rarity_pm = float(s.get("max_rarity_pm", 0) or 0)
    if max_rarity_pm > 0:
        rar_pm = gift_data.get("rarities_pm") or {}
        # Хотя бы один из атрибутов должен быть ≤ порога
        ok = False
        for v in rar_pm.values():
            if v is not None and v <= max_rarity_pm:
                ok = True
                break
        if not ok:
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
