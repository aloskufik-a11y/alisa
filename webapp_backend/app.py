"""
Публичный FastAPI-бэкенд для Mini App.

Самодостаточный — деплоится на Fly.io. Источники данных:
  • Portals — публичный API `portal-market.com/api/nfts/search`
  • Fragment — HTML `fragment.com/gifts?sort=price_asc`
  • MRKT — пушит сам бот через `POST /api/push` (X-API-Key)

Эндпоинты:
  GET  /                       — SPA (index.html)
  GET  /static/{path}          — статика
  GET  /api/feed               — aggregate feed (TON-цены)
  GET  /api/health             — статус
  GET  /api/markets            — статус каждого маркета (count, age_sec)
  GET  /api/floor-history      — история floor по имени коллекции
  GET  /api/settings           — read-only snapshot of bot settings
                                 (если бот не пушил, отдаёт дефолты)
  POST /api/settings           — Mini App шлёт diff настроек (бот поллит)
  POST /api/push               — приём событий от бота (требует X-API-Key)
  POST /api/test-alert         — Mini App просит бота прислать тестовое уведомление
  GET  /api/pending_settings   — бот поллит pending diff (требует X-API-Key)
  GET  /api/pending_test_alert — бот поллит запрос на тестовый алерт
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backend")

API_KEY = os.getenv("PUSH_API_KEY", "").strip()
PORTALS_TTL = 30
FRAGMENT_TTL = 60
MAX_FEED = 1000
FLOOR_HISTORY_MAX = 240          # ~4 часа при шаге в 60 сек
FLOOR_HISTORY_INTERVAL = 60      # сек между точками

app = FastAPI(title="Gift Monitor Web App")

# CORS — Mini App открыт из tg origin'а; внешние страницы (например Telegram
# Web K/A) тоже должны спокойно дёргать /api/feed. Никаких креденшалов в
# запросах нет, поэтому wildcard безопасен.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Persistent state directory: /data на Fly.io если есть mount, иначе рядом.
DATA_DIR = Path(os.getenv("BACKEND_DATA_DIR", "") or "/data")
if not DATA_DIR.exists() or not os.access(DATA_DIR, os.W_OK):
    DATA_DIR = Path(__file__).parent / ".state"
try:
    DATA_DIR.mkdir(exist_ok=True, parents=True)
except Exception:
    DATA_DIR = Path(__file__).parent
PENDING_FILE = DATA_DIR / "pending_settings.json"
PUSHED_SETTINGS_FILE = DATA_DIR / "pushed_settings.json"
FLOOR_HISTORY_FILE = DATA_DIR / "floor_history.json"

# ─── В памяти ──────────────────────────────────────────────────────────────
_pushed_feed: Deque[dict] = deque(maxlen=MAX_FEED)
_pushed_settings: dict = {}

# Кэши маркетов: backend сам скрейпит публичные источники, но если бот
# присылает batch — заменяем кэш.
_portals_cache: dict = {"ts": 0, "items": [], "from_bot": False}
_fragment_cache: dict = {"ts": 0, "items": [], "from_bot": False}
_mrkt_cache: dict = {"ts": 0, "items": [], "from_bot": True}  # MRKT — только бот
# Getgems — данные приходят из бота через push/push_batch. Backend не делает
# собственный скрейп Getgems, чтобы не делить лимит 400 req / 5 min на IP.
_getgems_cache: dict = {"ts": 0, "items": [], "from_bot": True}

# Изменения настроек, ожидающие применения ботом (поллит /api/pending_settings)
_pending_settings: dict = {"ts": 0, "settings": {}}
_last_applied_ts: int = 0

# История floor по имени коллекции: name -> deque[(ts, floor_ton)]
_floor_history: dict[str, Deque[tuple[int, float]]] = defaultdict(
    lambda: deque(maxlen=FLOOR_HISTORY_MAX)
)
_last_floor_record_ts: float = 0.0


def _safe_load_json(path: Path) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"failed to load {path}")
    return None


def _safe_save_json(path: Path, data: Any) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(path)
    except Exception:
        logger.exception(f"failed to save {path}")


def _load_persisted_state() -> None:
    global _last_applied_ts
    p = _safe_load_json(PENDING_FILE)
    if isinstance(p, dict):
        _pending_settings["ts"] = int(p.get("ts", 0) or 0)
        s = p.get("settings")
        _pending_settings["settings"] = s if isinstance(s, dict) else {}
        _last_applied_ts = int(p.get("last_applied_ts", 0) or 0)
        if _pending_settings["settings"]:
            logger.info(
                "Loaded %s pending settings keys from disk",
                len(_pending_settings["settings"]),
            )
    s = _safe_load_json(PUSHED_SETTINGS_FILE)
    if isinstance(s, dict):
        _pushed_settings.update(s)
    h = _safe_load_json(FLOOR_HISTORY_FILE)
    if isinstance(h, dict):
        for name, points in h.items():
            if isinstance(points, list):
                dq = _floor_history[name]
                for p in points[-FLOOR_HISTORY_MAX:]:
                    if isinstance(p, list) and len(p) == 2:
                        try:
                            dq.append((int(p[0]), float(p[1])))
                        except (TypeError, ValueError):
                            continue


def _persist_pending() -> None:
    _safe_save_json(
        PENDING_FILE,
        {
            "ts": _pending_settings.get("ts", 0),
            "settings": _pending_settings.get("settings", {}),
            "last_applied_ts": _last_applied_ts,
        },
    )


def _persist_pushed_settings() -> None:
    _safe_save_json(PUSHED_SETTINGS_FILE, _pushed_settings)


def _persist_floor_history() -> None:
    snap = {
        name: [list(p) for p in points]
        for name, points in _floor_history.items()
        if points
    }
    _safe_save_json(FLOOR_HISTORY_FILE, snap)


_load_persisted_state()


# ─── Periodic scrapers (Portals + Fragment, no auth) ──────────────────────
async def fetch_portals() -> list[dict]:
    # Если бот присылает свежие batch’и — не лезем на portal-market.com.
    if _portals_cache.get("from_bot") and time.time() - _portals_cache["ts"] < 300:
        return _portals_cache["items"]
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
            # Реальное время листинга — для корректной работы фильтра
            # «✨ Новые 5 мин» в Mini App. Раньше клали time.time() (момент
            # cкрейпа) → весь батч казался «свежим» сразу после fetch'а
            # и уходил из «новых» через 5 мин одновременно.
            listed_at = p.get("listed_at")
            ts = int(time.time())
            if isinstance(listed_at, str) and listed_at:
                try:
                    from datetime import datetime
                    iso = listed_at.replace("Z", "+00:00")
                    ts = int(datetime.fromisoformat(iso).timestamp())
                except (ValueError, TypeError):
                    pass
            items.append({
                "ts": ts,
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
    if _fragment_cache.get("from_bot") and time.time() - _fragment_cache["ts"] < 300:
        return _fragment_cache["items"]
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
        "portals_from_bot": bool(_portals_cache.get("from_bot")),
        "fragment_cached": len(_fragment_cache["items"]),
        "fragment_from_bot": bool(_fragment_cache.get("from_bot")),
        "mrkt_cached": len(_mrkt_cache["items"]),
        "getgems_cached": len(_getgems_cache["items"]),
        "getgems_from_bot": bool(_getgems_cache.get("from_bot")),
        "pending_settings_ts": _pending_settings.get("ts", 0),
        "last_applied_ts": _last_applied_ts,
        "settings_known": bool(_pushed_settings),
        "data_dir": str(DATA_DIR),
    }


@app.get("/api/markets")
async def markets_status():
    """Per-market status для live-индикаторов в UI."""
    now = int(time.time())
    out = {}
    for name, cache in (
        ("mrkt", _mrkt_cache),
        ("portals", _portals_cache),
        ("fragment", _fragment_cache),
        ("getgems", _getgems_cache),
    ):
        ts = int(cache.get("ts", 0) or 0)
        out[name] = {
            "count": len(cache.get("items", [])),
            "ts": ts,
            "age_sec": (now - ts) if ts else None,
            "from_bot": bool(cache.get("from_bot")),
        }
    return {"ok": True, "now": now, "markets": out}


@app.get("/api/floor-history")
async def floor_history(name: str = "", limit: int = 120):
    """
    История floor по имени коллекции.
    Без параметра `name` — список имён, по которым есть история.
    limit ограничен сверху FLOOR_HISTORY_MAX.
    """
    limit = max(1, min(int(limit or 120), FLOOR_HISTORY_MAX))
    key = (name or "").strip().lower()
    if not key:
        return {
            "ok": True,
            "names": sorted(_floor_history.keys()),
            "interval": FLOOR_HISTORY_INTERVAL,
        }
    points = list(_floor_history.get(key) or ())[-limit:]
    return {
        "ok": True,
        "name": key,
        "interval": FLOOR_HISTORY_INTERVAL,
        "points": [{"ts": ts, "floor": fv} for ts, fv in points],
    }


@app.get("/api/settings")
async def settings():
    """Возвращает последний snapshot настроек, переданный ботом, или дефолт."""
    return {"ok": True, "settings": _pushed_settings or _DEFAULT_SETTINGS}


_DEFAULT_SETTINGS = {
    "max_price_ton": 50.0,
    "min_price_ton": 0.0,
    "floor_tolerance_pct": 0.0,
    "strict_below_floor": True,
    "min_savings_ton": 0.0,
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
    "watchlist_names": [],
    "watchlist_models": [],
    "watchlist_backdrops": [],
    "floor_drop_alert": False,
    "floor_drop_pct": 5.0,
    "mini_app_url": "",
    "daily_digest_enabled": True,
    "daily_digest_hour_utc": 6,
    "daily_digest_window_hours": 24,
    "last_digest_date": "",
    "rare_priority_enabled": True,
    "rare_priority_pm": 5.0,
    "ai_provider": "off",
    "groq_api_key": "",
    "groq_model": "llama-3.3-70b-versatile",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.0-flash",
    "ai_for_alerts": False,
    "ai_for_digest": True,
    "ai_alerts_min_discount_pct": 10.0,
    "ai_persona": "balanced",
    "ai_custom_prompt": "",
}


@app.get("/api/feed")
async def feed(market: str | None = None,
               source: str = "all",
               limit: int = 500):
    """source = pushed | live | all"""
    items: list[dict] = []
    if source in ("live", "all"):
        items.extend(await fetch_portals())
        items.extend(await fetch_fragment())
        items.extend(list(_mrkt_cache["items"]))
        items.extend(list(_getgems_cache["items"]))
    if source in ("pushed", "all"):
        items.extend(list(_pushed_feed))

    if market and market != "all":
        items = [i for i in items if i.get("market") == market]

    # dedup by (market,id) — оставляем более свежий
    seen: dict = {}
    for it in items:
        key = (it.get("market"), it.get("id"))
        if key not in seen or (it.get("ts", 0) > seen[key].get("ts", 0)):
            seen[key] = it
    out = list(seen.values())

    # Cross-market floor: берём min из всех лотов по имени коллекции.
    # Но если по имени всего один лот — floor не равен его же цене (иначе
    # в UI покажется 0% скидки для любого экземпляра).
    items_by_name: dict[str, list[dict]] = defaultdict(list)
    for it in out:
        nm = (it.get("name") or "").strip().lower()
        if nm:
            items_by_name[nm].append(it)

    floors_by_name: dict[str, float] = {}
    for nm, group in items_by_name.items():
        prices: list[float] = []
        for it in group:
            for v in (it.get("floor_price"), it.get("price")):
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    prices.append(fv)
        if prices:
            floors_by_name[nm] = min(prices)

    # Проставляем xfloor всем.
    for it in out:
        nm = (it.get("name") or "").strip().lower()
        xf = floors_by_name.get(nm)
        if xf is not None:
            it["xfloor"] = round(xf, 4)
            # Если оригинальный floor пуст и по этому имени есть хотя бы 2 лота
            # — подставим xfloor. Если лот единственный — оставляем пустым (чтобы
            # не показывать фиктивную скидку 0% / "floor = price").
            f = it.get("floor_price")
            single = len(items_by_name[nm]) <= 1
            if not f and not single:
                it["floor_price"] = it["xfloor"]
            elif (
                f
                and it.get("price")
                and abs(f - it["price"]) < 0.001
                and not single
            ):
                it["floor_price"] = it["xfloor"]

    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return {"ok": True, "count": len(out[:limit]), "items": out[:limit]}


async def _broadcast_to_peers(body: dict, x_api_key: str, endpoint: str = "/api/push") -> int:
    """
    Распространяет batch/settings на все Fly.io machines в кластере.
    Backend uvicorn слушает только IPv4 (0.0.0.0), поэтому 6PN IPv6 connect
    не работает. Используем PUBLIC URL c заголовком fly-force-instance-id для
    точечной маршрутизации на каждую машину.
    """
    sent = 0
    app_name = os.getenv("FLY_APP_NAME", "")
    if not app_name:
        return 0
    host = f"{app_name}.internal"
    try:
        # 6PN адреса нужны только чтобы получить fly-machine-id'ы.
        # Сами машины слушают на v4, поэтому идём через публичный proxy.
        infos = await asyncio.get_event_loop().getaddrinfo(host, 80)
    except Exception:
        return 0
    seen_ips = sorted({sa[0] for *_, sa in infos})
    if len(seen_ips) <= 1:
        return 0
    # public URL fall-back: шлём в LB много раз, чтобы Fly раскидала по машинам.
    # Помечаем как broadcast, чтобы получатель не делал бесконечный re-broadcast.
    public_url = f"https://{app_name}.fly.dev{endpoint}"
    headers = {"X-Internal-Broadcast": "1"}
    if x_api_key:
        headers["X-API-Key"] = x_api_key
    async with httpx.AsyncClient(timeout=6, http2=False) as client:
        # Шлём 4 × (peers-1), чтобы покрыть всех с запасом.
        for _ in range(max(8, len(seen_ips) * 4)):
            try:
                await client.post(public_url, json=body, headers=headers)
                sent += 1
            except Exception:
                pass
    return sent


@app.post("/api/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="dict required")

    # Если это межсерверный broadcast, не повторяем broadcast.
    is_broadcast = bool(request.headers.get("X-Internal-Broadcast"))

    items = body.get("items") or []
    settings_payload = body.get("settings")
    batch = body.get("batch")
    market_for_batch = (body.get("market") or "").lower()
    mode = body.get("mode") or "replace"

    if isinstance(settings_payload, dict):
        _pushed_settings.clear()
        _pushed_settings.update(settings_payload)
        _persist_pushed_settings()
        logger.info(
            "settings snapshot from bot (%s keys)",
            len(settings_payload),
        )
        # Отметим, что бот применил настройки — подтверждаем, что pending было подхвачено.
        pending_ts = body.get("applied_ts") or _pending_settings.get("ts", 0)
        if pending_ts and pending_ts <= _pending_settings.get("ts", 0):
            global _last_applied_ts
            _last_applied_ts = int(pending_ts)
            _persist_pending()

    pushed_cnt = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if "ts" not in it:
            it["ts"] = int(time.time())
        _pushed_feed.appendleft(it)
        pushed_cnt += 1

    batch_cnt = 0
    if isinstance(batch, list) and market_for_batch:
        cache = {
            "mrkt": _mrkt_cache,
            "portals": _portals_cache,
            "fragment": _fragment_cache,
            "getgems": _getgems_cache,
        }.get(market_for_batch)
        if cache is not None:
            now = int(time.time())
            cleaned = []
            for it in batch:
                if not isinstance(it, dict):
                    continue
                if "ts" not in it:
                    it["ts"] = now
                cleaned.append(it)
            if mode == "append" and cache.get("items"):
                seen = {(c.get("id")): c for c in cache["items"]}
                for it in cleaned:
                    seen[it.get("id")] = it
                cache["items"] = list(seen.values())
            else:
                cache["items"] = cleaned
            cache["ts"] = now
            cache["from_bot"] = True
            batch_cnt = len(cleaned)
            logger.info(f"{market_for_batch}: replaced cache from bot ({batch_cnt} items)")

    # Broadcast на остальные машины кластера, чтобы синхронизировать кэш.
    broadcast_cnt = 0
    if not is_broadcast and (batch or items or settings_payload):
        broadcast_cnt = await _broadcast_to_peers(body, x_api_key)

    return {
        "ok": True,
        "pushed": pushed_cnt,
        "batch": batch_cnt,
        "market": market_for_batch,
        "settings_updated": bool(settings_payload),
        "broadcast": broadcast_cnt,
    }


# ─── Editable settings (Mini App → backend → бот) ────────────────────
ALLOWED_KEYS = set(_DEFAULT_SETTINGS.keys())


@app.post("/api/settings")
async def update_settings(request: Request):
    """
    Принимает дифф настроек от Mini App. Сохраняет в _pending_settings,
    бот подхватит при следующем полле (/api/pending_settings).

    Никакой auth: WebApp доверяем по факту открытия в Telegram — это
    приватный URL, и X-API-Key был бы виден в JS всё равно.
    На стороне бота применяем только выбранный белый список ключей.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="dict required")
    diff = body.get("settings") if isinstance(body.get("settings"), dict) else body
    cleaned = {k: v for k, v in diff.items() if k in ALLOWED_KEYS}
    if not cleaned:
        raise HTTPException(status_code=400, detail="no allowed keys")

    is_broadcast = bool(request.headers.get("X-Internal-Broadcast"))

    # Сливаем с предыдущим pending (если бот ещё не применил)
    cur = dict(_pending_settings.get("settings") or {})
    cur.update(cleaned)
    _pending_settings["settings"] = cur
    _pending_settings["ts"] = int(time.time())
    _persist_pending()

    # Оптимистично сразу обновляем показываемый snapshot, чтобы UI отображал новые значения.
    if _pushed_settings:
        _pushed_settings.update(cleaned)
        _persist_pushed_settings()

    # Расходимся на peer machines, если это первичный POST.
    broadcast_cnt = 0
    if not is_broadcast:
        broadcast_cnt = await _broadcast_to_peers(body, "", endpoint="/api/settings")

    return {
        "ok": True,
        "applied_keys": list(cleaned.keys()),
        "pending_ts": _pending_settings["ts"],
        "broadcast": broadcast_cnt,
    }


@app.get("/api/pending_settings")
async def pending_settings(since: int = 0, x_api_key: str = Header(default="")):
    """Бот поллит этот endpoint каждые N секунд."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    ts = _pending_settings.get("ts", 0)
    if ts and ts > since:
        return {
            "ok": True,
            "changed": True,
            "ts": ts,
            "settings": _pending_settings.get("settings", {}),
        }
    return {"ok": True, "changed": False, "ts": ts}


# ─── Тестовый алерт (Mini App → backend → бот) ───────────────────────
_test_alert: dict = {"ts": 0}


@app.post("/api/test-alert")
async def post_test_alert(request: Request):
    """Mini App просит бота прислать тестовое уведомление."""
    is_broadcast = bool(request.headers.get("X-Internal-Broadcast"))
    _test_alert["ts"] = int(time.time())
    if not is_broadcast:
        try:
            body = await request.json()
        except Exception:
            body = {}
        await _broadcast_to_peers(body or {}, "", endpoint="/api/test-alert")
    return {"ok": True, "ts": _test_alert["ts"]}


@app.get("/api/pending_test_alert")
async def get_pending_test_alert(since: int = 0, x_api_key: str = Header(default="")):
    """Бот поллит этот endpoint и шлёт тестовое уведомление пользователю."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    ts = _test_alert.get("ts", 0)
    return {"ok": True, "changed": bool(ts and ts > since), "ts": ts}


# Background warm-up
@app.on_event("startup")
async def warm_up():
    asyncio.create_task(_periodic())


async def _periodic():
    last_floor_persist = 0.0
    while True:
        try:
            await fetch_portals()
            await fetch_fragment()
            _record_floor_snapshot()
            now = time.time()
            if now - last_floor_persist > 300:
                _persist_floor_history()
                last_floor_persist = now
        except Exception:
            logger.exception("periodic")
        await asyncio.sleep(45)


def _record_floor_snapshot() -> None:
    """Раз в FLOOR_HISTORY_INTERVAL сек записываем floor по каждой коллекции."""
    global _last_floor_record_ts
    now = time.time()
    if now - _last_floor_record_ts < FLOOR_HISTORY_INTERVAL:
        return
    _last_floor_record_ts = now

    by_name: dict[str, list[float]] = defaultdict(list)
    for cache in (_portals_cache, _fragment_cache, _mrkt_cache, _getgems_cache):
        for it in cache.get("items", []) or []:
            nm = (it.get("name") or "").strip().lower()
            if not nm:
                continue
            for v in (it.get("floor_price"), it.get("price")):
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    by_name[nm].append(fv)

    ts = int(now)
    for nm, prices in by_name.items():
        if len(prices) < 2:
            # При одном лоте floor неотличим от его же цены — не пишем.
            continue
        floor = min(prices)
        dq = _floor_history[nm]
        # Дедуп: если последняя точка совпадает по floor — продлеваем интервал.
        if dq and abs(dq[-1][1] - floor) < 0.001:
            continue
        dq.append((ts, round(floor, 4)))
