"""
Юнит-тесты основной логики.

Все цены унифицированы в TON. Stars конвертируются в TON через rate_provider.

Запуск:
    python test_mrkt.py
"""
import sys
import traceback

PASS = 0
FAIL = 0


def test(name: str, condition: bool):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n[1] logic.py — вспомогательные функции")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import (
        _safe_float, _extract_price, format_price, _fragment_name_to_slug,
        re_has_number_suffix,
    )

    test("_safe_float(None)", _safe_float(None) == 0.0)
    test("_safe_float('5.5')", _safe_float("5.5") == 5.5)
    test("_safe_float({'amount': 1500})", _safe_float({"amount": 1500}) == 1500.0)
    test("_safe_float('bad')", _safe_float("bad") == 0.0)
    test("_safe_float(0)", _safe_float(0) == 0.0)

    test("_extract_price: первое ненулевое",
         _extract_price({"price": 0, "amount": 100}, "price", "amount") == 100)
    test("_extract_price: пропускает 0",
         _extract_price({"price": 0, "amount": 0, "value": 50}, "price", "amount", "value") == 50)
    test("_extract_price: всё 0 → None",
         _extract_price({"price": 0, "amount": 0}, "price", "amount") is None)

    # format_price (TON → '<n> 💎')
    test("format_price 5.5 TON", format_price(5.5) == "5.50 💎")
    test("format_price 10 (int) TON", format_price(10.0) == "10.00 💎")
    test("format_price 0.001 TON", "0.001" in format_price(0.001))
    test("format_price 0 → '0 💎'", format_price(0) == "0 💎")
    test("format_price None → '? 💎'", format_price(None) == "? 💎")

    test("_fragment_name_to_slug: Eternal Rose",
         _fragment_name_to_slug("Eternal Rose") == "eternalrose")
    test("_fragment_name_to_slug: B-Day Candle",
         _fragment_name_to_slug("B-Day Candle") == "bdaycandle")
    test("_fragment_name_to_slug: Plush Pepe",
         _fragment_name_to_slug("Plush Pepe") == "plushpepe")

    test("re_has_number_suffix('eternalrose-42')", re_has_number_suffix("eternalrose-42"))
    test("re_has_number_suffix('eternalrose')", not re_has_number_suffix("eternalrose"))
    test("re_has_number_suffix('rose-abc')", not re_has_number_suffix("rose-abc"))

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[2] logic.py — parse_mrkt_json")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import parse_mrkt_json

    mrkt_resp = {
        "items": [
            {
                "id": "abc123",
                "name": "Eternal Rose",
                "slug": "eternal-rose-42",
                "num": 42,
                "price": 5.5,
                "floorPrice": 8.0,
                "rarity": "rare",
                "imageUrl": "https://example.com/img.png",
            }
        ],
        "cursor": "next_page_xyz",
    }
    items = parse_mrkt_json(mrkt_resp)
    test("MRKT: 1 item парсится", len(items) == 1)

    it = items[0]
    test("MRKT: id", it["id"] == "abc123")
    test("MRKT: name", it["name"] == "Eternal Rose")
    test("MRKT: slug", it["slug"] == "eternal-rose-42")
    test("MRKT: number", it["number"] == "42")
    test("MRKT: price", it["price"] == 5.5)
    test("MRKT: floor_price", it["floor_price"] == 8.0)
    test("MRKT: currency=TON", it["currency"] == "TON")
    test("MRKT: rarity капитализирован", it["rarity"] == "Rare")
    test("MRKT: image_url", "img.png" in it["image_url"])

    # Граничные случаи
    test("MRKT: price=0 пропускается",
         len(parse_mrkt_json({"items": [{"id": "x", "name": "X", "price": 0}]})) == 0)
    test("MRKT: без id пропускается",
         len(parse_mrkt_json({"items": [{"name": "X", "price": 5}]})) == 0)
    test("MRKT: list input поддерживается",
         len(parse_mrkt_json([{"id": "y", "name": "Y", "price": 3}])) == 1)
    test("MRKT: 'data' ключ поддерживается",
         len(parse_mrkt_json({"data": [{"id": "z", "name": "Z", "price": 2}]})) == 1)

    cap = parse_mrkt_json({"items": [{"id": "r", "name": "X", "price": 1, "rarity": "EPIC"}]})
    test("MRKT: rarity capitalize", cap[0]["rarity"] == "Epic")

    dict_p = parse_mrkt_json({"items": [{"id": "dp", "name": "X", "price": {"amount": 7.5}}]})
    test("MRKT: price как dict", len(dict_p) == 1 and dict_p[0]["price"] == 7.5)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[3] logic.py — parse_fragment_json (Stars→TON)")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import parse_fragment_json

    # Используем фиксированный курс для воспроизводимости
    RATE = 0.004  # 1 Star = 0.004 TON (TON=$5, Star=$0.02)

    frag = {
        "ok": True,
        "found": 2,
        "gifts": [
            {
                "id": "12345",
                "name": "Eternal Rose",
                "num": 42,
                "slug": "eternalrose-42",
                "price": 1500,
                "floor_price": 2200,
                "rarity": "Rare",
                "image_url": "",
            },
            {
                "id": "99999",
                "name": "Plush Pepe",
                "num": 1821,
                "price": 800,
            }
        ]
    }

    items = parse_fragment_json(frag, stars_to_ton_rate=RATE)
    test("Fragment: 2 items парсятся", len(items) == 2)

    it = items[0]
    test("Fragment: id", it["id"] == "12345")
    test("Fragment: name", it["name"] == "Eternal Rose")
    test("Fragment: number", it["number"] == "42")
    test("Fragment: slug из API", it["slug"] == "eternalrose-42")
    test("Fragment: stars_price=1500 (оригинал)", it["stars_price"] == 1500)
    test("Fragment: price в TON (1500*0.004=6.0)", abs(it["price"] - 6.0) < 0.001)
    test("Fragment: floor_price в TON (2200*0.004=8.8)", abs(it["floor_price"] - 8.8) < 0.001)
    test("Fragment: floor_stars=2200 (оригинал)", it["floor_stars"] == 2200)
    test("Fragment: currency=TON (после конвертации)", it["currency"] == "TON")
    test("Fragment: url корректный", it["url"] == "https://fragment.com/gift/eternalrose-42")

    it2 = items[1]
    test("Fragment: slug строится без API slug", it2["slug"] == "plushpepe-1821")
    test("Fragment: url без API slug", it2["url"] == "https://fragment.com/gift/plushpepe-1821")

    # ok=false
    err = {"ok": False, "error": "blocked"}
    test("Fragment: ok=false → []", len(parse_fragment_json(err)) == 0)

    # price=0
    zero = {"ok": True, "gifts": [{"id": "z", "name": "X", "price": 0}]}
    test("Fragment: price=0 пропускается", len(parse_fragment_json(zero)) == 0)

    # price как dict (Stars)
    dict_p = {"ok": True, "gifts": [{"id": "dp", "name": "X", "num": 1, "price": {"amount": 1000}}]}
    parsed = parse_fragment_json(dict_p, stars_to_ton_rate=RATE)
    test("Fragment: price как dict (1000 Stars * 0.004 = 4 TON)",
         len(parsed) == 1 and abs(parsed[0]["price"] - 4.0) < 0.001)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[3b] logic.py — parse_fragment_html (HTML-скрейпинг)")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import parse_fragment_html

    sample_html = """
    <a href="/gift/eternalrose-42?sort=price_asc" class="tm-grid-item">
      <img src="https://nft.fragment.com/gift/eternalrose-42.jpg" />
      <span class="item-name">Eternal Rose</span>
      <span class="item-num">&nbsp;#42</span>
      <div class="tm-grid-item-value tm-value icon-before icon-ton">25</div>
      <div class="tm-grid-item-status tm-status-avail">For sale</div>
    </a>
    <a href="/gift/plushpepe-1?sort=price_asc" class="tm-grid-item">
      <span class="item-name">Plush Pepe</span>
      <span class="item-num">&nbsp;#1</span>
      <div class="tm-grid-item-value tm-value icon-before icon-star">5000</div>
      <div class="tm-grid-item-status tm-status-avail">For sale</div>
    </a>
    <a href="/gift/sold-3?sort=price_asc" class="tm-grid-item">
      <span class="item-name">Sold Out</span>
      <span class="item-num">&nbsp;#3</span>
      <div class="tm-grid-item-value tm-value icon-before icon-ton">100</div>
      <div class="tm-grid-item-status tm-status-sold">Sold</div>
    </a>
    """

    items = parse_fragment_html(sample_html, stars_to_ton_rate=0.004)
    test("Fragment HTML: 2 лота на продаже (Sold пропущен)", len(items) == 2)

    it = items[0]
    test("Fragment HTML: id=slug", it["id"] == "eternalrose-42")
    test("Fragment HTML: name", it["name"] == "Eternal Rose")
    test("Fragment HTML: number", it["number"] == "42")
    test("Fragment HTML: TON цена не конвертируется", it["price"] == 25.0)
    test("Fragment HTML: TON → stars_price=None", it["stars_price"] is None)
    test("Fragment HTML: url", it["url"] == "https://fragment.com/gift/eternalrose-42")
    test("Fragment HTML: image_url", "nft.fragment.com" in it["image_url"])

    it2 = items[1]
    test("Fragment HTML: Stars→TON (5000*0.004=20)",
         abs(it2["price"] - 20.0) < 0.001)
    test("Fragment HTML: stars_price оригинал", it2["stars_price"] == 5000.0)

    # Пустой/невалидный HTML
    test("Fragment HTML: пустая строка → []", len(parse_fragment_html("")) == 0)
    test("Fragment HTML: None → []", len(parse_fragment_html(None)) == 0)
    test("Fragment HTML: мусор → []", len(parse_fragment_html("<html><body>nope</body></html>")) == 0)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[4] logic.py — parse_portals_graphql")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import parse_portals_graphql

    portals = {
        "data": {
            "alphaSearch": {
                "items": [
                    {
                        "address": "EQAbcdef123",
                        "name": "Eternal Rose",
                        "sale": {"fullPrice": "5500000000"}  # 5.5 TON в нанотонах
                    }
                ]
            }
        }
    }
    items = parse_portals_graphql(portals)
    test("Portals: 1 item", len(items) == 1)
    it = items[0]
    test("Portals: id=address", it["id"] == "EQAbcdef123")
    test("Portals: price в TON", abs(it["price"] - 5.5) < 0.001)
    test("Portals: currency=TON", it["currency"] == "TON")

    # Нет адреса
    no_addr = {"data": {"alphaSearch": {"items": [{"name": "X", "sale": {"fullPrice": "1000000000"}}]}}}
    test("Portals: без address пропускается", len(parse_portals_graphql(no_addr)) == 0)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[5] logic.py — is_profitable (всё в TON)")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from logic import is_profitable

    # Лимит по умолчанию = 50 TON
    cheap = {"price": 5.0, "currency": "TON", "rarity": "Rare"}
    test("is_profitable: 5 TON (лимит 50)", is_profitable(cheap, "mrkt"))

    expensive = {"price": 999.0, "currency": "TON"}
    test("is_profitable: 999 TON → False", not is_profitable(expensive, "mrkt"))

    # 1500 Stars → 6 TON по курсу 0.004 — ниже лимита 50 TON
    fragment_cheap = {"price": 6.0, "stars_price": 1500, "currency": "TON"}
    test("is_profitable: 6 TON (Fragment converted)", is_profitable(fragment_cheap, "fragment"))

    # 9999 Stars → 39.996 TON — ниже лимита 50, но фильтр маркетов?
    fragment_high = {"price": 39.996, "stars_price": 9999, "currency": "TON"}
    test("is_profitable: 39.996 TON (Fragment) — ниже лимита",
         is_profitable(fragment_high, "fragment"))

    # Нулевые / некорректные цены
    test("is_profitable: price=None → False", not is_profitable({"price": None, "currency": "TON"}, "mrkt"))
    test("is_profitable: price=0 → False", not is_profitable({"price": 0, "currency": "TON"}, "mrkt"))
    test("is_profitable: price=-1 → False", not is_profitable({"price": -1, "currency": "TON"}, "mrkt"))

    # TG-маркет (с префиксом tg:) проходит фильтр маркетов
    tg_msg = {"price": 5.0, "currency": "TON"}
    test("is_profitable: tg:channel префикс пропускает фильтр",
         is_profitable(tg_msg, "tg:portals_market"))

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[6] url_builder.py")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from url_builder import (
        build_mrkt_gift_link, build_mrkt_web_link,
        build_fragment_gift_link, build_fragment_collection_link,
        build_getgems_gift_link, get_market_label,
    )

    url = build_mrkt_gift_link("123", "eternal-rose-42", "Eternal Rose", "42")
    test("MRKT link: /app?startapp=EternalRose-42",
         url == "https://t.me/mrkt/app?startapp=EternalRose-42")

    test("MRKT link: id fallback",
         build_mrkt_gift_link("abc") == "https://t.me/mrkt/app?startapp=abc")
    test("MRKT link: slug fallback",
         build_mrkt_gift_link("x", "eternal-rose-42") == "https://t.me/mrkt/app?startapp=eternal-rose-42")

    test("MRKT web link",
         build_mrkt_web_link("eternal-rose-42") == "https://mrkt.fun/gift/eternal-rose-42")

    test("Fragment link: name+number",
         build_fragment_gift_link("0", "", "Eternal Rose", "42") == "https://fragment.com/gift/eternalrose-42")
    test("Fragment link: ready slug",
         build_fragment_gift_link("0", "plushpepe-1821") == "https://fragment.com/gift/plushpepe-1821")
    test("Fragment link: fallback",
         build_fragment_gift_link("0") == "https://fragment.com/gifts")

    coll = build_fragment_collection_link("Eternal Rose")
    test("Fragment collection link", "eternalrose" in coll and "filter=sale" in coll)

    test("GetGems link: address",
         build_getgems_gift_link("EQAbcdef") == "https://getgems.io/nft/EQAbcdef")
    test("GetGems link: slug",
         build_getgems_gift_link("EQAbcdef", slug="some-gift") == "https://getgems.io/gift/some-gift")

    test("Label mrkt", "MRKT" in get_market_label("mrkt"))
    test("Label fragment", "Fragment" in get_market_label("fragment"))
    test("Label portals", "GetGems" in get_market_label("portals"))

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[7] tg_message_parser.py (все цены → TON)")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from tg_message_parser import parse_telegram_message

    # Stars → TON автоматически
    r = parse_telegram_message("🎁 Eternal Rose — 1500 Stars")
    test("Parser: Stars '—' format → TON",
         r and r["currency"] == "TON" and r["stars_price"] == 1500)

    r = parse_telegram_message("Crystal Dragon — 800⭐")
    test("Parser: Stars emoji format → TON",
         r and r["currency"] == "TON" and r["stars_price"] == 800)

    r = parse_telegram_message("Plush Pepe – 800⭐")  # en-dash
    test("Parser: en-dash тоже работает",
         r and r["currency"] == "TON" and r["stars_price"] == 800)

    r = parse_telegram_message("Iron Crown -- 600 Stars")  # двойной hyphen
    test("Parser: double-hyphen работает",
         r and r["currency"] == "TON" and r["stars_price"] == 600)

    r = parse_telegram_message("Name: Iron Crown | Price: 2500 Stars")
    test("Parser: Name: ... | Price: ... format",
         r and r["currency"] == "TON" and r["stars_price"] == 2500)

    r = parse_telegram_message("Price: 5.5 TON | Eternal Rose")
    test("Parser: Price TON format",
         r and r["price"] == 5.5 and r["currency"] == "TON")

    r = parse_telegram_message("Eternal Rose for 3.2 TON")
    test("Parser: 'for X TON' format",
         r and r["price"] == 3.2 and r["currency"] == "TON")

    r = parse_telegram_message("1200⭐ Eternal Rose #42")
    test("Parser: leading Stars price → TON",
         r and r["currency"] == "TON" and r["stars_price"] == 1200)

    test("Parser: нет цены → None", parse_telegram_message("hello world nothing here") is None)
    test("Parser: пустая строка → None", parse_telegram_message("") is None)
    test("Parser: None → None", parse_telegram_message(None) is None)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[8] database.py")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from database import init_db, is_gift_seen, add_gift, get_stats

    init_db()

    import time
    uid = f"test_unit_{int(time.time() * 1000)}"
    test("DB: add_gift новый → True", add_gift(uid, "Test Gift", 5.0, "test") is True)
    test("DB: add_gift дубликат → False", add_gift(uid, "Test Gift", 5.0, "test") is False)
    test("DB: is_gift_seen после add", is_gift_seen(uid))
    test("DB: is_gift_seen несуществующего", not is_gift_seen("not_exists_xyz"))

    stats = get_stats()
    test("DB: get_stats().total >= 1", stats["total"] >= 1)
    test("DB: get_stats().by_source dict", isinstance(stats["by_source"], dict))

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[9] settings_store.py (TON-based, без max_price_stars)")
# ══════════════════════════════════════════════════════════════════════════════
try:
    from settings_store import load_settings, save_settings, DEFAULT_SETTINGS

    s = load_settings()
    test("Settings: все ключи присутствуют", all(k in s for k in DEFAULT_SETTINGS))
    test("Settings: max_price_ton > 0", s["max_price_ton"] > 0)
    test("Settings: notifications_on bool", isinstance(s["notifications_on"], bool))
    test("Settings: filter_markets list", isinstance(s["filter_markets"], list))
    test("Settings: filter_rarity list", isinstance(s["filter_rarity"], list))
    test("Settings: max_price_stars удалён (deprecated)",
         "max_price_stars" not in s)

    # Сохранение и чтение
    original = s["max_price_ton"]
    s["max_price_ton"] = 123.45
    save_settings(s)
    s2 = load_settings()
    test("Settings: сохраняется и читается", s2["max_price_ton"] == 123.45)
    s2["max_price_ton"] = original
    save_settings(s2)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n[10] config.py — _env_int безопасность")
# ══════════════════════════════════════════════════════════════════════════════
try:
    import os
    from config import _env_int

    # Сохраняем оригинал
    original_api = os.environ.get("API_ID", "")

    os.environ["TEST_INT_VAR"] = ""
    test("config: пустая строка → default", _env_int("TEST_INT_VAR", 42) == 42)

    os.environ["TEST_INT_VAR"] = "  "
    test("config: пробелы → default", _env_int("TEST_INT_VAR", 42) == 42)

    os.environ["TEST_INT_VAR"] = "123"
    test("config: '123' → 123", _env_int("TEST_INT_VAR", 0) == 123)

    os.environ["TEST_INT_VAR"] = "  456  "
    test("config: '  456  ' → 456 (trim)", _env_int("TEST_INT_VAR", 0) == 456)

    os.environ["TEST_INT_VAR"] = "abc"
    test("config: 'abc' → default (с warning)", _env_int("TEST_INT_VAR", 7) == 7)

    del os.environ["TEST_INT_VAR"]
    test("config: missing var → default", _env_int("MISSING_VAR_XYZ", 99) == 99)

except Exception as e:
    print(f"  💥 Ошибка: {e}")
    traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"Результат: ✅ {PASS} прошло  ❌ {FAIL} провалилось")
print("=" * 60)

if FAIL > 0:
    sys.exit(1)
