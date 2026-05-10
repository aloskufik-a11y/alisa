"""
URL Builder — правильные форматы ссылок на подарки.

Fragment.com:
  Конкретный подарок: https://fragment.com/gift/{collectionslug}-{number}
  Примеры: https://fragment.com/gift/eternalrose-42
           https://fragment.com/gift/plushpepe-1821
  Коллекция:          https://fragment.com/gifts/{collectionslug}?filter=sale&sort=price_asc

MRKT (mrkt.fun) — Telegram Mini App:
  Бот: @mrkt
  Mini App deep link на конкретный лот:
    https://t.me/mrkt/app?startapp={UUID_БЕЗ_ДЕФИСОВ}
  (этот формат используется самой MRKT в кнопке Share — JS:
   ${inGameLink}${gift.id.replace(/-/g,"")}, где
   inGameLink = "https://t.me/mrkt/app?startapp=", а gift.id — UUID лота)

  На входе функции `build_mrkt_gift_link`:
    gift_id ожидаем UUID (8-4-4-4-12 hex). Дефисы убираем — получаем
    32-символьный hex, который сервер MRKT парсит и возвращает в auth-ответе
    как s.giftId, после чего фронт открывает GIFT_OVERVIEW для этого лота.

  Telegram NFT (для апгрейженных гифтов): https://t.me/nft/{Name}-{Number}

Portals (Telegram Mini App, portals-market.com):
  Mini App deep link: https://t.me/portals/market?startapp={slug_or_id}
  Веб:                https://portals-market.com/

GetGems (старые NFT Telegram):
  NFT:  https://getgems.io/nft/{address}
  Gift: https://getgems.io/gift/{slug}
"""

import re

# UUID без дефисов: 32 hex-символа
_UUID_NO_DASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
# UUID канонический: 8-4-4-4-12
_UUID_DASH_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_mrkt_uuid(value: str) -> str:
    """
    Возвращает MRKT-совместимый идентификатор лота:
      'aaaa...-bbbb-...' → 'aaaabbbb...' (32 hex без дефисов).
    Если на вход дали уже 32 hex — оставляем как есть.
    Если строка не похожа на UUID — возвращаем пустоту.
    """
    if not value:
        return ""
    v = value.strip()
    if _UUID_DASH_RE.match(v):
        return v.replace("-", "").lower()
    if _UUID_NO_DASH_RE.match(v):
        return v.lower()
    return ""


def _fragment_collection_slug(name: str) -> str:
    """
    Превращает название коллекции в slug для Fragment URL.
    'Eternal Rose' -> 'eternalrose'
    'Plush Pepe'   -> 'plushpepe'
    'Swiss Watch'  -> 'swisswatch'
    'B-Day Candle' -> 'bdaycandle'
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_fragment_gift_link(gift_id: str, slug: str = "", name: str = "", number: str = "") -> str:
    """
    Строит прямую ссылку на конкретный подарок на Fragment.com.

    Fragment URL: https://fragment.com/gift/{collectionslug}-{number}
    - collectionslug = имя без пробелов/спецсимволов ('eternalrose')
    - number = порядковый номер в коллекции (НЕ database ID)

    Приоритет:
      1. slug уже в формате 'collectionname-NNN' → используем напрямую
      2. name + number → строим slug
      3. slug без числа → fallback
      4. ничего → страница подарков
    """
    # 1. Уже готовый slug 'collectionname-number'
    if slug and re.search(r'-\d+$', slug):
        return f"https://fragment.com/gift/{slug}"

    # 2. Есть название коллекции + номер экземпляра
    if name and number:
        col_slug = _fragment_collection_slug(name)
        return f"https://fragment.com/gift/{col_slug}-{number}"

    # 3. Slug без числа
    if slug:
        return f"https://fragment.com/gift/{slug}"

    # 4. Числовой ID → Fragment его не поддерживает в URL
    return "https://fragment.com/gifts"


def build_fragment_collection_link(name: str) -> str:
    """
    Ссылка на страницу коллекции (список всех экземпляров на продаже).
    'Eternal Rose' -> 'https://fragment.com/gifts/eternalrose?filter=sale&sort=price_asc'
    """
    slug = _fragment_collection_slug(name)
    return f"https://fragment.com/gifts/{slug}?filter=sale&sort=price_asc"


def build_mrkt_gift_link(gift_id: str, slug: str = "", name: str = "", number: str = "") -> str:
    """
    Строит deep link для MRKT Mini App, открывающий КОНКРЕТНЫЙ лот.

    Реальный формат, который использует сама MRKT в кнопке Share:
      https://t.me/mrkt/app?startapp={UUID без дефисов}

    Сервер MRKT парсит startapp, по 32-символьному hex распознаёт UUID лота
    и возвращает его в auth-ответе как `s.giftId`. Фронт после этого вызывает
    `getGiftInfo(giftId)` (GET /api/v1/gifts/gift/{UUID}) и открывает
    `GIFT_OVERVIEW` модал на конкретном лоте.

    Старый формат `?startapp=ChillFlame-326040` (CamelCase-Number) сервер
    НЕ распознаёт ⇒ открывалась главная страница MRKT. Это исправлено.

    Приоритеты:
      1. gift_id — UUID лота (нативный для MRKT)
      2. slug — фолбэк, имя коллекции в нижнем регистре открывает фильтр
         коллекции в маркете (e.g. ?startapp=chillflame)
      3. ничего → главная MRKT
    """
    uuid_clean = _normalize_mrkt_uuid(gift_id)
    if uuid_clean:
        return f"https://t.me/mrkt/app?startapp={uuid_clean}"

    # Фолбэк: имя коллекции в нижнем регистре открывает фильтр
    # (mrkt.openPage поддерживает: chillflame, vicecream, plinko, rocket, ...)
    if name:
        page_slug = re.sub(r"[^a-z0-9]", "", name.lower())
        if page_slug:
            return f"https://t.me/mrkt/app?startapp={page_slug}"

    # Если есть какой-то slug API — используем как есть
    if slug:
        return f"https://t.me/mrkt/app?startapp={slug}"

    # Последний фолбэк — просто открыть MRKT
    return "https://t.me/mrkt/app"


def build_mrkt_web_link(slug: str = "", gift_id: str = "") -> str:
    """
    Веб-версия MRKT.

    NB: mrkt.fun — это лишь лендинг с CTA «Open Mini App», у него НЕТ
    публичных URL вида /gift/{id} (мы это проверили: 404).
    Поэтому ведём в Mini App тем же способом — через t.me/mrkt/app.
    """
    uuid_clean = _normalize_mrkt_uuid(gift_id)
    if uuid_clean:
        return f"https://t.me/mrkt/app?startapp={uuid_clean}"
    if slug:
        page_slug = re.sub(r"[^a-z0-9]", "", slug.lower())
        if page_slug:
            return f"https://t.me/mrkt/app?startapp={page_slug}"
    return "https://t.me/mrkt/app"


def build_telegram_nft_link(name: str, number: str) -> str:
    """
    Прямая ссылка на гифт в нативном Telegram NFT интерфейсе:
      https://t.me/nft/{Name}-{Number}
    Работает для апгрейженных gift'ов (формат, который Telegram использует
    повсеместно для шеринга NFT-подарков).
    """
    if not (name and number):
        return ""
    # Имя коллекции без пробелов: "Vice Cream" → "ViceCream"
    name_camel = re.sub(r"\s+", "", name.strip())
    return f"https://t.me/nft/{name_camel}-{number}"


def build_portals_gift_link(slug: str = "", gift_id: str = "") -> str:
    """
    Deep link на конкретный лот в Portals Mini App.

    Используется верифицированный короткий username @portals — он алиас
    для @portals_market_bot, оба пути ведут в один Mini App. В их prod JS:
        publickBotUrl = "https://t.me/portals_market_bot/market"
        link = `${publickBotUrl}?startapp=gift_${e.id}`
    Мы отдаём более понятную пользователю короткую версию — она отображается
    в Telegram как «Portals Market» с верифицированной галочкой.
    """
    param = (gift_id or slug or "").strip()
    if not param:
        return "https://t.me/portals/market"
    return f"https://t.me/portals/market?startapp=gift_{param}"


def build_getgems_gift_link(address: str, slug: str = "") -> str:
    """Ссылка на NFT-подарок на GetGems (Portals)."""
    if slug:
        return f"https://getgems.io/gift/{slug}"
    return f"https://getgems.io/nft/{address}"


def get_market_label(market: str) -> str:
    """Читаемое название маркета с иконкой."""
    labels = {
        "mrkt": "🟣 MRKT (mrkt.fun)",
        "main_mrkt_bot": "🟣 MRKT (mrkt.fun)",
        "fragment": "🔵 Fragment.com",
        "portals": "🟢 GetGems (Portals)",
        "getgems": "🟢 GetGems",
    }
    return labels.get(market, f"🏪 {market}")


def build_market_buttons(
    market: str,
    gift_id: str,
    slug: str = "",
    name: str = "",
    number: str = "",
) -> list[dict]:
    """
    Возвращает список кнопок {text, url} для aiogram InlineKeyboard.
    Всегда даёт рабочую ссылку + fallback на коллекцию.
    """
    buttons = []

    if market in ("mrkt", "main_mrkt_bot"):
        # Основная Mini App ссылка на КОНКРЕТНЫЙ лот
        tg_link = build_mrkt_gift_link(gift_id, slug, name, number)
        buttons.append({"text": "🟣 Открыть в MRKT", "url": tg_link})
        # Дополнительная: посмотреть тот же gift в Telegram NFT
        nft_link = build_telegram_nft_link(name, number)
        if nft_link:
            buttons.append({"text": "🎁 Telegram NFT", "url": nft_link})

    elif market == "fragment":
        gift_link = build_fragment_gift_link(gift_id, slug, name, number)
        buttons.append({"text": "🔵 Открыть на Fragment", "url": gift_link})
        if name:
            col_link = build_fragment_collection_link(name)
            buttons.append({"text": "📋 Все лоты коллекции", "url": col_link})

    elif market == "portals":
        # Родной Mini App Portals (полный share-формат с gift_{UUID})
        portals_link = build_portals_gift_link(slug=slug, gift_id=gift_id)
        buttons.append({"text": "🟢 Открыть в Portals", "url": portals_link})
        # Дополнительная — t.me/nft при наличии name+number (нативный TG-просмотр)
        nft_link = build_telegram_nft_link(name, number)
        if nft_link:
            buttons.append({"text": "🎁 Telegram NFT", "url": nft_link})

    elif market == "getgems":
        # Getgems offchain-гифт. address передаётся как gift_id, slug — это
        # collectionAddress.
        getgems_link = build_getgems_gift_link(gift_id, slug)
        buttons.append({"text": "💎 Открыть в Getgems", "url": getgems_link})
        nft_link = build_telegram_nft_link(name, number)
        if nft_link:
            buttons.append({"text": "🎁 Telegram NFT", "url": nft_link})

    return buttons
