"""
Публичный FastAPI-бэкенд для Mini App.

Самодостаточный — деплоится на Fly.io. Источники данных:
  • Portals — публичный API `portal-market.com/api/nfts/search`
  • Fragment — HTML `fragment.com/gifts?sort=price_asc`
  • MRKT — пушит сам бот через `POST /api/push` (X-API-Key)

Эндпоинты:
  GET  /                     — SPA (index.html)
  GET  /static/{path}        — статика
  GET  /api/feed             — aggregate feed
  GET  /api/health           — статус
  GET  /api/settings         — read-only snapshot of bot settings
                               (если бот не пушил, отдаёт дефолты)
  POST /api/push             — приём событий от бота (требует X-API-Key)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backend")

API_KEY = os.getenv("PUSH_API_KEY", "").strip()
PORTALS_TTL = 30
FRAGMENT_TTL = 60
MAX_FEED = 1000

app = FastAPI(title="Gift Monitor Web App")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ─── В памяти ──────────────────────────────────────────────────────────────
_pushed_feed: Deque[dict] = deque(maxlen=MAX_FEED)
_pushed_settings: dict = {}

_portals_cache: dict = {"ts": 0, "items": []}
_fragment_cache: dict = {"ts": 0, "items": []}


# ─── Periodic scrapers (Portals + Fragment, no auth) ──────────────────────
async def fetch_portals() -> list[dict]:
    if time.time() - _portals_cache["ts"] < PORTALS_TTL:
        return _portals_cache["items"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://portal-market.com/api/nfts/search",
                params={"limit": 200, "offset": 0, "status": "listed"},
                headers={"User-Agent": "Mozilla/5.0 (gift-monitor backend)"},
            )
        r.raise_for_status()
        data = r.json()
        items = []
        for p in data.get("results", []) or []:
            attrs = p.get("attributes") or []
            def find(t):
                for a in attrs:
                    if a.get("type") == t:
                        return a
                return {}
            m, b, sym = find("model"), find("backdrop"), find("symbol")
            try:
                price = float(p.get("price"))
            except (TypeError, ValueError):
                price = None
            try:
                floor = float(p.get("floor_price"))
            except (TypeError, ValueError):
                floor = None
            items.append({
                "ts": int(time.time()),
                "market": "portals",
                "id": p.get("id"),
                "name": p.get("name"),
                "number": p.get("external_collection_number"),
                "slug": p.get("tg_id"),
                "price": price,
                "floor_price": floor,
                "rarity": None,
                "model_name": m.get("value"),
                "backdrop_name": b.get("value"),
                "symbol_name": sym.get("value"),
                "rarities_pm": {
                    "model": m.get("rarity_per_mille"),
                    "backdrop": b.get("rarity_per_mille"),
                    "symbol": sym.get("rarity_per_mille"),
                },
                "colors": [],
                "image_url": p.get("photo_url"),
                "url": f"https://t.me/portals/market?startapp=gift_{p.get('id')}",
                "listed_at": p.get("listed_at"),
            })
        _portals_cache["ts"] = time.time()
        _portals_cache["items"] = items
        logger.info(f"Portals: cached {len(items)} items")
        return items
    except Exception as e:
        logger.exception(f"Portals fetch failed: {e}")
        return _portals_cache["items"]


async def fetch_fragment(stars_to_ton: float = 0.004) -> list[dict]:
    """Парсит fragment.com/gifts?filter=sale&sort=price_asc.

    Stars-цены конвертируются в TON через `stars_to_ton`.
    Floor определяется как min(price) для одинаковых name в выдаче.
    """
    if time.time() - _fragment_cache["ts"] < FRAGMENT_TTL:
        return _fragment_cache["items"]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(
                "https://fragment.com/gifts?filter=sale&sort=price_asc",
                headers={
                    "User-Agent":
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0 Safari/537.36",
                    "Accept": "text/html",
                },
            )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        for card in soup.select("a.tm-grid-item"):
            status = card.select_one(".tm-grid-item-status")
            if not status or "tm-status-avail" not in (status.get("class") or []):
                continue
            href = card.get("href", "")
            slug = href.split("/gift/", 1)[-1].split("?", 1)[0]
            name_el = card.select_one(".item-name")
            num_el = card.select_one(".item-num")
            name = name_el.get_text(strip=True) if name_el else ""
            num_txt = (num_el.get_text(strip=True) if num_el else "").lstrip("#").strip()
            try:
                number = int(re.sub(r"\D", "", num_txt)) if num_txt else None
            except ValueError:
                number = None

            price = None
            for val in card.select(".tm-grid-item-value"):
                cls = " ".join(val.get("class") or [])
                txt = val.get_text(" ", strip=True).replace("\xa0", "").replace(" ", "")
                m = re.search(r"[\d.,]+", txt)
                if not m:
                    continue
                try:
                    p = float(m.group(0).replace(",", "."))
                except ValueError:
                    continue
                if "icon-ton" in cls:
                    price = p
                    break
                elif "icon-star" in cls:
                    price = p * stars_to_ton
                    break

            photo = card.select_one("img.tm-grid-thumb")
            image_url = (photo.get("src") if photo else None) or None
            if not href.startswith("http"):
                href = "https://fragment.com" + href

            items.append({
                "ts": int(time.time()),
                "market": "fragment",
                "id": slug or (name + str(number or "")),
                "name": name,
                "number": number,
                "slug": slug,
                "price": price,
                "floor_price": None,
                "rarity": None,
                "model_name": None,
                "backdrop_name": None,
                "symbol_name": None,
                "rarities_pm": {},
                "colors": [],
                "image_url": image_url,
                "url": href,
            })

        from collections import defaultdict
        by_name: dict[str, list[float]] = defaultdict(list)
        for it in items:
            if it["price"]:
                by_name[it["name"]].append(it["price"])
        for it in items:
            if it["price"] and by_name.get(it["name"]):
                it["floor_price"] = min(by_name[it["name"]])

        _fragment_cache["ts"] = time.time()
        _fragment_cache["items"] = items
        logger.info(f"Fragment: cached {len(items)} items")
        return items
    except Exception as e:
        logger.exception(f"Fragment fetch failed: {e}")
        return _fragment_cache["items"]


# ─── Routes ───────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse({"ok": True, "msg": "SPA missing"}, 500)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "pushed": len(_pushed_feed),
        "portals_cached": len(_portals_cache["items"]),
        "fragment_cached": len(_fragment_cache["items"]),
    }


@app.get("/api/settings")
async def settings():
    """Возвращает последний snapshot настроек, переданный ботом, или дефолт."""
    return {"ok": True, "settings": _pushed_settings or _DEFAULT_SETTINGS}


_DEFAULT_SETTINGS = {
    "max_price_ton": 50.0,
    "min_price_ton": 0.0,
    "floor_tolerance_pct": 0.0,
    "min_discount_pct": 0,
    "require_floor": True,
    "filter_rarity": [],
    "filter_markets": ["mrkt", "fragment", "portals"],
    "filter_collections": [],
    "monochrome_only": False,
    "number_filters": [],
    "max_rarity_pm": 0,
    "notifications_on": True,
    "mrkt_alerts_on": True,
    "fragment_alerts_on": True,
    "portals_alerts_on": True,
    "quiet_hours_start": 0,
    "quiet_hours_end": 0,
    "max_alerts_per_cycle": 0,
    "recent_rare_mode": False,
    "recent_rare_pm": 5.0,
}


@app.get("/api/feed")
async def feed(market: str | None = None,
               source: str = "all",
               limit: int = 200):
    """source = pushed | live | all"""
    items: list[dict] = []
    if source in ("live", "all"):
        items.extend(await fetch_portals())
        items.extend(await fetch_fragment())
    if source in ("pushed", "all"):
        items.extend(list(_pushed_feed))

    if market and market != "all":
        items = [i for i in items if i.get("market") == market]

    # dedup by (market,id)
    seen = set()
    out = []
    for it in items:
        key = (it.get("market"), it.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)

    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return {"ok": True, "count": len(out[:limit]), "items": out[:limit]}


@app.post("/api/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="dict required")
    items = body.get("items") or []
    settings_payload = body.get("settings")
    if isinstance(settings_payload, dict):
        _pushed_settings.clear()
        _pushed_settings.update(settings_payload)
    cnt = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if "ts" not in it:
            it["ts"] = int(time.time())
        _pushed_feed.appendleft(it)
        cnt += 1
    return {"ok": True, "pushed": cnt, "settings_updated": bool(settings_payload)}


# Background warm-up
@app.on_event("startup")
async def warm_up():
    asyncio.create_task(_periodic())


async def _periodic():
    while True:
        try:
            await fetch_portals()
            await fetch_fragment()
        except Exception:
            logger.exception("periodic")
        await asyncio.sleep(45)
