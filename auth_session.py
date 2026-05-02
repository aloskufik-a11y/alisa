"""
auth_session.py — Скрипт ОДНОРАЗОВОЙ авторизации Telethon.

Запусти ОДИН РАЗ:
    python auth_session.py

После успешной авторизации файл userbot_session.session сохранён.
Больше вводить номер и код НЕ НУЖНО.
"""

import asyncio
import os
import sys

# ─── ФИКС для Windows + Python 3.12 (CancelledError при connect) ──────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# ──────────────────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

load_dotenv()

API_ID   = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userbot_session")

PLACEHOLDER = "+7XXXXXXXXXX"


def get_phone() -> str:
    """Берёт номер из .env, если там не заглушка — иначе просит ввести."""
    env_phone = os.getenv("PHONE", "").strip()
    if env_phone and env_phone != PLACEHOLDER and env_phone.startswith("+"):
        print(f"📱 Используем номер из .env: {env_phone}")
        return env_phone
    # Просим ввести вручную
    while True:
        phone = input("\n📱 Введи номер телефона (формат +79001234567): ").strip()
        if phone.startswith("+") and phone[1:].isdigit() and len(phone) >= 8:
            return phone
        print("   ❌ Неверный формат. Попробуй ещё раз.")


async def authorize():
    if not API_ID or not API_HASH:
        print("❌ API_ID и API_HASH не заданы в .env")
        sys.exit(1)

    print("=" * 55)
    print("  TG Gift Monitor — Авторизация Telegram аккаунта")
    print("=" * 55)

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    # Пробуем подключиться с retry
    for attempt in range(1, 4):
        try:
            await client.connect()
            break
        except Exception as e:
            print(f"   ⚠️ Попытка {attempt}/3 подключения: {e}")
            if attempt == 3:
                print("❌ Не удалось подключиться к Telegram. Проверь интернет.")
                sys.exit(1)
            await asyncio.sleep(2)

    # Уже авторизованы?
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ Уже авторизованы как: {me.first_name} (@{me.username})")
        print(f"📁 Файл сессии: {SESSION_PATH}.session")
        print("\n▶️  Запускай бота: python main.py")
        await client.disconnect()
        return

    # Получаем номер
    phone = get_phone()

    # Отправляем код
    print(f"\n⏳ Отправляем код на {phone}...")
    try:
        await client.send_code_request(phone)
    except Exception as e:
        print(f"❌ Ошибка отправки кода: {e}")
        await client.disconnect()
        sys.exit(1)

    # Вводим код
    for attempt in range(1, 4):
        code = input("📬 Введи код из Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
            break
        except PhoneCodeInvalidError:
            print(f"   ❌ Неверный код (попытка {attempt}/3)")
            if attempt == 3:
                print("❌ Слишком много неверных кодов. Выход.")
                await client.disconnect()
                sys.exit(1)
        except SessionPasswordNeededError:
            # Двухфакторная аутентификация
            password = input("🔐 Введи пароль 2FA: ").strip()
            await client.sign_in(password=password)
            break
        except Exception as e:
            print(f"❌ Ошибка входа: {e}")
            await client.disconnect()
            sys.exit(1)

    me = await client.get_me()
    print(f"\n✅ Успешно авторизован: {me.first_name} (@{me.username})")
    print(f"📁 Сессия сохранена: {SESSION_PATH}.session")
    print("\n🚀 Теперь запускай: python main.py")
    print("   (Повторный ввод кода больше не нужен)\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(authorize())
