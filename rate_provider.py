"""
rate_provider.py — Динамический курс конвертации Stars → TON.

Telegram Stars стоят ~$0.02 за штуку (50 Stars = $0.99 официально).
TON/USD берём с публичного TON API (без авторизации).
Курс кешируется на 30 минут.

Формула: stars_in_ton = stars * STAR_USD / TON_USD
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Официальная цена Stars: 50 звёзд = $0.99 → ≈ $0.0198/звезда
STAR_USD: float = 0.02

# Fallback TON/USD если API недоступен
FALLBACK_TON_USD: float = 5.0

# Минимально допустимый курс (защита от 0)
MIN_TON_USD: float = 0.5

# Интервал обновления курса
RATE_CACHE_TTL: int = 30 * 60  # 30 минут

# User-Agent для HTTP-запросов (некоторые API режектят default Python UA)
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json",
}


class RateProvider:
    """
    Потокобезопасный провайдер курса TON/USD и конвертации Stars→TON.
    Кешируется, автоматически обновляется.
    """

    def __init__(self):
        self._ton_usd: float = FALLBACK_TON_USD
        self._last_update: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def ton_usd(self) -> float:
        return self._ton_usd

    @property
    def star_usd(self) -> float:
        return STAR_USD

    def stars_to_ton(self, stars: float) -> float:
        """Конвертирует Stars в TON по текущему курсу."""
        if self._ton_usd <= 0:
            return stars * STAR_USD / FALLBACK_TON_USD
        return round(stars * STAR_USD / self._ton_usd, 6)

    def ton_to_stars(self, ton: float) -> float:
        """Конвертирует TON в Stars по текущему курсу (для справки)."""
        return round(ton * self._ton_usd / STAR_USD)

    def is_stale(self) -> bool:
        return (time.time() - self._last_update) > RATE_CACHE_TTL

    async def update(self) -> bool:
        """Обновляет курс TON/USD. Возвращает True при успехе."""
        async with self._lock:
            if not self.is_stale():
                return True  # Не нужно обновлять

            # Пробуем несколько источников
            for fetch_fn in [
                self._fetch_tonapi,
                self._fetch_coingecko,
                self._fetch_binance,
            ]:
                try:
                    price = await fetch_fn()
                    if price and price >= MIN_TON_USD:
                        old = self._ton_usd
                        self._ton_usd = price
                        self._last_update = time.time()
                        logger.info(
                            f"TON/USD обновлён: ${price:.3f}"
                            + (f" (было ${old:.3f})" if abs(price - old) > 0.01 else "")
                        )
                        return True
                except Exception as e:
                    logger.debug(f"Rate source {fetch_fn.__name__} failed: {e}")

            logger.warning(f"Не удалось обновить курс TON/USD, используем ${self._ton_usd:.3f}")
            # Помечаем как частично обновлённый, чтобы не спамить запросами
            self._last_update = time.time() - RATE_CACHE_TTL + 300  # Повтор через 5 мин
            return False

    async def ensure_fresh(self):
        """Вызывается перед использованием курса. Обновляет если устарел."""
        if self.is_stale():
            await self.update()

    # ── Источники курса ──────────────────────────────────────────────────────

    @staticmethod
    async def _fetch_tonapi() -> Optional[float]:
        """TON API — официальный эндпоинт."""
        url = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    # {"rates": {"TON": {"prices": {"USD": 5.23}}}}
                    return float(
                        data["rates"]["TON"]["prices"]["USD"]
                    )
        return None

    @staticmethod
    async def _fetch_coingecko() -> Optional[float]:
        """CoinGecko — популярный агрегатор цен."""
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd"
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return float(data["the-open-network"]["usd"])
        return None

    @staticmethod
    async def _fetch_binance() -> Optional[float]:
        """Binance API — спотовая цена TON/USDT."""
        url = "https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT"
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return float(data["price"])
        return None

    def format_rate_info(self) -> str:
        """Возвращает строку с текущим курсом для отображения в /status."""
        age_min = int((time.time() - self._last_update) / 60)
        stars_per_ton = self.ton_to_stars(1.0)
        return (
            f"💱 TON/USD: <b>${self._ton_usd:.3f}</b>\n"
            f"   1 TON ≈ {stars_per_ton:,} ⭐  |  обновлено {age_min} мин назад"
        ).replace(",", " ")


# Глобальный синглтон
rate_provider = RateProvider()
