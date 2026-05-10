"""
Aiogram Bot — уведомления и управление настройками.
ВСЕ ЦЕНЫ В TON. Stars показываются только как справочная информация.
"""
import html
import os
import random
import asyncio
import logging
from datetime import datetime


def _esc(text) -> str:
    """HTML-escape для безопасной вставки текста в parse_mode=HTML.

    Защищает от того, что AI-провайдер или внешняя строка вернёт текст с
    символами <, >, &. aiogram.parse_mode=HTML парсит такие как разметку
    и упадёт TelegramBadRequest, ломая алерт.
    Принимает любой тип, конвертирует в str."""
    return html.escape("" if text is None else str(text), quote=False)

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
    build_mrkt_web_link,
    build_market_buttons,
)
from logic import (
    format_price,
    format_stars,
    number_categories,
    is_monochrome,
    number_filter_label,
    all_number_filter_categories,
)

logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# FSM: chat_id → тип ожидаемого ввода ("ton" | "discount")
_pending_input: dict[int, str] = {}

# Кэш недавно отправленных алертов: cache_key (market_id) → gift dict.
# Используется для callback "🤖 Спросить AI" — id в callback_data уже не
# хранит весь gift (TG limit 64 bytes), поэтому подтягиваем из кэша.
# LRU поведение: при превышении 300 шт. удаляем самые старые.
_alert_cache: dict[str, dict] = {}
_ALERT_CACHE_MAX = 300


def _cache_alert(gift: dict, market: str) -> str | None:
    """Сохраняет gift по market+id, возвращает cache_key (≤ 50 байт)
    или None если у лота нет id."""
    raw_id = gift.get("id")
    if raw_id is None or raw_id == "":
        return None
    # Telegram callback_data: ≤ 64 bytes total. "ai|" префикс = 3, оставляем 50 для key.
    key = f"{market}_{str(raw_id)}"[:50]
    _alert_cache[key] = {"gift": dict(gift), "market": market}
    # Простая очистка: если переполнили, удаляем 50 самых старых (FIFO).
    if len(_alert_cache) > _ALERT_CACHE_MAX:
        for k in list(_alert_cache.keys())[: len(_alert_cache) - _ALERT_CACHE_MAX]:
            _alert_cache.pop(k, None)
    return key


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
    notif_icon = "🔔" if s.get("notifications_on", True) else "🔕"
    # URL берётся из настроек или из переменной окружения как fallback,
    # чтобы кнопка не пропадала, если settings.json случайно очистится.
    mini_app_url = (
        (s.get("mini_app_url") or "").strip()
        or os.getenv("MINI_APP_URL", "").strip()
        or os.getenv("WEBAPP_BACKEND_URL", "").strip()
    )
    rows = [
        [InlineKeyboardButton(text="💎 Цена и Floor",         callback_data="menu_price")],
        [InlineKeyboardButton(text="🎯 Фильтры подарков",     callback_data="menu_filters")],
        [InlineKeyboardButton(text="🏪 Маркеты",              callback_data="menu_markets")],
        [InlineKeyboardButton(text=f"{notif_icon} Уведомления", callback_data="menu_notifs")],
        [InlineKeyboardButton(text="📊 Текущие настройки",     callback_data="show_settings")],
        [InlineKeyboardButton(text="📈 Статус бота",           callback_data="show_status")],
        [InlineKeyboardButton(text="🎁 Тест-уведомление",       callback_data="test_random_gift")],
    ]
    # Если задан URL — показываем кнопку запуска Mini App
    if mini_app_url:
        try:
            from aiogram.types import WebAppInfo
            rows.insert(
                0,
                [InlineKeyboardButton(
                    text="🪄 Открыть Web App с лентой",
                    web_app=WebAppInfo(url=mini_app_url),
                )]
            )
        except Exception:
            pass
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notifs_menu_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    on = bool(s.get("notifications_on", True))
    icon_main = "🔔" if on else "🔕"
    mrkt_on = bool(s.get("mrkt_alerts_on", True))
    frag_on = bool(s.get("fragment_alerts_on", True))
    port_on = bool(s.get("portals_alerts_on", True))
    qs = int(s.get("quiet_hours_start", 0) or 0)
    qe = int(s.get("quiet_hours_end", 0) or 0)
    quiet_text = "выкл." if qs == qe else f"{qs:02d}:00–{qe:02d}:00 UTC"
    cycle = int(s.get("max_alerts_per_cycle", 0) or 0)
    cycle_text = f"{cycle}/цикл" if cycle > 0 else "без лимита"
    rare_mode = "🟢" if bool(s.get("recent_rare_mode", False)) else "⚪"
    rare_pm = float(s.get("recent_rare_pm", 5.0) or 5.0)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{icon_main} Все уведомления",                callback_data="toggle_notif")],
        [InlineKeyboardButton(text=f"{'✅' if mrkt_on else '⬜'} 🟣 MRKT алерты",  callback_data="toggle_mrkt_alerts")],
        [InlineKeyboardButton(text=f"{'✅' if frag_on else '⬜'} 🔵 Fragment алерты", callback_data="toggle_fragment_alerts")],
        [InlineKeyboardButton(text=f"{'✅' if port_on else '⬜'} 🟢 Portals алерты", callback_data="toggle_portals_alerts")],
        [InlineKeyboardButton(text=f"🌙 Тихие часы: {quiet_text}",                callback_data="set_quiet_hours")],
        [InlineKeyboardButton(text=f"📊 Лимит: {cycle_text}",                     callback_data="set_max_per_cycle")],
        [InlineKeyboardButton(text=f"{rare_mode} Редкие свежие (≤{rare_pm:g}‰)", callback_data="toggle_recent_rare")],
        [InlineKeyboardButton(text=f"💠 Порог редкости: {rare_pm:g}‰",            callback_data="set_recent_rare_pm")],
        [InlineKeyboardButton(text="◀️ Назад",                                    callback_data="back_main")],
    ])


def price_menu_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    require_floor = bool(s.get("require_floor", True))
    rf_icon = "🟢" if require_floor else "⚪"
    max_p = s.get("max_price_ton", 50)
    min_p = s.get("min_price_ton", 0) or 0
    floor_tol = float(s.get("floor_tolerance_pct", 0))
    min_disc = int(s.get("min_discount_pct", 0))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Макс. цена: {max_p} TON",               callback_data="set_max_ton")],
        [InlineKeyboardButton(text=f"💵 Мин. цена: {min_p} TON",                callback_data="set_min_ton")],
        [InlineKeyboardButton(text=f"📐 Допуск над Floor: {floor_tol:g}%",      callback_data="set_floor_tol")],
        [InlineKeyboardButton(text=f"📉 Мин. скидка от Floor: {min_disc}%",     callback_data="set_discount")],
        [InlineKeyboardButton(text=f"{rf_icon} Только с известным Floor",       callback_data="toggle_require_floor")],
        [InlineKeyboardButton(text="◀️ Назад",                                  callback_data="back_main")],
    ])


def filters_menu_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    mono = bool(s.get("monochrome_only", False))
    mono_icon = "🟢" if mono else "⚪"
    rar_n = len(s.get("filter_rarity", []))
    col_n = len(s.get("filter_collections", []))
    num_n = len(s.get("number_filters", []))
    rar_pm = float(s.get("max_rarity_pm", 0) or 0)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mono_icon} Только монохромные backdrop",  callback_data="toggle_monochrome")],
        [InlineKeyboardButton(text=f"#️⃣ По номеру подарка ({num_n})",          callback_data="menu_numbers")],
        [InlineKeyboardButton(text=f"✨ По редкости ({rar_n})",                  callback_data="menu_rarity")],
        [InlineKeyboardButton(text=f"💠 Макс. rarity (per-mille): {rar_pm:g}",   callback_data="set_max_rarity_pm")],
        [InlineKeyboardButton(text=f"📚 По коллекциям ({col_n})",                callback_data="menu_collections")],
        [InlineKeyboardButton(text="◀️ Назад",                                  callback_data="back_main")],
    ])


def numbers_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    active = set(s.get("number_filters", []))
    rows = []
    for cat in all_number_filter_categories():
        icon = "✅" if cat in active else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {number_filter_label(cat)}",
            callback_data=f"num_{cat}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_filters")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rarity_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    active = s.get("filter_rarity", [])
    rarities = ["Legendary", "Epic", "Rare", "Uncommon", "Common"]
    rows = []
    for r in rarities:
        icon = "✅" if r in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {r}", callback_data=f"rarity_{r}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_filters")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def collections_kb(page: int = 0) -> InlineKeyboardMarkup:
    """
    Показывает список коллекций из MRKT floor cache (если есть) с тогглами.
    Поддерживает простую пагинацию по 10 в страницу.
    """
    from floor_cache import _mrkt
    s = load_settings()
    active = set(s.get("filter_collections", []))
    all_cols = sorted(_mrkt._data.keys()) if _mrkt._data else []
    if not all_cols:
        # Берём отсюда же что юзер уже добавил (хотя бы можно убрать)
        all_cols = sorted(active)

    PAGE = 10
    pages = max(1, (len(all_cols) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = all_cols[page * PAGE: (page + 1) * PAGE]

    rows = []
    for c in chunk:
        icon = "✅" if c in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {c}", callback_data=f"col_{c[:60]}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Стр.", callback_data=f"colpage_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="colpage_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Стр. ▶️", callback_data=f"colpage_{page+1}"))
    if nav:
        rows.append(nav)

    if active:
        rows.append([InlineKeyboardButton(text=f"🗑 Сбросить ({len(active)})", callback_data="col_reset")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_filters")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def markets_kb() -> InlineKeyboardMarkup:
    s = load_settings()
    active = s.get("filter_markets", [])
    markets = [
        ("mrkt",     "🟣 MRKT (mrkt.fun)"),
        ("fragment", "🔵 Fragment.com"),
        ("portals",  "🟢 Portals (portal-market.com)"),
    ]
    rows = []
    for key, name in markets:
        icon = "✅" if key in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"market_{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(target: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=target)]
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


@dp.message(Command("setwebapp"))
async def cmd_setwebapp(message: types.Message):
    """Привязать публичный HTTPS-URL Mini App. Без аргумента — отвязывает."""
    if not _only_owner(message.from_user.id):
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    s = load_settings()
    if len(parts) < 2 or not parts[1].strip():
        s["mini_app_url"] = ""
        save_settings(s)
        await message.answer(
            "✅ Mini App URL очищен. Кнопка скрыта в меню.",
            reply_markup=main_menu_kb()
        )
        return
    url = parts[1].strip()
    if not url.startswith("https://"):
        await message.answer("⚠️ URL должен начинаться с <b>https://</b>.")
        return
    s["mini_app_url"] = url
    save_settings(s)
    await message.answer(
        f"✅ Mini App URL сохранён:\n<code>{url}</code>\n\n"
        f"Открой /settings — внизу появится кнопка «🪄 Открыть Web App».",
        reply_markup=main_menu_kb()
    )


@dp.message(Command("ai_test"))
async def cmd_ai_test(message: types.Message):
    """Проверяет что AI-провайдер настроен и отвечает."""
    if not _only_owner(message.from_user.id):
        return
    from ai_advisor import get_active_provider, test_provider
    s = load_settings()
    provider_name = (s.get("ai_provider") or "off").lower()
    if provider_name == "off":
        await message.answer(
            "🤖 AI-провайдер не выбран.\n\n"
            "Откройте Mini App → Настройки → 🤖 AI-помощник, выберите Groq или Gemini, "
            "вставьте API-ключ и сохраните."
        )
        return
    await message.answer(f"🤖 Тестирую {provider_name}…")
    provider = get_active_provider(s)
    ok, text = await test_provider(provider)
    if ok:
        await message.answer(
            f"✅ <b>{_esc(provider_name)}</b> работает\n\n"
            f"<i>Ответ модели:</i>\n<code>{_esc(text[:500])}</code>"
        )
    else:
        await message.answer(
            f"❌ <b>{_esc(provider_name)}</b> не отвечает\n\n<i>{_esc(text)}</i>"
        )


@dp.message(Command("digest"))
async def cmd_digest(message: types.Message):
    """Шлёт daily digest прямо сейчас (для проверки)."""
    if not _only_owner(message.from_user.id):
        return
    from daily_digest import send_digest_now
    s = load_settings()
    window = int(s.get("daily_digest_window_hours", 24))
    await message.answer(f"📊 Считаю digest за последние {window}ч…")
    ok = await send_digest_now(message.bot, message.from_user.id, window_hours=window)
    if not ok:
        await message.answer("❌ Не удалось сформировать digest. Смотри логи.")


@dp.message(Command("ai_ask"))
async def cmd_ai_ask(message: types.Message):
    """Свободный диалог с AI. Использование: /ai_ask <вопрос>"""
    if not _only_owner(message.from_user.id):
        return
    from ai_advisor import get_active_provider, get_fallback_provider, free_chat
    s = load_settings()
    provider = get_active_provider(s)
    fallback = get_fallback_provider(s)
    if provider is None:
        await message.answer(
            "🤖 AI-провайдер не настроен.\n\n"
            "Mini App → Настройки → 🤖 AI-помощник → выбрать провайдер + ключ."
        )
        return
    text = (message.text or "")
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: <code>/ai_ask &lt;вопрос&gt;</code>\n\n"
            "Пример: <code>/ai_ask Как сейчас рынок Plush Pepe?</code>"
        )
        return
    question = parts[1].strip()[:1000]
    placeholder = await message.answer(f"🤖 Думаю над вопросом…")
    answer = await free_chat(provider, question, fallback=fallback)
    if not answer:
        await placeholder.edit_text(
            "❌ AI не ответил. Возможно лимит токенов или ошибка ключа. "
            "Проверьте /ai_test."
        )
        return
    provider_emoji = {"groq": "⚡", "gemini": "✨"}.get(
        (s.get("ai_provider") or "").lower(), "🤖"
    )
    await placeholder.edit_text(
        f"{provider_emoji} <b>AI</b>\n\n{_esc(answer[:3500])}",
        parse_mode="HTML",
    )


@dp.message(Command("ai_stats"))
async def cmd_ai_stats(message: types.Message):
    """Показывает статистику AI: запросы, cache hit-rate, провайдеры, оценка токенов."""
    if not _only_owner(message.from_user.id):
        return
    import ai_cache
    st = ai_cache.get_stats()
    requests = st.get("requests", 0)
    hits = st.get("cache_hits", 0)
    miss = st.get("cache_miss", 0)
    fallbacks = st.get("fallbacks", 0)
    errors = st.get("errors", 0)
    hit_rate = (hits / requests * 100.0) if requests else 0.0
    by_prov = st.get("by_provider") or {}
    by_task = st.get("by_task") or {}
    in_tokens = st.get("est_input_tokens", 0)
    out_tokens = st.get("est_output_tokens", 0)
    cache_size = st.get("cache_size", 0)
    s = load_settings()
    primary = (s.get("ai_provider") or "off").lower()
    primary_model = (s.get(f"{primary}_model") if primary in ("groq", "gemini") else "") or "—"
    fast_model = (s.get("ai_fast_model") or "—")
    fb_provider = (s.get("ai_fallback_provider") or "off").lower()
    fb_model = (s.get("ai_fallback_model") or "—") if fb_provider != "off" else "—"
    ttl = int(s.get("ai_cache_ttl_sec") or 0)

    by_prov_lines = ", ".join(f"{k}={v}" for k, v in by_prov.items()) or "—"
    by_task_lines = ", ".join(f"{k}={v}" for k, v in by_task.items()) or "—"

    text = (
        "🤖 <b>AI Stats</b>\n\n"
        f"<b>Конфиг:</b>\n"
        f"  • основная: <code>{primary}</code> / <code>{primary_model}</code>\n"
        f"  • быстрая (auto-verdict): <code>{fast_model}</code>\n"
        f"  • fallback: <code>{fb_provider}</code> / <code>{fb_model}</code>\n"
        f"  • cache TTL: <code>{ttl}s</code>\n\n"
        f"<b>Запросы:</b> {requests}\n"
        f"  • из кэша: {hits} ({hit_rate:.0f}%)\n"
        f"  • в LLM: {miss}\n"
        f"  • fallback использован: {fallbacks}\n"
        f"  • ошибок (пустой ответ от обоих): {errors}\n\n"
        f"<b>По провайдерам:</b> {by_prov_lines}\n"
        f"<b>По задачам:</b> {by_task_lines}\n\n"
        f"<b>Оценка токенов:</b> in≈{in_tokens}, out≈{out_tokens}\n"
        f"<b>Размер кэша:</b> {cache_size} записей"
    )
    await message.answer(text, parse_mode="HTML")


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

@dp.callback_query(F.data.startswith("ai|"))
async def cb_ai_ask(callback: CallbackQuery):
    """On-demand AI-вердикт по клику в алерте."""
    if not _only_owner(callback.from_user.id):
        await callback.answer("Доступ только владельцу.", show_alert=False)
        return
    cache_key = (callback.data or "")[3:]
    cached = _alert_cache.get(cache_key)
    if not cached:
        await callback.answer(
            "Лот устарел (бот перезапускался). Откройте новый алерт.",
            show_alert=True,
        )
        return
    s = load_settings()
    from ai_advisor import (
        get_active_provider,
        get_fallback_provider,
        analyze_gift,
    )
    provider = get_active_provider(s)
    fallback = get_fallback_provider(s)
    if provider is None:
        await callback.answer(
            "AI не настроен. Откройте Mini App → AI-помощник.",
            show_alert=True,
        )
        return
    await callback.answer("🤖 Спрашиваю AI…")
    # task=on_demand → cache-TTL короче (≤60с): пользователь хочет «свежее» мнение.
    text = await analyze_gift(
        provider, cached["gift"], cached["market"],
        settings=s, task="on_demand", fallback=fallback,
    )
    if not text:
        await callback.message.answer(
            "❌ AI не ответил. Возможно лимит токенов или сетевая ошибка.",
            reply_to_message_id=callback.message.message_id,
        )
        return
    provider_emoji = {"groq": "⚡", "gemini": "✨"}.get(
        (s.get("ai_provider") or "").lower(), "🤖"
    )
    await callback.message.answer(
        f"{provider_emoji} <i>AI: {_esc(text[:1000])}</i>",
        parse_mode="HTML",
        reply_to_message_id=callback.message.message_id,
    )


@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ <b>Настройки мониторинга:</b>", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu_price")
async def menu_price(callback: CallbackQuery):
    await callback.message.edit_text(
        "💎 <b>Цена и Floor</b>\n\n"
        "Здесь задаются ценовые границы и поведение Floor-фильтра.\n"
        "<i>Floor — авторитетная минимальная цена коллекции с маркета.</i>",
        reply_markup=price_menu_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_filters")
async def menu_filters(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎯 <b>Фильтры подарков</b>\n\n"
        "Что должно «попасть» в карточку выгодного лота помимо цены.",
        reply_markup=filters_menu_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_numbers")
async def menu_numbers(callback: CallbackQuery):
    await callback.message.edit_text(
        "#️⃣ <b>Фильтр по номеру подарка</b>\n\n"
        "Категории применяются как «или» — лот пройдёт, если номер совпадает "
        "хотя бы с одной выбранной категорией. Если ничего не выбрано — фильтр выключен.",
        reply_markup=numbers_kb()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("num_"))
async def toggle_number(callback: CallbackQuery):
    cat = callback.data.removeprefix("num_")
    s = load_settings()
    nums = list(s.get("number_filters", []))
    if cat in nums:
        nums.remove(cat)
    else:
        if cat in all_number_filter_categories():
            nums.append(cat)
    s["number_filters"] = nums
    save_settings(s)
    await callback.message.edit_reply_markup(reply_markup=numbers_kb())
    await callback.answer()


@dp.callback_query(F.data == "toggle_monochrome")
async def toggle_monochrome(callback: CallbackQuery):
    s = load_settings()
    s["monochrome_only"] = not bool(s.get("monochrome_only", False))
    save_settings(s)
    state = "включён ✅" if s["monochrome_only"] else "выключен ⚪"
    await callback.answer(f"Монохром-фильтр {state}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=filters_menu_kb())


@dp.callback_query(F.data == "menu_collections")
async def menu_collections(callback: CallbackQuery):
    from floor_cache import _mrkt
    if not _mrkt._data:
        text = (
            "📚 <b>Фильтр по коллекциям</b>\n\n"
            "Список коллекций ещё не загружен из MRKT.\n"
            "Подожди 1-2 минуты после старта бота — кэш floor'ов "
            "обновляется в фоне."
        )
    else:
        text = (
            "📚 <b>Фильтр по коллекциям</b>\n\n"
            f"Загружено: <b>{len(_mrkt._data)}</b> коллекций.\n"
            "Если ни одна не выбрана — алертим обо всех."
        )
    await callback.message.edit_text(text, reply_markup=collections_kb(page=0))
    await callback.answer()


@dp.callback_query(F.data.startswith("colpage_"))
async def col_page(callback: CallbackQuery):
    raw = callback.data.removeprefix("colpage_")
    if raw == "noop":
        await callback.answer()
        return
    try:
        page = int(raw)
    except ValueError:
        await callback.answer()
        return
    await callback.message.edit_reply_markup(reply_markup=collections_kb(page=page))
    await callback.answer()


@dp.callback_query(F.data == "col_reset")
async def col_reset(callback: CallbackQuery):
    s = load_settings()
    s["filter_collections"] = []
    save_settings(s)
    await callback.answer("Коллекции сброшены", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=collections_kb(page=0))


@dp.callback_query(F.data.startswith("col_"))
async def toggle_collection(callback: CallbackQuery):
    name = callback.data.removeprefix("col_")
    if not name or name == "reset":
        await callback.answer()
        return
    s = load_settings()
    cols = list(s.get("filter_collections", []))
    if name in cols:
        cols.remove(name)
    else:
        cols.append(name)
    s["filter_collections"] = cols
    save_settings(s)
    await callback.message.edit_reply_markup(reply_markup=collections_kb(page=0))
    await callback.answer()


@dp.callback_query(F.data == "set_max_rarity_pm")
async def set_max_rarity_pm_prompt(callback: CallbackQuery):
    s = load_settings()
    _pending_input[callback.from_user.id] = "rarity_pm"
    cur = float(s.get("max_rarity_pm", 0) or 0)
    await callback.message.edit_text(
        "💠 <b>Максимальный rarity (per-mille)</b>\n\n"
        "Лот пройдёт, если ХОТЯ БЫ ОДИН его атрибут (model/backdrop/symbol) "
        "имеет rarity_per_mille ≤ заданного значения.\n\n"
        "<b>Шкала:</b>\n"
        "  &lt; 1   — Legendary\n"
        "  &lt; 5   — Epic\n"
        "  &lt; 30  — Rare\n"
        "  &lt; 100 — Uncommon\n"
        "  ≥ 100   — Common\n\n"
        f"Текущее: <b>{cur:g}</b> (0 = без фильтра).\n"
        "Отправь число от 0 до 1000:",
        reply_markup=back_kb("menu_filters")
    )
    await callback.answer()


@dp.callback_query(F.data == "set_min_ton")
async def set_min_ton_prompt(callback: CallbackQuery):
    s = load_settings()
    _pending_input[callback.from_user.id] = "min_ton"
    cur = float(s.get("min_price_ton", 0) or 0)
    await callback.message.edit_text(
        "💵 <b>Минимальная цена (TON)</b>\n\n"
        "Не алертить лоты дешевле этого значения. Полезно отсечь "
        "копеечные лоты, которые часто оказываются скамом или мусором.\n\n"
        f"Текущее: <b>{cur} TON</b>\n"
        "Отправь число (0 = без ограничения):",
        reply_markup=back_kb("menu_price")
    )
    await callback.answer()


@dp.callback_query(F.data == "show_settings")
async def show_settings(callback: CallbackQuery):
    s = load_settings()
    from rate_provider import rate_provider
    await rate_provider.ensure_fresh()

    rarity_text  = ", ".join(s["filter_rarity"]) if s["filter_rarity"] else "все"
    market_map   = {"mrkt": "MRKT", "fragment": "Fragment", "portals": "Portals"}
    markets_text = ", ".join(market_map.get(m, m) for m in s.get("filter_markets", [])) or "все"
    notif_text   = "Включены ✅" if s.get("notifications_on", True) else "Выключены 🔕"
    discount_text = f"{s['min_discount_pct']}%" if s.get("min_discount_pct", 0) > 0 else "без фильтра"

    floor_tol = float(s.get("floor_tolerance_pct", 0.0))
    floor_tol_text = f"+{floor_tol:g}%" if floor_tol > 0 else "только пол (0%)"
    require_floor = bool(s.get("require_floor", True))
    rf_text = "да ✅" if require_floor else "нет (риск ложных алертов) ⚠️"

    stars_equiv = _fmt_int(rate_provider.ton_to_stars(s["max_price_ton"]))

    cols = s.get("filter_collections", [])
    cols_text = (", ".join(cols[:5]) + (f" +{len(cols)-5}" if len(cols) > 5 else "")) if cols else "все"

    nums = s.get("number_filters", [])
    nums_text = ", ".join(number_filter_label(n).split(" ", 1)[-1] for n in nums) if nums else "без фильтра"

    mono_text = "да 🟢" if s.get("monochrome_only", False) else "нет ⚪"

    rar_pm = float(s.get("max_rarity_pm", 0) or 0)
    rar_pm_text = f"≤ {rar_pm:g} pm" if rar_pm > 0 else "без фильтра"

    min_ton = float(s.get("min_price_ton", 0) or 0)
    min_ton_text = f"{min_ton} TON" if min_ton > 0 else "без ограничения"

    text = (
        f"📊 <b>Текущие настройки</b>\n\n"
        f"<b>💎 Цена и Floor</b>\n"
        f"  • Макс. цена: <b>{s['max_price_ton']} TON</b> "
        f"<i>(≈ {stars_equiv} ⭐)</i>\n"
        f"  • Мин. цена: <b>{min_ton_text}</b>\n"
        f"  • Допуск над Floor: <b>{floor_tol_text}</b>\n"
        f"  • Мин. скидка от Floor: <b>{discount_text}</b>\n"
        f"  • Требовать known Floor: <b>{rf_text}</b>\n\n"
        f"<b>🎯 Фильтры подарков</b>\n"
        f"  • Монохром: <b>{mono_text}</b>\n"
        f"  • Номера: <b>{nums_text}</b>\n"
        f"  • Редкости: <b>{rarity_text}</b>\n"
        f"  • Макс. rarity (per-mille): <b>{rar_pm_text}</b>\n"
        f"  • Коллекции: <b>{cols_text}</b>\n\n"
        f"<b>🏪 Маркеты:</b> {markets_text}\n"
        f"<b>🔔 Уведомления:</b> {notif_text}"
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
    s["notifications_on"] = not s.get("notifications_on", True)
    save_settings(s)
    status = "включены ✅" if s["notifications_on"] else "выключены 🔕"
    await callback.answer(f"Уведомления {status}")
    await callback.message.edit_reply_markup(reply_markup=notifs_menu_kb())


@dp.callback_query(F.data == "menu_notifs")
async def menu_notifs(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔔 <b>Уведомления и парсинг</b>\n\n"
        "  • <b>Per-market</b>: можно отключить алерты от любого маркета.\n"
        "  • <b>Тихие часы</b>: окно UTC, в которое алерты не отправляются.\n"
        "  • <b>Лимит</b>: максимум алертов за один цикл опроса (per market).\n"
        "  • <b>Редкие свежие</b>: дополнительный режим — алертит даже если "
        "цена выше floor, если у лота есть редкий атрибут (≤ порог ‰).",
        reply_markup=notifs_menu_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "toggle_mrkt_alerts")
async def toggle_mrkt_alerts(callback: CallbackQuery):
    s = load_settings()
    s["mrkt_alerts_on"] = not bool(s.get("mrkt_alerts_on", True))
    save_settings(s)
    await callback.answer("MRKT: " + ("вкл" if s["mrkt_alerts_on"] else "выкл"))
    await callback.message.edit_reply_markup(reply_markup=notifs_menu_kb())


@dp.callback_query(F.data == "toggle_fragment_alerts")
async def toggle_fragment_alerts(callback: CallbackQuery):
    s = load_settings()
    s["fragment_alerts_on"] = not bool(s.get("fragment_alerts_on", True))
    save_settings(s)
    await callback.answer("Fragment: " + ("вкл" if s["fragment_alerts_on"] else "выкл"))
    await callback.message.edit_reply_markup(reply_markup=notifs_menu_kb())


@dp.callback_query(F.data == "toggle_portals_alerts")
async def toggle_portals_alerts(callback: CallbackQuery):
    s = load_settings()
    s["portals_alerts_on"] = not bool(s.get("portals_alerts_on", True))
    save_settings(s)
    await callback.answer("Portals: " + ("вкл" if s["portals_alerts_on"] else "выкл"))
    await callback.message.edit_reply_markup(reply_markup=notifs_menu_kb())


@dp.callback_query(F.data == "toggle_recent_rare")
async def toggle_recent_rare(callback: CallbackQuery):
    s = load_settings()
    s["recent_rare_mode"] = not bool(s.get("recent_rare_mode", False))
    save_settings(s)
    await callback.answer("Режим: " + ("вкл 🟢" if s["recent_rare_mode"] else "выкл ⚪"), show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=notifs_menu_kb())


@dp.callback_query(F.data == "set_recent_rare_pm")
async def set_recent_rare_pm_prompt(callback: CallbackQuery):
    _pending_input[callback.from_user.id] = "recent_rare_pm"
    s = load_settings()
    cur = float(s.get("recent_rare_pm", 5.0) or 5.0)
    await callback.message.edit_text(
        "💠 <b>Порог редкости (per-mille) для режима «Редкие свежие»</b>\n\n"
        "Лот считается достаточно редким, если хотя бы один из его атрибутов "
        "(model/backdrop/symbol) имеет rarity_per_mille ≤ заданного значения.\n\n"
        f"Текущее: <b>{cur:g}‰</b>\n"
        "Введи число от 0 до 1000 (рекомендую 1–5):",
        reply_markup=back_kb("menu_notifs")
    )
    await callback.answer()


@dp.callback_query(F.data == "set_max_per_cycle")
async def set_max_per_cycle_prompt(callback: CallbackQuery):
    _pending_input[callback.from_user.id] = "max_per_cycle"
    s = load_settings()
    cur = int(s.get("max_alerts_per_cycle", 0) or 0)
    await callback.message.edit_text(
        "📊 <b>Лимит алертов на цикл (per market)</b>\n\n"
        "Максимум сообщений за один цикл опроса каждого маркета.\n"
        "Помогает не получить 50 сообщений после простоя.\n\n"
        f"Текущее: <b>{cur if cur > 0 else 'без лимита'}</b>\n"
        "Введи число (0 = без лимита, 5 = умеренно):",
        reply_markup=back_kb("menu_notifs")
    )
    await callback.answer()


@dp.callback_query(F.data == "set_quiet_hours")
async def set_quiet_hours_prompt(callback: CallbackQuery):
    _pending_input[callback.from_user.id] = "quiet_hours"
    s = load_settings()
    qs = int(s.get("quiet_hours_start", 0) or 0)
    qe = int(s.get("quiet_hours_end", 0) or 0)
    cur = "выкл." if qs == qe else f"{qs:02d}:00–{qe:02d}:00 UTC"
    await callback.message.edit_text(
        "🌙 <b>Тихие часы (UTC)</b>\n\n"
        "В это окно алерты не отправляются. Часовой пояс — UTC "
        "(можно учитывать сдвиг от твоего локального).\n\n"
        f"Текущее: <b>{cur}</b>\n"
        "Введи две цифры через дефис: <code>22-7</code> "
        "(значит тихо с 22:00 до 07:00 UTC), <code>0-0</code> = выключить.",
        reply_markup=back_kb("menu_notifs")
    )
    await callback.answer()


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
        reply_markup=back_kb("menu_price")
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
        reply_markup=back_kb("menu_price")
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
        reply_markup=back_kb("menu_price")
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
    await callback.message.edit_reply_markup(reply_markup=price_menu_kb())


@dp.message(F.text.regexp(r"^\d{1,2}\s*-\s*\d{1,2}$"))
async def handle_range_input(message: types.Message):
    """Обрабатывает ввод в формате `H1-H2` для тихих часов."""
    if not _only_owner(message.from_user.id):
        return
    pending = _pending_input.get(message.from_user.id)
    if pending != "quiet_hours":
        return
    _pending_input.pop(message.from_user.id, None)
    try:
        a, b = message.text.split("-")
        h1, h2 = int(a.strip()), int(b.strip())
    except ValueError:
        await message.answer("⚠️ Неверный формат. Пример: <code>22-7</code> или <code>0-0</code>.",
                             reply_markup=notifs_menu_kb())
        return
    if not (0 <= h1 <= 23) or not (0 <= h2 <= 23):
        await message.answer("⚠️ Часы должны быть в диапазоне 0–23.",
                             reply_markup=notifs_menu_kb())
        return
    s = load_settings()
    s["quiet_hours_start"] = h1
    s["quiet_hours_end"] = h2
    save_settings(s)
    if h1 == h2:
        await message.answer("✅ Тихие часы выключены.", reply_markup=notifs_menu_kb())
    else:
        await message.answer(
            f"✅ Тихие часы: <b>{h1:02d}:00–{h2:02d}:00 UTC</b>",
            reply_markup=notifs_menu_kb()
        )


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
            await message.answer("⚠️ Введи значение от 0 до 50.", reply_markup=price_menu_kb())
            return
        s["floor_tolerance_pct"] = round(value_f, 2)
        save_settings(s)
        label = f"+{s['floor_tolerance_pct']:g}%" if s["floor_tolerance_pct"] > 0 else "только пол (0%)"
        await message.answer(
            f"✅ Допуск над Floor: <b>{label}</b>",
            reply_markup=price_menu_kb()
        )

    elif pending == "min_ton":
        if value_f < 0 or value_f > 100_000:
            await message.answer("⚠️ Введи значение от 0 до 100 000 TON.", reply_markup=price_menu_kb())
            return
        s["min_price_ton"] = round(value_f, 4)
        save_settings(s)
        label = f"{s['min_price_ton']} TON" if s["min_price_ton"] > 0 else "без ограничения"
        await message.answer(
            f"✅ Мин. цена: <b>{label}</b>",
            reply_markup=price_menu_kb()
        )

    elif pending == "rarity_pm":
        if value_f < 0 or value_f > 1000:
            await message.answer("⚠️ Введи значение от 0 до 1000.", reply_markup=filters_menu_kb())
            return
        s["max_rarity_pm"] = round(value_f, 3)
        save_settings(s)
        label = f"≤ {s['max_rarity_pm']:g} pm" if s["max_rarity_pm"] > 0 else "без фильтра"
        await message.answer(
            f"✅ Макс. rarity: <b>{label}</b>",
            reply_markup=filters_menu_kb()
        )

    elif pending == "max_per_cycle":
        if value_f < 0 or value_f > 1000:
            await message.answer("⚠️ Введи значение от 0 до 1000.", reply_markup=notifs_menu_kb())
            return
        s["max_alerts_per_cycle"] = int(value_f)
        save_settings(s)
        label = f"{s['max_alerts_per_cycle']}/цикл" if s["max_alerts_per_cycle"] > 0 else "без лимита"
        await message.answer(
            f"✅ Лимит алертов: <b>{label}</b>",
            reply_markup=notifs_menu_kb()
        )

    elif pending == "recent_rare_pm":
        if value_f < 0 or value_f > 1000:
            await message.answer("⚠️ Введи значение от 0 до 1000.", reply_markup=notifs_menu_kb())
            return
        s["recent_rare_pm"] = round(value_f, 3)
        save_settings(s)
        await message.answer(
            f"✅ Порог редкости: <b>≤ {s['recent_rare_pm']:g}‰</b>",
            reply_markup=notifs_menu_kb()
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
    "Vice Cream", "Chill Flame", "Snake Box",
]
RANDOM_MARKETS  = ["mrkt", "fragment", "portals"]
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

    # Случайная палитра — иногда монохромная (одного hue)
    if random.random() < 0.5:
        # монохром: вариации одного hue (синий)
        colors = [0x336699, 0x4477AA, 0x224488, 0x55AABB]
    else:
        # разные цвета
        colors = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00]

    gift: dict = {
        "id": fake_id,
        "name": name,
        "model_name": "Test Model",
        "backdrop_name": "Test Backdrop",
        "symbol_name": "Test Symbol",
        "colors": colors,
        "rarities_pm": {"model": 5.0, "backdrop": 12.0, "symbol": 0.8},
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

    await callback.answer("Тест отправлен")
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
    name        = gift.get("name", "Unknown")
    number      = gift.get("number", "")
    slug        = gift.get("slug", "")
    raw_id      = gift.get("id", "")
    price       = gift.get("price", 0)        # TON
    floor       = gift.get("floor_price")     # TON
    rarity      = gift.get("rarity", "")
    image_url   = gift.get("image_url", "")
    stars_price = gift.get("stars_price")     # Stars (оригинал для Fragment)
    floor_stars = gift.get("floor_stars")     # Stars floor (для Fragment)
    model       = gift.get("model_name", "")
    backdrop    = gift.get("backdrop_name", "")
    symbol      = gift.get("symbol_name", "")
    colors      = gift.get("colors", [])
    rar_pm      = gift.get("rarities_pm", {}) or {}

    # ── Кнопки ───────────────────────────────────────────────────────────────
    btn_specs = build_market_buttons(market, raw_id, slug=slug, name=name, number=number)
    buttons = [
        [InlineKeyboardButton(text=b["text"], url=b["url"])] for b in btn_specs
    ] if btn_specs else []

    market_icons = {
        "mrkt": "🟣 MRKT", "main_mrkt_bot": "🟣 MRKT",
        "fragment": "🔵 Fragment",
        "portals": "🟢 Portals", "getgems": "🟢 Portals",
    }
    market_icon = market_icons.get(market, market)

    # Доп. кнопка веб-MRKT (на случай, если Mini App недоступна)
    if market in ("mrkt", "main_mrkt_bot"):
        web_link = build_mrkt_web_link(slug, raw_id)
        if web_link and web_link != "https://mrkt.fun":
            buttons.append([InlineKeyboardButton(text="🌐 Веб (mrkt.fun)", url=web_link)])

    if not buttons:
        gift_url = f"https://t.me/{market}"
        buttons = [[InlineKeyboardButton(text="🔗 Открыть", url=gift_url)]]

    # Кнопка «🤖 Спросить AI» — если AI настроен (пусть и без auto-комментариев),
    # юзер может тапнуть для on-demand вердикта. callback_data ограничено 64 байта,
    # поэтому шлём только market+id (id обычно short uuid). Сам gift кэшируется
    # in-memory в _alert_cache (LRU 200 шт.).
    s_ai_kb = load_settings()
    if (s_ai_kb.get("ai_provider") or "off").lower() != "off":
        cache_key = _cache_alert(gift, market)
        if cache_key:
            buttons.append([InlineKeyboardButton(
                text="🤖 Спросить AI",
                callback_data=f"ai|{cache_key}",
            )])

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
    floor_valid   = isinstance(floor, (int, float)) and floor > 0
    if floor_valid:
        floor_str = format_price(floor)
        floor_stars_hint = ""
        if floor_stars and market == "fragment":
            floor_stars_hint = f" <i>({format_stars(floor_stars)})</i>"
        floor_line = f"\n📊 Floor: <b>{floor_str}</b>{floor_stars_hint}"
        # Скидка/наценка относительно floor
        if price > 0 and floor > price + 1e-9:
            pct = round((floor - price) / floor * 100, 1)
            discount_line = f"\n📉 Скидка: <b>−{pct}%</b> от Floor"
        elif price > 0 and abs(floor - price) <= 1e-6:
            discount_line = "\n🎯 <b>Ровно по Floor</b>"
        elif price > 0 and price > floor:
            pct = round((price - floor) / floor * 100, 1)
            discount_line = f"\n⬆️ <b>+{pct}%</b> над Floor"

    rarity_emoji_map = {
        "Legendary": "🟡", "Epic": "🟣", "Rare": "🔵",
        "Uncommon": "🟢", "Common": "⚪",
    }
    rarity_icon = rarity_emoji_map.get(rarity, "✨")
    rarity_line = f"\n{rarity_icon} Редкость: <b>{rarity}</b>" if rarity else ""

    # Атрибуты model/backdrop/symbol с per-mille (если есть)
    attr_lines = []
    for label, val, key in (
        ("Модель",   model,    "model"),
        ("Фон",      backdrop, "backdrop"),
        ("Символ",   symbol,   "symbol"),
    ):
        if not val:
            continue
        pm = rar_pm.get(key)
        pm_str = f" <i>({pm:g}‰)</i>" if isinstance(pm, (int, float)) and pm > 0 else ""
        attr_lines.append(f"  • {label}: <b>{val}</b>{pm_str}")
    attrs_block = ("\n" + "\n".join(attr_lines)) if attr_lines else ""

    badges = []
    cats = number_categories(number)
    cat_emoji = {
        "lucky": "🍀", "repeat": "🔁", "round": "⭕",
        "sub100": "🥇", "low": "💯", "pretty100": "🔢",
        "sequential": "↗️", "palindrome": "🔄",
    }
    for c in sorted(cats):
        if c in cat_emoji:
            badges.append(cat_emoji[c])
    if colors and is_monochrome(colors):
        badges.append("🎨")
    badges_line = ("\n" + " ".join(badges)) if badges else ""

    caption = (
        f"🚀 <b>Выгодный лот!</b>\n\n"
        f"🎁 <b>{name}{num_text}</b>"
        f"{rarity_line}\n"
        f"💰 Цена: <b>{price_str}</b>{stars_hint}"
        f"{floor_line}{discount_line}"
        f"{attrs_block}{badges_line}\n"
        f"🏪 {market_icon}"
    )

    sent_ok = False
    try:
        if image_url:
            try:
                await bot_instance.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=caption,
                    reply_markup=keyboard,
                )
                sent_ok = True
            except Exception:
                pass  # Fallback на текст

        if not sent_ok:
            await bot_instance.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            sent_ok = True
    except Exception as e:
        logger.error(f"send_gift_alert ошибка: {e}")

    # Логируем алерт для daily digest. Никогда не блокирует основной поток.
    if sent_ok:
        try:
            from database import log_alert
            log_alert(market, gift)
        except Exception:
            logger.exception("send_gift_alert: log_alert провалился")

        # Если включен AI-вердикт для алертов И дисконт достаточный — спросим LLM
        # асинхронно и пришлём отдельным сообщением (не редактируем оригинальный
        # алерт чтобы avoid issues с photo-captions). 200-300мс не считается "ждать первым"
        # потому что запускаем в fire-and-forget после успешной отправки.
        try:
            s_ai = load_settings()
            if s_ai.get("ai_for_alerts"):
                min_disc = float(s_ai.get("ai_alerts_min_discount_pct", 10.0))
                if floor_valid:
                    p = float(gift.get("price") or 0)
                    f = float(floor)
                    real_disc = (f - p) / f * 100 if f > 0 and p > 0 else 0
                else:
                    real_disc = 0
                if real_disc >= min_disc or not floor_valid:
                    asyncio.create_task(_send_ai_verdict(bot_instance, chat_id, gift, market))
        except Exception:
            logger.exception("send_gift_alert: AI-вердикт не запустился")


async def _send_ai_verdict(bot_instance, chat_id: int, gift: dict, market: str) -> None:
    """Запрашивает у активного AI-провайдера короткий вердикт и шлёт его как
    follow-up сообщение под только что отправленным алертом.

    Использует **быструю** модель (`get_fast_provider`) — авто-вердикт должен
    приходить ≈ за 100-300 мс, чтобы не отставать от user perception. Если
    fast-провайдер не настроен (нет ключа) — fallback на основной.
    Если основной возвращает пусто — fallback на резервного провайдера.
    """
    try:
        from ai_advisor import (
            get_active_provider,
            get_fast_provider,
            get_fallback_provider,
            analyze_gift,
        )
        s = load_settings()
        # Под алертом — быстрая модель (auto task), кэш по сигнатуре лота
        provider = get_fast_provider(s) or get_active_provider(s)
        if provider is None:
            return
        fallback = get_fallback_provider(s)
        text = await analyze_gift(
            provider, gift, market,
            settings=s, task="auto", fallback=fallback,
        )
        if not text:
            return
        # Префикс эмодзи зависит от провайдера для прозрачности
        provider_emoji = {"groq": "⚡", "gemini": "✨"}.get(
            (load_settings().get("ai_provider") or "").lower(), "🤖"
        )
        await bot_instance.send_message(
            chat_id=chat_id,
            text=f"{provider_emoji} <i>AI: {_esc(text)}</i>",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        logger.exception("_send_ai_verdict: ошибка")


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
