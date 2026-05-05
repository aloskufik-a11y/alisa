"""
settings_store.py — Потокобезопасное хранилище настроек.
ВСЕ ЦЕНЫ В TON — убрали max_price_stars, всё единое max_price_ton.
"""
import json
import os
import threading

SETTINGS_FILE = os.getenv("SETTINGS_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.json"
)

_lock = threading.Lock()

DEFAULT_SETTINGS: dict = {
    "max_price_ton": 50.0,              # Макс. цена в TON для ВСЕХ маркетов (абсолютный потолок)
    "min_price_ton": 0.0,               # Мин. цена в TON (нижний порог; 0 = без ограничения)
    "floor_tolerance_pct": 0.0,         # Допустимое превышение Floor (%). 0 = только floor
    "min_discount_pct": 0,              # Мин. скидка от Floor (%) (доп. фильтр)
    "require_floor": True,              # Алертить только лоты с известным floor
    "filter_rarity": [],                # [] = все редкости
    "filter_markets": ["mrkt", "fragment", "portals"],
    "filter_collections": [],           # [] = все коллекции; иначе только эти (по имени)
    "monochrome_only": False,           # Только лоты с монохромным backdrop
    "number_filters": [],               # ['low','sub100','round','repeat','lucky','sequential','palindrome','pretty100']
    "max_rarity_pm": 0,                 # 0 = без фильтра, иначе хотя бы один атрибут ≤ этого pm
    "notifications_on": True,

    # Per-market on/off
    "mrkt_alerts_on": True,
    "fragment_alerts_on": True,
    "portals_alerts_on": True,

    # Тихие часы (UTC). 0-0 = выключено. Пример: 22-7 = тихо с 22:00 до 07:00 UTC.
    "quiet_hours_start": 0,
    "quiet_hours_end":   0,

    # Лимит алертов в одном цикле опроса. 0 = без лимита.
    "max_alerts_per_cycle": 0,

    # Режим "редкие свежие листинги" — алертит даже если price > floor,
    # когда у лота есть атрибут с per-mille ≤ recent_rare_pm.
    "recent_rare_mode": False,
    "recent_rare_pm":   5.0,

    # Watchlist: алертит ЛЮБОЙ новый лот с этими атрибутами,
    # даже если price выше floor. Списки имен (case-insensitive).
    "watchlist_names":     [],   # gift name, e.g. "Plush Pepe"
    "watchlist_models":    [],   # model name, e.g. "Diamond Ring"
    "watchlist_backdrops": [],   # backdrop name, e.g. "Sapphire"

    # Авто-алерт когда floor коллекции внезапно ОПУСТИЛСЯ ниже на N%
    # (берётся snapshot floor каждые 60 сек, разница > порога → алерт).
    "floor_drop_alert":    False,
    "floor_drop_pct":      5.0,

    # Telegram Mini App: публичный HTTPS URL Web App (для кнопки в меню).
    # Пустая строка = кнопка скрыта.
    "mini_app_url": "",
}

# Ключи которые больше не нужны (удаляем при миграции)
_DEPRECATED_KEYS = {"max_price_stars"}


def load_settings() -> dict:
    with _lock:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                changed = False

                # Миграция: убираем устаревшие ключи
                for dep_key in _DEPRECATED_KEYS:
                    if dep_key in data:
                        del data[dep_key]
                        changed = True
                        import logging
                        logging.getLogger(__name__).info(
                            f"Settings: устаревший ключ '{dep_key}' удалён"
                        )

                # Добавляем недостающие ключи из DEFAULT
                for k, v in DEFAULT_SETTINGS.items():
                    if k not in data:
                        data[k] = v
                        changed = True

                if changed:
                    _write(data)
                return data

            except (json.JSONDecodeError, OSError):
                pass

        # Файл не существует или повреждён — создаём с дефолтами
        settings = DEFAULT_SETTINGS.copy()
        _write(settings)
        return settings


def save_settings(settings: dict):
    # Очищаем устаревшие ключи при сохранении
    cleaned = {k: v for k, v in settings.items() if k not in _DEPRECATED_KEYS}
    with _lock:
        _write(cleaned)


def _write(settings: dict):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)
