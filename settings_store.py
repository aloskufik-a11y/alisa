"""
settings_store.py — Потокобезопасное хранилище настроек.
ВСЕ ЦЕНЫ В TON — убрали max_price_stars, всё единое max_price_ton.
"""
import json
import os
import threading

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

_lock = threading.Lock()

DEFAULT_SETTINGS: dict = {
    "max_price_ton": 50.0,              # Макс. цена в TON для ВСЕХ маркетов
    "min_discount_pct": 0,              # Мин. скидка от Floor (%)
    "filter_rarity": [],                # [] = все редкости
    "filter_markets": ["mrkt", "fragment"],
    "notifications_on": True,
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
