"""
AI advisor — обёртка над LLM-провайдерами Groq и Google Gemini.
Используется для:
1. Кратких комментариев под каждым алертом ("стоит ли брать") — async, не блокирует
2. Аналитики дня в начале daily digest
3. On-demand вердикт по нажатию кнопки "🤖 Спросить AI" в алерте
4. /ai_ask <вопрос> — свободный диалог с AI (контекст: рынок NFT-подарков)

Архитектура:
- BaseProvider — protocol с одним методом async chat(messages, **kw) -> str
- GroqProvider, GeminiProvider — реализации
- get_active_provider(settings) -> BaseProvider | None
- get_fast_provider(settings) -> BaseProvider | None — быстрая модель для авто-вердиктов
- get_fallback_provider(settings) -> BaseProvider | None — резерв при квоте/сбое
- analyze_gift(provider, gift, context, task=...) -> str
- analyze_daily(provider, digest_stats) -> str
- free_chat(provider, question) -> str

Persona-профили: trader / speculator / collector / custom — разный тон и приоритеты.
Все ошибки сетевые/auth/quota не ронят бот — функции возвращают пустую строку
и логируют warning. aiohttp.ClientSession переиспользуется (один на провайдер).

v2 фишки (2026-05):
- Per-task model selection: авто-вердикт идёт на ai_fast_model (llama-3.1-8b-instant
  по дефолту, ~50-150ms), /ai_ask и digest — на основной более умной модели.
- Response cache: вердикт по re-listing того же лота берётся из памяти (ttl 5 мин).
- Fallback chain: при ошибке/таймауте основного провайдера — пытаемся резерв.
- Stats counters: команда /ai_stats показывает hit-rate / по-провайдеру / по-задаче.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

import ai_cache

logger = logging.getLogger(__name__)


# Один shared connector на процесс — экономит TCP handshakes.
# keepalive_timeout=60 — Groq/Gemini держат соединение ~60s.
_shared_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    """Lazy-init shared aiohttp session. Не закрываем — живёт до конца процесса."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=15),
        )
    return _shared_session


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

    name = "groq"
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
            session = await _get_session()
            async with session.post(
                self.BASE_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    logger.warning(f"Groq API {resp.status} model={self.model}: {body}")
                    return ""
                data = await resp.json()
                return (data["choices"][0]["message"]["content"] or "").strip()
        except asyncio.TimeoutError:
            logger.warning(f"Groq API: timeout (model={self.model})")
            return ""
        except Exception:
            logger.exception("Groq API: unexpected error")
            return ""


class GeminiProvider:
    """Google Gemini — REST API. Документация: https://ai.google.dev/api"""

    name = "gemini"
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
            session = await _get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    logger.warning(f"Gemini API {resp.status} model={self.model}: {body}")
                    return ""
                data = await resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    return ""
                parts = candidates[0].get("content", {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts).strip()
                return text
        except asyncio.TimeoutError:
            logger.warning(f"Gemini API: timeout (model={self.model})")
            return ""
        except Exception:
            logger.exception("Gemini API: unexpected error")
            return ""


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

def _build_provider(name: str, api_key: str, model: str) -> Any | None:
    """Универсальная фабрика по строковому имени.
    Возвращает None если ключа нет / провайдер неизвестен."""
    name = (name or "").lower().strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not api_key:
        return None
    if name == "groq":
        if model not in GROQ_MODELS:
            model = GROQ_MODELS[0]
        return GroqProvider(api_key=api_key, model=model)
    if name == "gemini":
        if model not in GEMINI_MODELS:
            model = GEMINI_MODELS[0]
        return GeminiProvider(api_key=api_key, model=model)
    return None


def get_active_provider(settings: dict) -> Any | None:
    """Основной провайдер для /ai_ask, daily digest, on-demand vердикта."""
    provider_name = (settings.get("ai_provider") or "off").lower().strip()
    if provider_name == "groq":
        return _build_provider(
            "groq",
            settings.get("groq_api_key") or "",
            settings.get("groq_model") or GROQ_MODELS[0],
        )
    if provider_name == "gemini":
        return _build_provider(
            "gemini",
            settings.get("gemini_api_key") or "",
            settings.get("gemini_model") or GEMINI_MODELS[0],
        )
    return None


def get_fast_provider(settings: dict) -> Any | None:
    """Быстрый провайдер для авто-вердикта под алертом (fire-and-forget).

    Использует тот же API-ключ, что и основной, но более быстрая модель —
    `ai_fast_model` (по дефолту llama-3.1-8b-instant). Если основной провайдер
    Gemini — пытаемся использовать gemini-2.0-flash-lite (если разрешён),
    иначе fallback на основную модель.

    Возвращает None если основной провайдер не настроен.
    """
    provider_name = (settings.get("ai_provider") or "off").lower().strip()
    fast_model = (settings.get("ai_fast_model") or "").strip()
    if provider_name == "groq":
        key = settings.get("groq_api_key") or ""
        # llama-3.1-8b-instant ≈ 50-150ms — это самая быстрая Groq-модель
        model = fast_model if fast_model in GROQ_MODELS else "llama-3.1-8b-instant"
        if model not in GROQ_MODELS:
            model = settings.get("groq_model") or GROQ_MODELS[0]
        return _build_provider("groq", key, model)
    if provider_name == "gemini":
        key = settings.get("gemini_api_key") or ""
        # gemini-2.0-flash-lite — самая быстрая среди Gemini
        model = fast_model if fast_model in GEMINI_MODELS else "gemini-2.0-flash-lite"
        if model not in GEMINI_MODELS:
            model = settings.get("gemini_model") or GEMINI_MODELS[0]
        return _build_provider("gemini", key, model)
    return None


def get_fallback_provider(settings: dict) -> Any | None:
    """Резервный провайдер, отдельный от основного. Используется при сбое/квоте."""
    fb = (settings.get("ai_fallback_provider") or "off").lower().strip()
    if fb in ("", "off", "none"):
        return None
    key = (settings.get("ai_fallback_api_key") or "").strip()
    if not key:
        # Если резервный ключ не задан, но включен fallback — пробуем основной ключ
        # того же провайдера (на случай если у пользователя один ключ Groq и
        # один Gemini — он мог поставить как primary один, а как fallback другой
        # но забыть прописать ключ).
        if fb == "groq":
            key = (settings.get("groq_api_key") or "").strip()
        elif fb == "gemini":
            key = (settings.get("gemini_api_key") or "").strip()
    model = (settings.get("ai_fallback_model") or "").strip()
    return _build_provider(fb, key, model)


# ──────────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Persona-профили — пользователь выбирает в Mini App, prompt подстраивается под его стиль.
# ──────────────────────────────────────────────────────────────────────────────
AI_PERSONAS = {
    "trader": {
        "label": "📈 Трейдер (флипы +5-15%)",
        "system": "Ты — опытный трейдер NFT-подарков Telegram. Цель — флип за 1-3 дня "
                  "с прибылью +5-15%. Оцениваешь ликвидность, скорость продажи, риск "
                  "падения floor. BUY = можно перепродать +10% за 48ч; HOLD = ждать "
                  "лучшей цены; SKIP = риск стака на бирже.",
    },
    "speculator": {
        "label": "🎲 Спекулянт (агрессивные сделки)",
        "system": "Ты — агрессивный спекулянт NFT-подарков. Ищешь дисконты ≥20% и "
                  "редкие модели для быстрой перепродажи с большим профитом. Игнорируй "
                  "малоценные сделки. BUY = очень выгодно, рекомендуешь брать сразу.",
    },
    "collector": {
        "label": "🏛 Коллекционер (редкости)",
        "system": "Ты — коллекционер редких NFT-подарков. Главное — уникальные "
                  "атрибуты (rare model/backdrop/symbol), а не сиюминутный профит. "
                  "BUY = редкость которую не повторить; SKIP = массовый лот без "
                  "уникальных атрибутов.",
    },
    "balanced": {
        "label": "⚖️ Балансированный (по умолчанию)",
        "system": "Ты — эксперт по NFT-подаркам Telegram. Анализируешь конкретный лот "
                  "для трейдера. Указывай: стоит ли брать (BUY / HOLD / SKIP), почему, "
                  "и потенциальный профит/риск.",
    },
    "custom": {
        "label": "✏️ Свой prompt",
        "system": "",  # заменяется на ai_custom_prompt из настроек
    },
}

GIFT_VERDICT_FORMAT = (
    " Отвечай СТРОГО в формате: первая строка — одно слово BUY / HOLD / SKIP, "
    "затем «conf:N/10» где N=0-10 уверенность; затем тире и 1-2 предложения "
    "почему именно так. Всего < 250 знаков, русский, без markdown. "
    "Используй только переданные данные. Если данных мало — conf:1/10 + "
    "пометка 'мало данных'."
)

DIGEST_SUMMARY_SYSTEM = """\
Ты — аналитик NFT-маркета подарков Telegram. На входе — статистика за день.
Дай КРАТКИЙ (3-5 предложений, < 600 знаков) брифинг на русском про общее настроение
рынка, какие коллекции трендят, и что советуешь делать трейдеру в ближайшие 24ч.
Не используй markdown, заголовки, списки. Только связный текст."""

FREE_CHAT_SYSTEM = """\
Ты — помощник трейдера NFT-подарков Telegram. Отвечаешь на свободные вопросы
про маркеты MRKT, Portals, Fragment; цены floor, редкости, стратегии.
Отвечай на русском, кратко (≤ 600 знаков), по делу. Если не знаешь — скажи об этом."""


def _resolve_system_prompt(settings: dict, base_format: str = "") -> str:
    """Возвращает system-prompt для analyze_gift в зависимости от выбранной persona."""
    persona = (settings.get("ai_persona") or "balanced").lower().strip()
    if persona == "custom":
        custom = (settings.get("ai_custom_prompt") or "").strip()
        if custom:
            return custom + base_format
        # Fallback на balanced если кастомный пуст
        persona = "balanced"
    return AI_PERSONAS.get(persona, AI_PERSONAS["balanced"])["system"] + base_format


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

    # Контекст: статистика по коллекции за 24ч из alerts_log.
    # Помогает модели сравнить текущий лот с историей: «5 алертов, лучший
    # дисконт −18%, средняя цена 2.4 TON». Без этого AI «слепой».
    history_str = ""
    try:
        from database import collection_history
        h = collection_history(name, hours=24) if name and name != "?" else {}
        if h.get("alerts_count"):
            cnt = h["alerts_count"]
            best = h.get("best_discount_pct")
            avg = h.get("avg_discount_pct")
            min_p = h.get("min_price")
            parts = [f"24ч: {cnt} алертов"]
            if best is not None:
                parts.append(f"лучший −{best}%")
            if avg is not None:
                parts.append(f"средний −{avg}%")
            if min_p is not None:
                parts.append(f"min={min_p}TON")
            history_str = " | " + ", ".join(parts)
    except Exception:
        pass

    return (
        f"{name} #{number}, маркет={market}, цена={price} TON"
        f"{floor_str}{discount}{rar_str}{extras_str}{history_str}"
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

async def _chat_with_fallback(primary: Any, fallback: Any | None,
                              system: str, user: str, *,
                              max_tokens: int = 180,
                              temperature: float = 0.7,
                              task: str = "auto") -> tuple[str, str]:
    """Зовёт primary, при пустом ответе пытается fallback.
    Возвращает (text, used_provider_name)."""
    text = await primary.chat(system, user, max_tokens=max_tokens,
                              temperature=temperature)
    if text:
        ai_cache.record_miss(primary.name, len(system) + len(user), len(text))
        return text, primary.name
    # Fallback при пустом ответе. Проверяем что fallback != primary полностью
    # (имя+модель), иначе тот же неработающий вызов повторим.
    if fallback is not None and (
        fallback.name != primary.name
        or getattr(fallback, "model", "") != getattr(primary, "model", "")
    ):
        ai_cache.record_fallback()
        text = await fallback.chat(system, user, max_tokens=max_tokens,
                                   temperature=temperature)
        if text:
            ai_cache.record_miss(fallback.name, len(system) + len(user), len(text))
            return text, fallback.name
    ai_cache.record_error()
    return "", primary.name


async def analyze_gift(provider: Any, gift: dict, market: str,
                       settings: dict | None = None,
                       *, task: str = "on_demand",
                       fallback: Any | None = None) -> str:
    """Возвращает 1-3 предложения с вердиктом по конкретному лоту, или ''.

    settings: текущий снапшот настроек, чтобы выбрать persona/custom prompt.
    task:     "auto" (под алертом, кэш агрессивный) | "on_demand" (нажал кнопку,
              кэш более мягкий — пользователь хочет «свежее» мнение).
    fallback: резервный провайдер (если primary вернёт пусто).
    """
    if provider is None:
        return ""
    s = settings or {}
    persona = (s.get("ai_persona") or "balanced").lower().strip()
    model = getattr(provider, "model", "")
    sig = ai_cache.make_signature(gift, market, persona=persona, model=model)
    ttl = int(s.get("ai_cache_ttl_sec") or 0)
    # Auto-вердикт берёт из кэша агрессивно; on_demand — только если очень свежий.
    effective_ttl = ttl if task == "auto" else min(ttl, 60)
    ai_cache.record_request(task)
    cached = ai_cache.get(sig, effective_ttl)
    if cached is not None:
        return cached
    system = _resolve_system_prompt(s, GIFT_VERDICT_FORMAT)
    user_prompt = _format_gift_for_ai(gift, market)
    text, _ = await _chat_with_fallback(
        provider, fallback, system, user_prompt,
        max_tokens=180, temperature=0.6, task=task,
    )
    if text:
        ai_cache.put(sig, text)
    return text


async def analyze_daily(provider: Any, digest_stats: dict,
                        *, fallback: Any | None = None) -> str:
    """Брифинг по сводке за сутки, или ''."""
    if provider is None:
        return ""
    ai_cache.record_request("digest")
    user_prompt = _format_digest_for_ai(digest_stats)
    text, _ = await _chat_with_fallback(
        provider, fallback, DIGEST_SUMMARY_SYSTEM, user_prompt,
        max_tokens=350, temperature=0.7, task="digest",
    )
    return text


async def free_chat(provider: Any, question: str,
                    *, fallback: Any | None = None) -> str:
    """Свободный диалог. Используется командой /ai_ask."""
    if provider is None:
        return ""
    q = (question or "").strip()
    if not q:
        return ""
    ai_cache.record_request("chat")
    text, _ = await _chat_with_fallback(
        provider, fallback, FREE_CHAT_SYSTEM, q[:1000],
        max_tokens=400, temperature=0.7, task="chat",
    )
    return text


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
