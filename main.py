"""
Главная точка входа — запускает все компоненты системы.
Включает graceful shutdown и валидацию конфига.
"""
import asyncio
import logging
import signal
import sys
import os
import time

# ─── Windows: нужен SelectorEventLoop для Telethon ──────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-22s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Шум от сторонних библиотек
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

logger = logging.getLogger("main")

SESSION_PATH = os.getenv("SESSION_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "userbot_session"
)

# Флаг для graceful shutdown
_shutdown_event: asyncio.Event | None = None


async def main():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    # 1. Валидация конфига (выходит с кодом 1 если критические поля пустые)
    from config import validate_config, API_ID, API_HASH
    warnings = validate_config()
    for w in warnings:
        logger.warning(w)

    # 2. Инициализация БД
    from database import init_db
    init_db()

    # 3. Создаём Telethon клиент
    # На хостингах без persistent volume (Koyeb free / Render) удобнее
    # передавать сессию через env-var TELEGRAM_STRING_SESSION; локально по-прежнему
    # используется файл userbot_session.session.
    from telethon import TelegramClient
    string_session = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
    if string_session:
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(string_session), API_ID, API_HASH)
        logger.info("Using TELEGRAM_STRING_SESSION from env (no .session file needed)")
    else:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    logger.info("Подключение Telethon...")
    await client.start()

    me = await client.get_me()
    logger.info(f"✅ Авторизован: {me.first_name} (@{me.username})")

    # 4. Подключаем Telethon клиент к скраперу
    from mini_app_scraper import set_client, start_mini_app_scrapers
    set_client(client)

    # 5. Регистрируем обработчики каналов
    from userbot import register_userbot_handlers
    register_userbot_handlers(client)

    # 6. Запускаем фоновые скраперы (Mini App)
    await start_mini_app_scrapers()

    logger.info("🚀 Все компоненты запущены!")

    # 7. Импорт нотификатора и Fragment-монитора
    from notifier import start_notifier
    from fragment_scraper import start_fragment_monitor

    # 8. Периодическая очистка БД (каждые 12 часов)
    async def periodic_cleanup():
        from database import cleanup_old_gifts
        while not _shutdown_event.is_set():
            await asyncio.sleep(12 * 3600)
            deleted = cleanup_old_gifts(days=14)
            if deleted:
                logger.info(f"БД: очищено {deleted} устаревших записей")

    asyncio.create_task(periodic_cleanup(), name="db_cleanup")

    # 8b. Если настроен публичный backend — периодически шлём snapshot настроек
    async def periodic_settings_push():
        from feed_store import push_settings
        from settings_store import load_settings
        if not os.getenv("WEBAPP_BACKEND_URL"):
            return
        while not _shutdown_event.is_set():
            try:
                await push_settings(load_settings())
            except Exception:
                logger.exception("settings push failed")
            await asyncio.sleep(60)

    asyncio.create_task(periodic_settings_push(), name="settings_push")

    # 8c. Если настроен backend — поллим изменения настроек из Mini App
    async def periodic_settings_pull():
        from feed_store import pull_pending_settings, push_settings
        from settings_store import load_settings, save_settings
        if not os.getenv("WEBAPP_BACKEND_URL"):
            return
        last_applied_ts = 0
        while not _shutdown_event.is_set():
            try:
                data = await pull_pending_settings(since_ts=last_applied_ts)
                if data and data.get("changed"):
                    new_ts = int(data.get("ts", 0))
                    incoming = data.get("settings") or {}
                    if isinstance(incoming, dict) and incoming:
                        cur = load_settings()
                        # Применяем только пришедшие ключи
                        cur.update(incoming)
                        save_settings(cur)
                        last_applied_ts = new_ts
                        logger.info(
                            f"Mini App settings applied: {list(incoming.keys())}"
                        )
                        # Подтверждаем backend, что изменения применены
                        await push_settings(cur)
            except Exception:
                logger.exception("settings pull failed")
            await asyncio.sleep(15)

    asyncio.create_task(periodic_settings_pull(), name="settings_pull")

    # 8d. Поллим тестовый алерт от Mini App
    async def periodic_test_alert():
        from feed_store import pull_pending_test_alert
        from notifier import send_alert
        if not os.getenv("WEBAPP_BACKEND_URL"):
            return
        last_ts = int(time.time())
        while not _shutdown_event.is_set():
            try:
                ts = await pull_pending_test_alert(since_ts=last_ts)
                if ts and ts > last_ts:
                    last_ts = ts
                    await send_alert(
                        "🔔 <b>Тест Mini App</b>\n"
                        "Это сообщение пришло по запросу из веб-приложения. "
                        "Канал доставки уведомлений работает."
                    )
                    logger.info("Mini App test alert sent")
            except Exception:
                logger.exception("test alert poll failed")
            await asyncio.sleep(8)

    asyncio.create_task(periodic_test_alert(), name="test_alert_pull")

    # 8e. Self keep-alive ping — не даём Render free Web Service уснуть.
    # Render помечает контейнер idle если 15 мин нет HTTP-трафика. Раз в 10 минут
    # дёргаем свой публичный URL — request возвращается обратно к нам и сбрасывает таймер.
    # Активно только если RENDER_EXTERNAL_URL выставлен (Render автоматически его пробрасывает).
    async def periodic_self_ping():
        external = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if not external:
            return
        import aiohttp
        url = f"{external}/healthz"
        logger.info(f"Self keep-alive ping enabled → {url}")
        while not _shutdown_event.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        logger.debug(f"Self-ping {r.status}")
            except Exception as e:
                logger.debug(f"Self-ping failed: {e}")
            await asyncio.sleep(600)

    asyncio.create_task(periodic_self_ping(), name="self_ping")

    # 8a. Web App HTTP сервер.
    # Поднимаем если задан WEBAPP_PORT (локально) или PORT (Render/Railway/etc).
    # На Render free Web Service бот ОБЯЗАН слушать $PORT, иначе контейнер
    # помечается как unhealthy и не получает трафика → не разбудится keep-alive ping.
    webapp_runner = None
    port_str = os.getenv("WEBAPP_PORT") or os.getenv("PORT") or "0"
    try:
        webapp_port = int(port_str)
    except ValueError:
        webapp_port = 0
    if webapp_port > 0:
        try:
            import webapp_server
            webapp_runner = await webapp_server.run(
                host=os.getenv("WEBAPP_HOST", "0.0.0.0"),
                port=webapp_port,
            )
        except Exception as e:
            logger.exception(f"Не удалось запустить Web App сервер: {e}")

    # 9. Запускаем всё вместе
    try:
        await asyncio.gather(
            start_fragment_monitor(),
            start_notifier(),
            client.run_until_disconnected(),
        )
    except asyncio.CancelledError:
        logger.info("Задачи отменены, завершаем...")
    finally:
        logger.info("Закрываем соединения...")
        if webapp_runner is not None:
            try:
                await webapp_runner.cleanup()
            except Exception:
                pass
        await client.disconnect()
        logger.info("Бот остановлен. До свидания! 👋")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Бот остановлен пользователем.")
    except SystemExit as e:
        sys.exit(e.code)
