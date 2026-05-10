"""
Конфигурация из переменных окружения (.env).
При старте валидирует критически важные переменные.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int = 0) -> int:
    """Безопасно парсит int из env-переменной (пустая строка → default)."""
    raw = os.getenv(name, "")
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        # Некорректное значение → используем default, но предупреждаем
        print(f"⚠️  {name}={raw!r} не является числом, использую {default}")
        return default


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# ── Userbot (https://my.telegram.org) ──────────────────────────────────────────
API_ID: int = _env_int("API_ID", 0)
API_HASH: str = _env_str("API_HASH", "")

# ── Bot (@BotFather) ───────────────────────────────────────────────────────────
BOT_TOKEN: str = _env_str("BOT_TOKEN", "")
USER_ID: int = _env_int("USER_ID", 0)

# ── Каналы/боты для прослушивания через userbot ────────────────────────────────
# Можно переопределить через .env (CHANNELS=portals,portals_market_bot,main_mrkt_bot,MRKT)
_channels_env = _env_str("CHANNELS", "")
CHANNELS_TO_MONITOR: list[str] = (
    [c.strip() for c in _channels_env.split(",") if c.strip()]
    if _channels_env else
    [
        # MRKT
        "main_mrkt_bot",
        "MRKT",
        # Portals — публичный канал и сам бот, оба отдают новые лоты
        "portals",
        "portals_market_bot",
    ]
)

# ── Интервалы опроса (секунды) ─────────────────────────────────────────────────
# Полный поллинг (все страницы) — низкая частота, чтобы не было 429.
FRAGMENT_POLL_INTERVAL: int = _env_int("FRAGMENT_POLL_INTERVAL", 60)
MRKT_POLL_INTERVAL: int = _env_int("MRKT_POLL_INTERVAL", 60)
GETGEMS_POLL_INTERVAL: int = _env_int("GETGEMS_POLL_INTERVAL", 60)
GETGEMS_API_KEY: str = _env_str("GETGEMS_API_KEY", "")

# Fast-lane поллинг — только 1-я страница (где появляются новые лоты), очень частое.
# Цель: latency «лот появился → алерт» ≤ FAST_POLL_INTERVAL/2 секунд (avg).
# Один HTTP-запрос на цикл = ~7-10 req/min на market — далеко ниже rate-limit
# (MRKT/Portals переваривают 2 req/sec без 429, Fragment.com ставит лимит на ~30 req/min).
# Снижение default 10→8 даёт avg latency ~4с вместо ~5с, без риска 429.
FAST_POLL_INTERVAL: int = _env_int("FAST_POLL_INTERVAL", 8)
FAST_POLL_PAGES: int = _env_int("FAST_POLL_PAGES", 1)

# Параллельная отправка алертов в Telegram. Сем=8 безопасно ниже лимита 30/sec
# на одного пользователя. Поднимать выше 12 не имеет смысла — aiogram + Telegram
# балансируют сами через RetryAfter.
ALERT_DISPATCH_CONCURRENCY: int = _env_int("ALERT_DISPATCH_CONCURRENCY", 8)

# Размер in-memory LRU дедуп-кэша. Спасает от sqlite-roundtrip на hot-path
# fast-lane при опросе ≤8с. 50000 — покрывает ~3-7 дней истории всех маркетов.
DEDUP_CACHE_SIZE: int = _env_int("DEDUP_CACHE_SIZE", 50000)

# ── Фильтрация (исторический верхний предел Stars-цены) ──────────────────────
MAX_PROFITABLE_PRICE_STARS: int = 5000


def validate_config() -> list[str]:
    """Проверяет конфиг и возвращает список предупреждений (не фатальных)."""
    warnings: list[str] = []
    errors: list[str] = []

    if not API_ID or not API_HASH:
        errors.append("❌ API_ID / API_HASH не заданы (нужны для userbot — https://my.telegram.org)")
    if not BOT_TOKEN:
        errors.append("❌ BOT_TOKEN не задан (получить у @BotFather)")
    if not USER_ID:
        warnings.append("⚠️  USER_ID не задан — уведомления не будут отправлены")

    if FRAGMENT_POLL_INTERVAL < 30:
        warnings.append(
            f"⚠️  FRAGMENT_POLL_INTERVAL={FRAGMENT_POLL_INTERVAL}с — слишком часто, "
            "возможен бан Fragment.com за rate limit"
        )
    if MRKT_POLL_INTERVAL < 30:
        warnings.append(
            f"⚠️  MRKT_POLL_INTERVAL={MRKT_POLL_INTERVAL}с — слишком часто, "
            "MRKT API может вернуть 429"
        )

    if errors:
        print("\n".join(errors))
        print("\nЗаполни .env файл и перезапусти бота.")
        print("Пример: скопируй .env.example → .env и заполни значения.\n")
        sys.exit(1)

    return warnings
