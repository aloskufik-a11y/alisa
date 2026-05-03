"""
Главная точка входа — запускает все компоненты системы.
Включает graceful shutdown и валидацию конфига.
"""
import asyncio
import logging
import signal
import sys
import os

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

SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userbot_session")

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
    from telethon import TelegramClient
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

    # 8a. Web App HTTP сервер (если задан WEBAPP_PORT — поднимаем)
    webapp_runner = None
    try:
        webapp_port = int(os.getenv("WEBAPP_PORT", "0") or 0)
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
