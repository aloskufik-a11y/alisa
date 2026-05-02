"""
find_mrkt_links.py — Debug утилита для исследования API MRKT.

Запуск: python find_mrkt_links.py
Показывает реальный ответ API без авторизации (публичные данные).
"""
import asyncio
import sys
import os
import json

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main():
    import aiohttp

    print("=" * 60)
    print("MRKT API Debug Utility")
    print("=" * 60)

    # ── 1. Пробуем публичный эндпоинт без авторизации ─────────────────
    print("\n[1] Пробуем публичный запрос к MRKT API (без токена)...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://mrkt.fun",
        "Referer": "https://mrkt.fun/",
    }

    payload = {
        "backdropNames": [], "collectionNames": [], "count": 5,
        "cursor": "", "lowToHigh": True,
        "ordering": "price", "removeSelfSales": None,
        "modelNames": [], "symbolNames": [],
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "https://api.tgmrkt.io/api/v1/gifts/saling",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                print(f"  Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("items", data.get("data", []))
                    print(f"  Лотов в ответе: {len(items)}")
                    if items:
                        print("\n  Первый item (полная структура):")
                        print(json.dumps(items[0], ensure_ascii=False, indent=2))
                    print("\n  Корневые ключи ответа:", list(data.keys()))
                    cursor = data.get("cursor") or data.get("nextCursor")
                    print(f"  Cursor для пагинации: {cursor!r}")
                else:
                    text = await resp.text()
                    print(f"  Ответ: {text[:500]}")
        except Exception as e:
            print(f"  Ошибка: {e}")

    # ── 2. Fragment API публичный запрос ───────────────────────────────
    print("\n[2] Fragment API (публичный)...")

    frag_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://fragment.com/gifts",
        "Origin": "https://fragment.com",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "https://fragment.com/api",
                data="method=searchGifts&filter=on_sale&sort=listed&type=unique&limit=3&offset=0",
                headers=frag_headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                print(f"  Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    gifts = data.get("gifts", [])
                    print(f"  ok={data.get('ok')}, лотов={len(gifts)}, found={data.get('found')}")
                    if gifts:
                        print("\n  Первый gift (полная структура):")
                        print(json.dumps(gifts[0], ensure_ascii=False, indent=2))
                    print("\n  Корневые ключи ответа:", list(data.keys()))
                else:
                    text = await resp.text()
                    print(f"  Ответ ({resp.status}): {text[:300]}")
        except Exception as e:
            print(f"  Ошибка: {e}")

    # ── 3. Тест парсеров из logic.py ───────────────────────────────────
    print("\n[3] Тест парсеров...")
    from logic import parse_mrkt_json, parse_fragment_json, format_price

    # MRKT mock
    mrkt_mock = {
        "items": [
            {
                "id": "test123",
                "slug": "eternal-rose-42",
                "name": "Eternal Rose",
                "number": 42,
                "price": 5.5,
                "floor_price": 8.0,
                "rarity": "Rare",
                "image_url": "https://example.com/img.png",
            }
        ]
    }
    mrkt_items = parse_mrkt_json(mrkt_mock)
    print(f"  MRKT parser: {len(mrkt_items)} items")
    if mrkt_items:
        item = mrkt_items[0]
        print(f"    {item['name']} #{item['number']} — {format_price(item['price'], item['currency'])}")
        discount = round((item["floor_price"] - item["price"]) / item["floor_price"] * 100, 1)
        print(f"    Floor: {format_price(item['floor_price'], 'TON')} (скидка {discount}%)")

    # Fragment mock
    frag_mock = {
        "ok": True,
        "found": 1,
        "gifts": [
            {
                "id": "987654",
                "name": "Eternal Rose",
                "num": 42,
                "slug": "eternalrose-42",
                "price": 1500,
                "floor_price": 2200,
                "rarity": "Rare",
                "image_url": "",
            }
        ]
    }
    frag_items = parse_fragment_json(frag_mock)
    print(f"  Fragment parser: {len(frag_items)} items")
    if frag_items:
        item = frag_items[0]
        print(f"    {item['name']} #{item['number']} — {format_price(item['price'], item['currency'])}")
        print(f"    URL: {item['url']}")

    print("\n✅ Готово!")


if __name__ == "__main__":
    asyncio.run(main())
