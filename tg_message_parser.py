"""
Парсер текстовых сообщений из Telegram-каналов.
Поддерживает максимум форматов объявлений о продаже подарков.
ВСЕ ЦЕНЫ возвращаются в TON: Stars конвертируются через rate_provider.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Паттерны для Stars-цены
_STARS_UNITS = r"(?:Stars?|⭐|★|star)"
# Паттерны для TON-цены
_TON_UNITS = r"(?:TON|Ton|ton|💎)"
# Число (с точкой/запятой)
_NUM = r"(\d[\d\s,.']*(?:[.,]\d+)?)"

# Тире-подобные символы (em-dash, en-dash, hyphen).
# Вынесено в обычный raw-string, чтобы НЕ ломать f-strings: внутри f-string
# фигурные скобки квантификатора {1,3} интерпретируются как Python-выражение.
_DASH = r"(?:[—–\-]{1,3})"


def parse_telegram_message(text: str) -> Optional[dict]:
    """
    Извлекает данные о подарке из текста Telegram-сообщения.
    Все цены приводятся к TON (Stars конвертируются через rate_provider).

    Поддерживаемые форматы:
      Stars:
        "🎁 Eternal Rose — 1500 Stars"
        "Crystal Dragon — 800⭐"
        "1 500⭐ Eternal Rose"
        "Name: Iron Crown | Price: 2500 Stars"
        "Цена: 800 ★ | Название: Eternal Rose"

      TON:
        "Price: 5.5 TON | Eternal Rose"
        "Eternal Rose for 3.2 TON"
        "2.5 TON — Crystal Dragon"
        "5.5💎 Eternal Rose"
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    first_line = text.split("\n")[0][:128]

    # ── Порядок попыток (от наиболее специфичных к общим) ─────────────────────

    # 1. "NAME — NUM Stars"
    m = re.search(
        rf"([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-'\"()#]+?)\s*{_DASH}\s*"
        rf"{_NUM}\s*{_STARS_UNITS}",
        text, re.IGNORECASE,
    )
    if m:
        name = _clean_name(m.group(1))
        price = _parse_num(m.group(2))
        if name and price:
            return _stars_result(name, price)

    # 2. "NAME — NUM TON" (TON-цена)
    m = re.search(
        rf"([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\s\-'\"()#]+?)\s*{_DASH}\s*"
        rf"{_NUM}\s*{_TON_UNITS}",
        text, re.IGNORECASE,
    )
    if m:
        name = _clean_name(m.group(1))
        price = _parse_num(m.group(2))
        if name and price:
            return {"name": name, "price": price, "currency": "TON"}

    # 3. Ведущая Stars-цена: "1500⭐ Eternal Rose" / "1 500 Stars Eternal Rose"
    m = re.search(
        rf"^{_NUM}\s*{_STARS_UNITS}\s+([A-Za-zА-Яа-яЁё].{{2,64}})",
        first_line, re.IGNORECASE,
    )
    if m:
        price = _parse_num(m.group(1))
        name = _clean_name(m.group(2))
        if price and name:
            return _stars_result(name, price)

    # 4. Ведущая TON-цена: "5.5 TON Eternal Rose" / "5.5💎 Eternal Rose"
    m = re.search(
        rf"^{_NUM}\s*{_TON_UNITS}\s+([A-Za-zА-Яа-яЁё].{{2,64}})",
        first_line, re.IGNORECASE,
    )
    if m:
        price = _parse_num(m.group(1))
        name = _clean_name(m.group(2))
        if price and name:
            return {"name": name, "price": price, "currency": "TON"}

    # 5. "Name/Название: X | Price/Цена: Y Stars/TON" (в любом порядке)
    name_m = re.search(
        r"(?:Name|Название|Title)\s*[:\-]\s*(.+?)(?:\||\n|$)",
        text, re.IGNORECASE,
    )
    price_m = re.search(
        rf"(?:Price|Цена|Стоимость|Cost)\s*[:\-]\s*{_NUM}\s*({_STARS_UNITS}|{_TON_UNITS})?",
        text, re.IGNORECASE,
    )
    if price_m:
        name = _clean_name(name_m.group(1)) if name_m else None
        price = _parse_num(price_m.group(1))
        currency_hint = (price_m.group(2) or "").upper()
        is_ton = "TON" in currency_hint or "💎" in currency_hint
        # Если имя не нашли через "Name:" — берём остаток first_line без "Price: …"
        if not name:
            cleaned_first = re.sub(
                rf"(?:Price|Цена|Стоимость|Cost)\s*[:\-]\s*{_NUM}\s*"
                rf"(?:{_STARS_UNITS}|{_TON_UNITS})?",
                "", first_line, flags=re.IGNORECASE,
            )
            cleaned_first = re.sub(r"\|+", " ", cleaned_first)
            name = _clean_name(cleaned_first)
        if name and price:
            if is_ton:
                return {"name": name, "price": price, "currency": "TON"}
            return _stars_result(name, price)

    # 6. "NAME for X TON" / "NAME за X TON"
    m = re.search(
        rf"^(.+?)\s+(?:for|за|by)\s+{_NUM}\s*{_TON_UNITS}",
        first_line, re.IGNORECASE,
    )
    if m:
        name = _clean_name(m.group(1))
        price = _parse_num(m.group(2))
        if price and name:
            return {"name": name, "price": price, "currency": "TON"}

    # 7. Standalone TON price anywhere (используем first_line без цены как имя)
    m = re.search(
        rf"{_NUM}\s*{_TON_UNITS}",
        text, re.IGNORECASE,
    )
    if m:
        price = _parse_num(m.group(1))
        if price and price <= 10_000:
            cleaned_first = re.sub(
                rf"{_NUM}\s*{_TON_UNITS}", "", first_line,
                flags=re.IGNORECASE,
            )
            name = _clean_name(cleaned_first) or _clean_name(first_line)
            if name:
                return {"name": name, "price": price, "currency": "TON"}

    # 8. Standalone Stars price (2-7 цифр)
    m = re.search(
        rf"\b(\d{{2,7}})\s*{_STARS_UNITS}",
        text, re.IGNORECASE,
    )
    if m:
        price = float(m.group(1))
        name = _clean_name(first_line)
        if price and name:
            return _stars_result(name, price)

    return None


def _stars_result(name: str, stars_price: float) -> dict:
    """
    Создаёт результат с конвертацией Stars→TON.
    Использует синхронный доступ к rate_provider (курс обновляется фоном).
    """
    try:
        from rate_provider import rate_provider
        rate = rate_provider.stars_to_ton(1.0)
        if rate <= 0:
            raise ValueError("rate is zero")
        price_ton = round(stars_price * rate, 6)
    except Exception:
        # Fallback: $0.02/Star ÷ $5/TON = 0.004 TON/Star
        price_ton = round(stars_price * 0.004, 6)

    return {
        "name": name,
        "price": price_ton,
        "stars_price": stars_price,
        "currency": "TON",   # После конвертации
    }


def _parse_num(s: str) -> Optional[float]:
    """Парсит число из строки, поддерживает разделители тысяч и запятую."""
    if not s:
        return None
    # Убираем пробелы и апострофы (разделители тысяч)
    s = re.sub(r"[\s'\u00a0]", "", s.strip())
    # Заменяем запятую на точку
    s = s.replace(",", ".")
    # Если несколько точек — берём последнюю как десятичную
    parts = s.split(".")
    if len(parts) > 2:
        s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def _clean_name(s: str) -> Optional[str]:
    """Очищает название от мусора."""
    if not s:
        return None
    # Убираем emoji-мусор в начале
    s = re.sub(r"^[\U0001F300-\U0001FFFF\u2600-\u26FF\u2700-\u27BF\s🎁💎⭐★\-—–]+", "", s)
    s = s.strip(" \t\n\r-—–#@")
    # Обрезаем до первого знака препинания, если слишком длинное
    if len(s) > 80:
        m = re.search(r"[.!?|]", s)
        if m:
            s = s[:m.start()].strip()
        else:
            s = s[:80].strip()
    return s if len(s) >= 2 else None
