"""
Telegram Userbot — слушает сообщения из каналов/чатов.
Использует общий Telethon client из main.py.
"""
import logging

from telethon import events

from config import CHANNELS_TO_MONITOR
from database import is_gift_seen, add_gift
from logic import is_profitable
from notifier import bot
from tg_message_parser import parse_telegram_message

logger = logging.getLogger(__name__)


def register_userbot_handlers(client):
    """
    Регистрирует обработчики событий на уже созданном клиенте.
    Вызывается после авторизации в main.py.
    """

    @client.on(events.NewMessage(chats=CHANNELS_TO_MONITOR))
    async def handle_new_message(event):
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from settings_store import load_settings
        from config import USER_ID

        s = load_settings()
        if not s.get("notifications_on", True):
            return

        message_text = event.message.text or ""
        if not message_text.strip():
            return  # Игнорируем пустые/медиа сообщения без текста

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", None) or getattr(chat, "username", "Unknown")
        chat_username = getattr(chat, "username", None)

        gift_data = parse_telegram_message(message_text)
        if not gift_data:
            return

        gift_id = f"tg_{chat.id}_{event.message.id}"
        if is_gift_seen(gift_id):
            return

        is_new = add_gift(gift_id, gift_data["name"], gift_data["price"], f"tg:{chat_title}")
        if not is_new:
            return

        if not is_profitable(gift_data, market="tg"):
            return

        # Ссылка на сообщение
        if chat_username:
            link = f"https://t.me/{chat_username}/{event.message.id}"
        else:
            clean_id = str(chat.id).lstrip("-").removeprefix("100")
            link = f"https://t.me/c/{clean_id}/{event.message.id}"

        currency = gift_data.get("currency", "Stars")
        price = gift_data["price"]
        price_str = f"{price} TON" if currency == "TON" else f"{int(price) if price == int(price) else price} ⭐"

        alert_text = (
            f"📢 <b>Новый лот в Telegram!</b>\n\n"
            f"🎁 <b>Название:</b> {gift_data['name']}\n"
            f"💰 <b>Цена:</b> {price_str}\n"
            f"📣 <b>Источник:</b> {chat_title}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Перейти к сообщению", url=link)]
        ])
        try:
            await bot.send_message(chat_id=USER_ID, text=alert_text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка отправки TG алерта: {e}")

    logger.info(f"Userbot: слушаем каналы: {CHANNELS_TO_MONITOR}")
