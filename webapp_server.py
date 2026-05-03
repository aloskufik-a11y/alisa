"""
webapp_server.py — Лёгкий aiohttp-сервер для Telegram Mini App.

Эндпоинты:
  GET /              — индексная HTML-страница SPA
  GET /static/{path} — статика
  GET /api/feed      — JSON со списком последних выгодных лотов
  GET /api/health    — healthcheck

Разворачивается как фоновая task в `main.py`. HTTPS-публикация делается
через `deploy expose` (devinapps tunnel) или внешний reverse-proxy.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from aiohttp import web

from feed_store import snapshot, size

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"
STATIC_DIR = WEBAPP_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"


async def _index(_: web.Request) -> web.Response:
    if not INDEX_FILE.exists():
        return web.Response(text="Web App index.html missing", status=500)
    return web.FileResponse(INDEX_FILE)


async def _feed(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "200"))
    except ValueError:
        limit = 200
    items = snapshot(limit=limit)

    market = (request.query.get("market") or "").lower()
    if market and market != "all":
        items = [i for i in items if i.get("market") == market]

    try:
        min_disc = float(request.query.get("min_disc", "0") or 0)
    except ValueError:
        min_disc = 0.0
    if min_disc > 0:
        out = []
        for it in items:
            f, p = it.get("floor_price"), it.get("price")
            if isinstance(f, (int, float)) and isinstance(p, (int, float)) and f > 0:
                disc = (f - p) / f * 100
                if disc >= min_disc:
                    out.append(it)
        items = out

    return web.json_response({
        "ok": True,
        "count": len(items),
        "items": items,
    })


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "feed_size": size()})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/api/feed", _feed)
    app.router.add_get("/api/health", _health)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


async def run(host: str = "0.0.0.0", port: int = 8088) -> None:
    """Запускает сервер. Не блокирует event loop, остаётся в текущем run loop."""
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"WebApp HTTP сервер запущен на {host}:{port}")
    return runner  # хранится в main.py для shutdown


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("WEBAPP_PORT", "8088"))

    async def _main():
        await run(port=port)
        await asyncio.Event().wait()

    asyncio.run(_main())
