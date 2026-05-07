"""
AI advisor — обёртка над LLM-провайдерами Groq и Google Gemini.
Используется для:
1. Кратких комментариев под каждым алертом ("стоит ли брать") — async, не блокирует
2. Аналитики дня в начале daily digest

Архитектура:
- BaseProvider — protocol с одним методом async chat(messages, **kw) -> str
- GroqProvider, GeminiProvider — реализации
- get_active_provider(settings) -> BaseProvider | None
- analyze_gift(provider, gift, context) -> str
- analyze_daily(provider, digest_stats) -> str

Все ошибки сетевые/auth/quota не ронят бот — функции возвращают пустую строку
и логируют warning.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Каталог моделей — пользователь выбирает в Mini App из этих списков.
# ──────────────────────────────────────────────────────────────────────────────
GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # лучший баланс (default)
    "llama-3.1-8b-instant",      # самый быстрый
    "mixtral-8x7b-32768",        # 32k контекст
    "gemma2-9b-it",              # лёгкая Google-модель
]

GEMINI_MODELS = [
    "gemini-2.0-flash",          # default — быстрая, бесплатная
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",            # лучшая, но платная
]


# ──────────────────────────────────────────────────────────────────────────────
# Provider implementations
# ──────────────────────────────────────────────────────────────────────────────

class GroqProvider:
    """Groq — OpenAI-compatible API. Документация: https://console.groq.com/docs"""

    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key.strip()
        self.model = model

    async def chat(self, system: str, user: str, max_tokens: int = 200,
                   temperature: float = 0.7) -> str:
        if not self.api_key:
            return ""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.BASE_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        logger.warning(f"Groq API {resp.status}: {body}")
                        return ""
                    data = await resp.json()
                    return (data["choices"][0]["message"]["content"] or "").strip()
        except asyncio.TimeoutError:
            logger.warning("Groq API: timeout")
            return ""
        except Exception:
            logger.exception("Groq API: unexpected error")
            return ""


class GeminiProvider:
    """Google Gemini — REST API. Документация: https://ai.google.dev/api"""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key = api_key.strip()
        self.model = model

    async def chat(self, system: str, user: str, max_tokens: int = 200,
                   temperature: float = 0.7) -> str:
        if not self.api_key:
            return ""
        # Gemini принимает один поток сообщений с system instruction отдельно
        url = (
            f"{self.BASE_URL}/{self.model}:generateContent?key={self.api_key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "user", "parts": [{"text": user}]}
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        logger.warning(f"Gemini API {resp.status}: {body}")
                        return ""
                    data = await resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        return ""
                    parts = candidates[0].get("content", {}).get("parts") or []
                    text = "".join(p.get("text", "") for p in parts).strip()
                    return text
        except asyncio.TimeoutError:
            logger.warning("Gemini API: timeout")
            return ""
        except Exception:
            logger.exception("Gemini API: unexpected error")
            return ""


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

def get_active_provider(settings: dict) -> Any | None:
    """Создаёт провайдер по выбранному в settings, или None если AI выключен."""
    provider_name = (settings.get("ai_provider") or "off").lower().strip()
    if provider_name == "groq":
        key = (settings.get("groq_api_key") or "").strip()
        model = (settings.get("groq_model") or GROQ_MODELS[0]).strip()
        if not key:
            return None
        if model not in GROQ_MODELS:
            model = GROQ_MODELS[0]
        return GroqProvider(api_key=key, model=model)
    if provider_name == "gemini":
        key = (settings.get("gemini_api_key") or "").strip()
        model = (settings.get("gemini_model") or GEMINI_MODELS[0]).strip()
        if not key:
            return None
        if model not in GEMINI_MODELS:
            model = GEMINI_MODELS[0]
        return GeminiProvider(api_key=key, model=model)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────────

GIFT_VERDICT_SYSTEM = """\
Ты — эксперт по NFT-подаркам Telegram. Анализируешь конкретный лот для трейдера.
Отвечай КРАТКО (1-3 предложения, < 250 знаков), на русском, без воды.
Указывай: стоит ли брать (BUY / HOLD / SKIP), почему, и потенциальный профит/риск.
Используй только данные которые тебе передали. Если данных мало — скажи об этом.
Не используй markdown, заголовки, списки. Только сплошной текст."""

DIGEST_SUMMARY_SYSTEM = """\
Ты — аналитик NFT-маркета подарков Telegram. На входе — статистика за день.
Дай КРАТКИЙ (3-5 предложений, < 600 знаков) брифинг на русском про общее настроение
рынка, какие коллекции трендят, и что советуешь делать трейдеру в ближайшие 24ч.
Не используй markdown, заголовки, списки. Только связный текст."""


def _format_gift_for_ai(gift: dict, market: str) -> str:
    """Превращает gift dict в компактный текст для LLM."""
    name = gift.get("name") or "?"
    number = gift.get("number") or "?"
    price = gift.get("price") or 0
    floor = gift.get("floor_price")
    discount = ""
    if floor and price and floor > price:
        d_pct = round((float(floor) - float(price)) / float(floor) * 100, 1)
        discount = f", дисконт −{d_pct}% от floor"
    rar = gift.get("rarities_pm") or {}
    rar_parts = []
    for k in ("model", "backdrop", "symbol"):
        v = rar.get(k)
        if v:
            rar_parts.append(f"{k}={v}‰")
    rar_str = (", " + ", ".join(rar_parts)) if rar_parts else ""

    extras = []
    if gift.get("model_name"):
        extras.append(f"model={gift['model_name']}")
    if gift.get("backdrop_name"):
        extras.append(f"backdrop={gift['backdrop_name']}")
    if gift.get("symbol_name"):
        extras.append(f"symbol={gift['symbol_name']}")
    extras_str = (", " + ", ".join(extras)) if extras else ""

    floor_str = f", floor={floor}" if floor else ""
    return (
        f"{name} #{number}, маркет={market}, цена={price} TON"
        f"{floor_str}{discount}{rar_str}{extras_str}"
    )


def _format_digest_for_ai(stats: dict) -> str:
    parts = [
        f"Всего алертов за {stats.get('window_hours', 24)}ч: {stats.get('total_alerts', 0)}",
    ]
    by_market = stats.get("by_market") or {}
    if by_market:
        parts.append("По маркетам: " + ", ".join(
            f"{m}={c}" for m, c in by_market.items()
        ))
    if stats.get("biggest_discount_pct"):
        parts.append(f"Биггест-дисконт: {stats['biggest_discount_pct']}% от floor")
    if stats.get("avg_savings_ton"):
        parts.append(f"Средняя экономия: {stats['avg_savings_ton']} TON/алерт")
    top = stats.get("top_deals") or []
    if top:
        parts.append("Топ сделок:")
        for d in top[:5]:
            parts.append(
                f"- {d.get('name')} #{d.get('number')}: "
                f"{d.get('price')}TON, −{d.get('discount_pct')}%, {d.get('market')}"
            )
    hottest = stats.get("hottest_collections") or []
    if hottest:
        parts.append("Хот-коллекции:")
        for h in hottest:
            parts.append(
                f"- {h['name']} ({h['count']} алертов, avg {h['avg_discount']}%)"
            )
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def analyze_gift(provider: Any, gift: dict, market: str) -> str:
    """Возвращает 1-3 предложения с вердиктом по конкретному лоту, или ''."""
    if provider is None:
        return ""
    user_prompt = _format_gift_for_ai(gift, market)
    return await provider.chat(GIFT_VERDICT_SYSTEM, user_prompt, max_tokens=180)


async def analyze_daily(provider: Any, digest_stats: dict) -> str:
    """Брифинг по сводке за сутки, или ''."""
    if provider is None:
        return ""
    user_prompt = _format_digest_for_ai(digest_stats)
    return await provider.chat(DIGEST_SUMMARY_SYSTEM, user_prompt, max_tokens=350)


async def test_provider(provider: Any) -> tuple[bool, str]:
    """Возвращает (ok, message_or_error). Используется командой /ai_test."""
    if provider is None:
        return (False, "AI провайдер не настроен (выберите Groq или Gemini в Mini App).")
    text = await provider.chat(
        "Ты — тестовый бот. Отвечай ОЧЕНЬ кратко.",
        "Скажи 'OK' и текущий год.",
        max_tokens=30,
        temperature=0.0,
    )
    if not text:
        return (False, "Пустой ответ от AI — проверьте API-ключ и доступ к модели.")
    return (True, text)
