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
# Можно переопределить через .env (CHANNELS=portals,main_mrkt_bot,MRKT)
_channels_env = _env_str("CHANNELS", "")
CHANNELS_TO_MONITOR: list[str] = (
    [c.strip() for c in _channels_env.split(",") if c.strip()]
    if _channels_env else
    ["portals", "main_mrkt_bot", "MRKT"]
)

# ── Интервалы опроса (секунды) ─────────────────────────────────────────────────
FRAGMENT_POLL_INTERVAL: int = _env_int("FRAGMENT_POLL_INTERVAL", 60)
MRKT_POLL_INTERVAL: int = _env_int("MRKT_POLL_INTERVAL", 90)

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
