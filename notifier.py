"""
Aiogram Bot — уведомления и управление настройками.
ВСЕ ЦЕНЫ В TON. Stars показываются только как справочная информация.
"""
import random
import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, LinkPreviewOptions,
)

from config import BOT_TOKEN, USER_ID
from settings_store import load_settings, save_settings
from url_builder import (
    build_mrkt_gift_link,
    build_mrkt_web_link,
    build_fragment_gift_link,
    build_fragment_collection_link,
)
from logic import format_price, format_stars, _fragment_name_to_slug

logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# FSM: chat_id → тип ожидаемого ввода ("ton" | "discount")
_pending_input: dict[int, str] = {}


def _fmt_int(n: float) -> str:
    """Форматирует целое число с пробелом-разделителем тысяч."""
    try:
        return f"{int(round(n)):,}".replace(",", "\u00a0")
    except (TypeError, ValueError):
        return "?"

# Время старта (для /status)
_start_time: datetime = datetime.now()


# ======================== KEYBOARDS ========================

def main_menu_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    notif_icon = "🔔" if s["notifications_on"] else "🔕"
    require_floor = bool(s.get("require_floor", True))
    rf_icon = "🟢" if require_floor else "⚪"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Макс. цена (TON)",                callback_data="set_max_ton")],
        [InlineKeyboardButton(text="📐 Допуск над Floor (%)",            callback_data="set_floor_tol")],
        [InlineKeyboardButton(text="📉 Мин. скидка от Floor (%)",        callback_data="set_discount")],
        [InlineKeyboardButton(text=f"{rf_icon} Только с известным Floor", callback_data="toggle_require_floor")],
        [InlineKeyboardButton(text="✨ Фильтр по редкости",              callback_data="menu_rarity")],
        [InlineKeyboardButton(text="🏪 Активные маркеты",                callback_data="menu_markets")],
        [InlineKeyboardButton(text=f"{notif_icon} Уведомления",          callback_data="toggle_notif")],
        [InlineKeyboardButton(text="📊 Текущие настройки",               callback_data="show_settings")],
        [InlineKeyboardButton(text="📈 Статус бота",                     callback_data="show_status")],
        [InlineKeyboardButton(text="🎁 Тест — случайный подарок",        callback_data="test_random_gift")],
    ])


def rarity_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    active = s.get("filter_rarity", [])
    rarities = ["Legendary", "Epic", "Rare", "Uncommon", "Common"]
    rows = []
    for r in rarities:
        icon = "✅" if r in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {r}", callback_data=f"rarity_{r}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def markets_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    active = s.get("filter_markets", [])
    markets = [
        ("mrkt",     "🟣 MRKT (tgmrkt.io)"),
        ("fragment", "🔵 Fragment.com"),
        ("portals",  "🟢 GetGems (Portals)"),
    ]
    rows = []
    for key, name in markets:
        icon = "✅" if key in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"market_{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])


# ======================== HELPERS ========================

def _only_owner(user_id: int) -> bool:
    return user_id == USER_ID


# ======================== COMMANDS ========================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not _only_owner(message.from_user.id):
        return
    await message.answer(
        "👋 <b>TG Gift Monitor</b>\n\n"
        "Слежу за подарками на MRKT и Fragment.\n"
        "<b>Все цены в TON 💎</b> — Fragment конвертируется автоматически по актуальному курсу.\n\n"
        "⚙️ <b>Настройки:</b>",
        reply_markup=main_menu_kb()
    )


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    if not _only_owner(message.from_user.id):
        return
    await message.answer("⚙️ <b>Настройки мониторинга:</b>", reply_markup=main_menu_kb())


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not _only_owner(message.from_user.id):
        return
    await _send_status(message.answer)


@dp.message(Command("rate"))
async def cmd_rate(message: types.Message):
    """Показывает текущий курс TON/Stars."""
    if not _only_owner(message.from_user.id):
        return
    from rate_provider import rate_provider
    await rate_provider.ensure_fresh()
    stars_per_ton = _fmt_int(rate_provider.ton_to_stars(1.0))
    ton_per_star = rate_provider.stars_to_ton(1.0)
    text = (
        f"💱 <b>Текущий курс конвертации</b>\n\n"
        f"🌐 TON/USD: <b>${rate_provider.ton_usd:.3f}</b>\n"
        f"⭐ 1 Star = <code>{ton_per_star:.5f}</code> TON\n"
        f"💎 1 TON = <code>~{stars_per_ton}</code> ⭐\n\n"
        f"<i>Обновляется каждые 30 минут</i>"
    )
    await message.answer(text)


# ======================== CALLBACKS ========================

@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ <b>Настройки мониторинга:</b>", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "show_settings")
async def show_settings(callback: CallbackQuery):
    s = load_settings()
    from rate_provider import rate_provider
    await rate_provider.ensure_fresh()

    rarity_text  = ", ".join(s["filter_rarity"]) if s["filter_rarity"] else "Все"
    market_map   = {"mrkt": "MRKT", "fragment": "Fragment", "portals": "GetGems"}
    markets_text = ", ".join(market_map.get(m, m) for m in s.get("filter_markets", [])) or "Все"
    notif_text   = "Включены ✅" if s["notifications_on"] else "Выключены 🔕"
    discount_text = f"{s['min_discount_pct']}%" if s['min_discount_pct'] > 0 else "Без фильтра"

    floor_tol = float(s.get("floor_tolerance_pct", 0.0))
    floor_tol_text = f"+{floor_tol:g}%" if floor_tol > 0 else "только пол (0%)"
    require_floor = bool(s.get("require_floor", True))
    rf_text = "Да ✅" if require_floor else "Нет (риск ложных алертов) ⚠️"

    stars_equiv = _fmt_int(rate_provider.ton_to_stars(s["max_price_ton"]))

    text = (
        f"📊 <b>Текущие настройки:</b>\n\n"
        f"💎 Макс. цена: <b>{s['max_price_ton']} TON</b>\n"
        f"   <i>≈ {stars_equiv} ⭐ на Fragment по текущему курсу</i>\n\n"
        f"📐 Допуск над Floor: <b>{floor_tol_text}</b>\n"
        f"📉 Мин. скидка от Floor: <b>{discount_text}</b>\n"
        f"🟢 Требовать known Floor: <b>{rf_text}</b>\n\n"
        f"✨ Редкости: <b>{rarity_text}</b>\n"
        f"🏪 Маркеты: <b>{markets_text}</b>\n"
        f"🔔 Уведомления: <b>{notif_text}</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_kb())
    await callback.answer()


@dp.callback_query(F.data == "show_status")
async def show_status_cb(callback: CallbackQuery):
    await _send_status(callback.message.answer)
    await callback.answer()


@dp.callback_query(F.data == "toggle_notif")
async def toggle_notif(callback: CallbackQuery):
    s = load_settings()
    s["notifications_on"] = not s["notifications_on"]
    save_settings(s)
    status = "включены ✅" if s["notifications_on"] else "выключены 🔕"
    await callback.answer(f"Уведомления {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=main_menu_kb())


@dp.callback_query(F.data == "menu_rarity")
async def menu_rarity(callback: CallbackQuery):
    await callback.message.edit_text(
        "✨ <b>Фильтр по редкости</b>\n\n"
        "Выбери редкости для уведомлений.\n"
        "Если ничего не выбрано — приходят уведомления обо всех.",
        reply_markup=rarity_kb()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rarity_"))
async def toggle_rarity(callback: CallbackQuery):
    rarity = callback.data.removeprefix("rarity_")
    s = load_settings()
    active = s.get("filter_rarity", [])
    if rarity in active:
        active.remove(rarity)
    else:
        active.append(rarity)
    s["filter_rarity"] = active
    save_settings(s)
    await callback.message.edit_reply_markup(reply_markup=rarity_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu_markets")
async def menu_markets(callback: CallbackQuery):
    await callback.message.edit_text(
        "🏪 <b>Активные маркеты</b>\n\nВыбери, с каких маркетов получать уведомления:",
        reply_markup=markets_kb()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("market_"))
async def toggle_market(callback: CallbackQuery):
    market = callback.data.removeprefix("market_")
    s = load_settings()
    active = s.get("filter_markets", [])
    if market in active:
        active.remove(market)
    else:
        active.append(market)
    s["filter_markets"] = active
    save_settings(s)
    await callback.message.edit_reply_markup(reply_markup=markets_kb())
    await callback.answer()


# ── Установка числовых настроек ──────────────────────────────────────────────

@dp.callback_query(F.data == "set_max_ton")
async def set_max_ton_prompt(callback: CallbackQuery):
    s = load_settings()
    from rate_provider import rate_provider
    await rate_provider.ensure_fresh()
    stars_equiv = _fmt_int(rate_provider.ton_to_stars(s["max_price_ton"]))
    _pending_input[callback.from_user.id] = "ton"
    await callback.message.edit_text(
        f"💎 <b>Максимальная цена в TON</b>\n\n"
        f"Применяется ко ВСЕМ маркетам.\n"
        f"Fragment Stars конвертируются в TON автоматически.\n\n"
        f"Текущее: <b>{s['max_price_ton']} TON</b>\n"
        f"<i>≈ {stars_equiv} ⭐ по текущему курсу</i>\n\n"
        f"Отправь число (например: <code>25.5</code>):",
        reply_markup=back_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "set_discount")
async def set_discount_prompt(callback: CallbackQuery):
    s = load_settings()
    _pending_input[callback.from_user.id] = "discount"
    await callback.message.edit_text(
        f"📉 <b>Минимальная скидка от Floor-цены (%)</b>\n\n"
        f"Текущее: <b>{s['min_discount_pct']}%</b>\n\n"
        f"Отправь число от 0 до 99\n"
        f"(0 = без фильтра по скидке):",
        reply_markup=back_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "set_floor_tol")
async def set_floor_tol_prompt(callback: CallbackQuery):
    s = load_settings()
    _pending_input[callback.from_user.id] = "floor_tol"
    cur = float(s.get("floor_tolerance_pct", 0.0))
    await callback.message.edit_text(
        f"📐 <b>Допуск над Floor (%)</b>\n\n"
        f"Сколько процентов сверху от Floor-цены ещё считать «выгодным».\n"
        f"<b>0%</b> = алертить ТОЛЬКО лоты по Floor.\n"
        f"<b>5%</b> = плюс лоты до 5% выше Floor.\n\n"
        f"Текущее: <b>{cur:g}%</b>\n\n"
        f"Отправь число от 0 до 50:",
        reply_markup=back_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "toggle_require_floor")
async def toggle_require_floor(callback: CallbackQuery):
    s = load_settings()
    cur = bool(s.get("require_floor", True))
    s["require_floor"] = not cur
    save_settings(s)
    status = "только с известным Floor ✅" if s["require_floor"] else "разрешены лоты без Floor ⚠️"
    await callback.answer(f"Теперь: {status}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=main_menu_kb())


@dp.message(F.text.regexp(r"^\d+([\.,]\d+)?$"))
async def handle_number_input(message: types.Message):
    if not _only_owner(message.from_user.id):
        return

    pending = _pending_input.pop(message.from_user.id, None)
    if pending is None:
        return

    # Поддерживаем русскую запятую как десятичный разделитель (5,5 → 5.5)
    try:
        value_f = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Неверный формат числа.", reply_markup=main_menu_kb())
        return
    s = load_settings()

    if pending == "ton":
        if value_f <= 0 or value_f > 100_000:
            await message.answer("⚠️ Введи значение от 0.01 до 100 000 TON.", reply_markup=main_menu_kb())
            return
        s["max_price_ton"] = round(value_f, 4)
        save_settings(s)
        from rate_provider import rate_provider
        await rate_provider.ensure_fresh()
        stars_equiv = _fmt_int(rate_provider.ton_to_stars(s["max_price_ton"]))
        await message.answer(
            f"✅ Макс. цена: <b>{s['max_price_ton']} TON</b>\n"
            f"<i>≈ {stars_equiv} ⭐ по текущему курсу</i>",
            reply_markup=main_menu_kb()
        )

    elif pending == "discount":
        if value_f < 0 or value_f >= 100:
            await message.answer("⚠️ Введи значение от 0 до 99.", reply_markup=main_menu_kb())
            return
        s["min_discount_pct"] = int(value_f)
        save_settings(s)
        label = f"{s['min_discount_pct']}%" if s["min_discount_pct"] > 0 else "без фильтра"
        await message.answer(
            f"✅ Мин. скидка: <b>{label}</b>",
            reply_markup=main_menu_kb()
        )

    elif pending == "floor_tol":
        if value_f < 0 or value_f > 50:
            await message.answer("⚠️ Введи значение от 0 до 50.", reply_markup=main_menu_kb())
            return
        s["floor_tolerance_pct"] = round(value_f, 2)
        save_settings(s)
        label = f"+{s['floor_tolerance_pct']:g}%" if s["floor_tolerance_pct"] > 0 else "только пол (0%)"
        await message.answer(
            f"✅ Допуск над Floor: <b>{label}</b>",
            reply_markup=main_menu_kb()
        )

    else:
        await message.answer(
            "⚠️ Используй кнопки меню для выбора параметра.",
            reply_markup=main_menu_kb()
        )


# ── Тест — случайный подарок ─────────────────────────────────────────────────

RANDOM_GIFT_NAMES = [
    "Eternal Rose", "Golden Star", "Neon Skull", "Crystal Dragon",
    "Mystic Flame", "Lucky Clover", "Iron Crown", "Shadow Wolf",
    "Diamond Ring", "Cosmic Cat", "Plush Pepe", "Lol Pop",
]
RANDOM_MARKETS  = ["mrkt", "fragment"]
RANDOM_RARITIES = ["Legendary", "Epic", "Rare", "Uncommon"]


@dp.callback_query(F.data == "test_random_gift")
async def test_random_gift(callback: CallbackQuery):
    from rate_provider import rate_provider
    await rate_provider.ensure_fresh()

    name   = random.choice(RANDOM_GIFT_NAMES)
    rarity = random.choice(RANDOM_RARITIES)
    market = random.choice(RANDOM_MARKETS)
    number = str(random.randint(1, 9999))
    fake_id = str(random.randint(100000, 999999))

    price_ton = round(random.uniform(0.5, 30.0), 2)
    floor_ton = round(price_ton + random.uniform(1.0, 15.0), 2)

    gift: dict = {
        "id": fake_id,
        "name": name,
        "slug": f"{name.lower().replace(' ', '-')}-{number}",
        "number": number,
        "rarity": rarity,
        "price": price_ton,
        "price_ton": price_ton,
        "floor_price": floor_ton,
        "currency": "TON",
        "image_url": "",
    }

    if market == "fragment":
        # Добавляем оригинальные Stars для Fragment
        stars_price = round(price_ton / rate_provider.stars_to_ton(1.0))
        floor_stars = round(floor_ton / rate_provider.stars_to_ton(1.0))
        gift["stars_price"] = stars_price
        gift["floor_stars"] = floor_stars
        gift["url"] = f"https://fragment.com/gift/{name.replace(' ', '').lower()}-{number}"

    await callback.answer("✅ Тестовое уведомление отправлено!")
    await send_gift_alert(bot, USER_ID, gift, market)


# ======================== STATUS ========================

async def _send_status(send_fn):
    """Собирает и отправляет статус бота."""
    from database import get_stats
    from rate_provider import rate_provider

    uptime = datetime.now() - _start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}ч {minutes}м {seconds}с"

    try:
        stats = get_stats()
        sources_text = "\n".join(
            f"  • <code>{src}</code>: {cnt}"
            for src, cnt in stats["by_source"].items()
        ) or "  (нет данных)"

        await rate_provider.ensure_fresh()
        rate_info = rate_provider.format_rate_info()

        text = (
            f"📈 <b>Статус бота</b>\n\n"
            f"⏱ Аптайм: <b>{uptime_str}</b>\n\n"
            f"{rate_info}\n\n"
            f"🗄 <b>База данных:</b>\n"
            f"  • Всего записей: <b>{stats['total']}</b>\n"
            f"  • Сегодня: <b>{stats['today']}</b>\n"
            f"  По источникам:\n{sources_text}"
        )
    except Exception as e:
        text = f"📈 <b>Статус бота</b>\n\n⏱ Аптайм: <b>{uptime_str}</b>\n\n❌ Ошибка: {e}"

    await send_fn(text, reply_markup=back_kb())


# ======================== SEND ALERT ========================

async def send_gift_alert(bot_instance: Bot, chat_id: int, gift: dict, market: str):
    """
    Отправляет карточку подарка в Telegram.
    Все цены в TON. Stars показываются как дополнительная инфо для Fragment.
    """
    name      = gift.get("name", "Unknown")
    number    = gift.get("number", "")
    slug      = gift.get("slug", "")
    raw_id    = gift.get("id", "")
    price     = gift.get("price", 0)        # TON
    floor     = gift.get("floor_price")     # TON
    rarity    = gift.get("rarity", "")
    image_url = gift.get("image_url", "")
    stars_price = gift.get("stars_price")   # Stars (оригинал для Fragment)
    floor_stars = gift.get("floor_stars")   # Stars floor (для Fragment)

    # ── Ссылки и кнопки ─────────────────────────────────────────────────────
    if market in ("mrkt", "main_mrkt_bot"):
        tg_link  = build_mrkt_gift_link(raw_id, slug, name, number)
        web_link = build_mrkt_web_link(slug, raw_id)
        frag_slug = f"{_fragment_name_to_slug(name)}-{number}" if name and number else None
        frag_url  = f"https://fragment.com/gift/{frag_slug}" if frag_slug else None

        market_icon = "🟣 MRKT"
        gift_url    = tg_link
        buttons = [
            [InlineKeyboardButton(text="🟣 Открыть в MRKT",       url=tg_link)],
            [InlineKeyboardButton(text="🌐 Веб (mrkt.fun)",        url=web_link)],
        ]
        if frag_url:
            buttons.append([InlineKeyboardButton(text="🔵 Посмотреть на Fragment", url=frag_url)])

    elif market == "fragment":
        gift_url   = gift.get("url") or build_fragment_gift_link(raw_id, slug, name, number)
        col_url    = build_fragment_collection_link(name) if name else "https://fragment.com/gifts"
        market_icon = "🔵 Fragment"
        buttons = [
            [InlineKeyboardButton(text="🔵 Открыть на Fragment",   url=gift_url)],
            [InlineKeyboardButton(text="📋 Все лоты коллекции",    url=col_url)],
        ]

    elif market in ("portals", "getgems"):
        gift_url   = f"https://getgems.io/nft/{raw_id}"
        market_icon = "🟢 GetGems"
        buttons = [[InlineKeyboardButton(text="🟢 Открыть на GetGems", url=gift_url)]]

    else:
        gift_url   = f"https://t.me/{market}"
        market_icon = market
        buttons = [[InlineKeyboardButton(text="🔗 Открыть", url=gift_url)]]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    # ── Форматирование текста ────────────────────────────────────────────────
    price_str = format_price(price)
    num_text  = f" #{number}" if number else ""

    # Для Fragment добавляем Stars-эквивалент
    stars_hint = ""
    if stars_price and market == "fragment":
        stars_hint = f" <i>({format_stars(stars_price)})</i>"

    floor_line    = ""
    discount_line = ""
    if floor and isinstance(floor, (int, float)) and floor > 0:
        floor_str  = format_price(floor)
        # Для Fragment добавляем Stars-эквивалент floor
        floor_stars_hint = ""
        if floor_stars and market == "fragment":
            floor_stars_hint = f" <i>({format_stars(floor_stars)})</i>"
        floor_line = f"\n📊 Floor: <b>{floor_str}</b>{floor_stars_hint}"
        if price > 0 and floor > price:
            pct = round((floor - price) / floor * 100, 1)
            discount_line = f"\n📉 Скидка: <b>{pct}%</b> ниже Floor"

    rarity_emoji_map = {
        "Legendary": "🟡", "Epic": "🟣", "Rare": "🔵",
        "Uncommon": "🟢", "Common": "⚪",
    }
    rarity_icon = rarity_emoji_map.get(rarity, "✨")
    rarity_line = f"\n{rarity_icon} Редкость: <b>{rarity}</b>" if rarity else ""

    caption = (
        f"🚀 <b>Выгодный лот!</b>\n\n"
        f"🎁 <b>{name}{num_text}</b>"
        f"{rarity_line}\n"
        f"💰 Цена: <b>{price_str}</b>{stars_hint}"
        f"{floor_line}{discount_line}\n"
        f"🏪 {market_icon}"
    )

    try:
        if image_url:
            try:
                await bot_instance.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass  # Fallback на текст

        await bot_instance.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        logger.error(f"send_gift_alert ошибка: {e}")


async def send_alert(text: str):
    """Простая отправка текста (fallback)."""
    s = load_settings()
    if not s.get("notifications_on", True):
        return
    if not USER_ID:
        logger.warning("USER_ID не задан. Уведомление не отправлено.")
        return
    try:
        await bot.send_message(chat_id=USER_ID, text=text)
    except Exception as e:
        logger.error(f"send_alert ошибка: {e}")


async def start_notifier():
    """Запускает aiogram polling."""
    logger.info("Notifier bot запускается...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
