"""
URL Builder — правильные форматы ссылок на подарки.

Fragment.com:
  Конкретный подарок: https://fragment.com/gift/{collectionslug}-{number}
  Примеры: https://fragment.com/gift/eternalrose-42
           https://fragment.com/gift/plushpepe-1821
  Коллекция:          https://fragment.com/gifts/{collectionslug}?filter=sale&sort=price_asc

MRKT (mrkt.fun) — Telegram Mini App:
  Бот: @mrkt
  Mini App deep link: https://t.me/mrkt/app?startapp={slug_or_id}
  Веб:                https://mrkt.fun/gift/{slug}

GetGems (Portals):
  NFT:  https://getgems.io/nft/{address}
  Gift: https://getgems.io/gift/{slug}
"""

import re


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
    Строит deep link для MRKT Mini App.
    Формат: https://t.me/mrkt/app?startapp={startapp_param}

    MRKT принимает:
      - slug: 'eternal-rose-42' (если есть из API)
      - exact_slug: 'EternalRose-42' (CamelCase, как на Fragment)
      - gift_id: числовой ID (fallback)
    """
    # Приоритет: exact_slug из name+number > slug из API > raw ID
    if name and number:
        exact = f"{name.replace(' ', '')}-{number}"
        return f"https://t.me/mrkt/app?startapp={exact}"
    if slug:
        return f"https://t.me/mrkt/app?startapp={slug}"
    return f"https://t.me/mrkt/app?startapp={gift_id}"


def build_mrkt_web_link(slug: str = "", gift_id: str = "") -> str:
    """Веб-версия MRKT для браузера (без Mini App)."""
    param = slug if slug else gift_id
    return f"https://mrkt.fun/gift/{param}"


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
        # Основная Mini App ссылка
        tg_link = build_mrkt_gift_link(gift_id, slug, name, number)
        buttons.append({"text": "🟣 Открыть в MRKT", "url": tg_link})
        # Web fallback
        web_link = build_mrkt_web_link(slug, gift_id)
        buttons.append({"text": "🌐 Веб (mrkt.fun)", "url": web_link})

    elif market == "fragment":
        gift_link = build_fragment_gift_link(gift_id, slug, name, number)
        buttons.append({"text": "🔵 Открыть на Fragment", "url": gift_link})
        if name:
            col_link = build_fragment_collection_link(name)
            buttons.append({"text": "📋 Все лоты коллекции", "url": col_link})

    elif market in ("portals", "getgems"):
        link = build_getgems_gift_link(gift_id, slug)
        buttons.append({"text": "🟢 Открыть на GetGems", "url": link})

    return buttons
