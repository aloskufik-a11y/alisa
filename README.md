# TG Gift Monitor

Telegram-бот для мониторинга подарков на маркетплейсах MRKT, Fragment.com и
GetGems (Portals). Все цены приводятся к **TON** (Stars автоматически
конвертируются по актуальному курсу).

## Возможности

- **MRKT (tgmrkt.io)** — мониторинг через Telegram Mini App API (с
  авторизацией через Telethon `RequestWebView`).
- **Fragment.com** — HTML-скрейпинг публичной страницы `/gifts` с фильтром
  `filter=sale` и сортировками `price_asc` / `listed`.
- **GetGems (Portals)** — GraphQL `alphaSearch` с ценами в наноТОН.
- **Telegram-каналы** — userbot слушает указанные каналы и парсит
  объявления о продаже подарков через regex.
- **Уведомления** — отдельный aiogram-бот отправляет карточки выгодных
  лотов с inline-кнопками (Settings, Status, Rate, Test).
- **Дедупликация** — SQLite в WAL-режиме, чистка устаревших записей раз
  в 12 часов.
- **Фильтрация** — макс. цена в TON, мин. скидка от Floor (%), редкость,
  активные маркеты — настраивается через `/settings`.
- **Курс** — TONapi → CoinGecko → Binance (fallback chain), кеш 30 минут.

## Требования

- Python **3.12+**
- Telegram API credentials ([my.telegram.org](https://my.telegram.org))
- Telegram бот ([@BotFather](https://t.me/BotFather))
- Свой Telegram user ID ([@userinfobot](https://t.me/userinfobot))

## Установка

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполни .env своими значениями
python main.py
```

При первом запуске Telethon попросит ввести номер телефона и код
подтверждения для userbot-сессии. Сессия сохранится в файле
`userbot_session.session`.

## Структура проекта

```
main.py              # Точка входа — поднимает Telethon, бота, скраперы
config.py            # Загрузка и валидация .env
database.py          # SQLite с WAL — дедупликация подарков
settings_store.py    # JSON-настройки фильтров (thread-locked)
rate_provider.py     # Курс TON/USD с fallback-цепочкой
logic.py             # Парсеры API/HTML и фильтр is_profitable
url_builder.py       # Построение ссылок на маркеты

mini_app_scraper.py  # MRKT (Telethon WebApp + JWT)
fragment_scraper.py  # Fragment.com (HTML-скрейпинг)
userbot.py           # Telethon-обработчики каналов
notifier.py          # aiogram-бот: команды, FSM, отправка алертов
tg_message_parser.py # Regex-парсер текстовых объявлений

test_mrkt.py         # Юнит-тесты основной логики
find_mrkt_links.py   # Утилита для отладки API/парсеров
check_channels.py    # Проверка доступа к каналам
```

## Команды бота

- `/start` — главное меню (inline-кнопки)
- `/settings` — фильтры (макс. цена, мин. скидка, редкость, маркеты)
- `/status` — uptime, статистика БД, активные модули
- `/rate` — текущий курс TON/USD и Stars↔TON

## Конфиг (`settings.json`)

```jsonc
{
  "max_price_ton": 50.0,           // Макс. цена в TON
  "min_discount_pct": 0,            // Мин. скидка от Floor (%)
  "filter_rarity": [],              // ["Legendary","Epic"...] или []
  "filter_markets": ["mrkt", "fragment"],  // активные маркеты
  "notifications_on": true
}
```

## Тесты

```bash
python test_mrkt.py
```

Покрывает: helpers, парсеры (MRKT JSON, Fragment JSON+HTML, Portals
GraphQL, Telegram messages), is_profitable, url_builder, БД, settings.

## Утилиты

- `find_mrkt_links.py` — показывает живой ответ MRKT API (без auth — 401
  ожидается) и Fragment, прогоняет парсеры.
- `check_channels.py` — проверяет, что userbot имеет доступ к
  `CHANNELS_TO_MONITOR`.

## Безопасность

- `.env` и `*.session` НЕ попадают в git (см. `.gitignore`).
- Все секреты — только в переменных окружения.
- Bot отвечает только владельцу (`USER_ID`).
