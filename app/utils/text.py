"""Text and price parsing helpers."""
from __future__ import annotations

import html
import re
import unicodedata

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₽": "RUB", "֏": "AMD"}


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    """Parse '£241.92', '76,76€', '1.299,00 €', '$1,299.99', 'EUR 12' -> (value, currency)."""
    if not text:
        return None, None
    t = html.unescape(str(text)).strip()
    currency = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in t:
            currency = code
            break
    if currency is None:
        m = re.search(r"\b(USD|EUR|GBP|AMD)\b", t, re.I)
        if m:
            currency = m.group(1).upper()
    m = re.search(r"\d[\d.,\s']*", t)
    if not m:
        return None, currency
    num = m.group(0).strip().replace(" ", "").replace("'", "").rstrip(".,")
    if "," in num and "." in num:
        # whichever comes last is the decimal separator
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = (head.replace(",", "") + "." + tail) if len(tail) in (1, 2) else num.replace(",", "")
    elif num.count(".") == 1:
        head, _, tail = num.partition(".")
        if len(tail) == 3 and len(head) <= 3 and currency in ("EUR",):
            num = head + tail  # "1.299" in German = 1299
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None, currency
    return (value if value > 0 else None), currency


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</li>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fold(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace (for matching)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Word-boundary, case/accent-insensitive phrase match.
    A trailing '*' makes the last word a prefix: 'galaxy s*' matches 'Galaxy S25'."""
    prefix = phrase.rstrip().endswith("*")
    h = " " + re.sub(r"[^a-z0-9.+!&']+", " ", fold(haystack)) + " "
    p = re.sub(r"[^a-z0-9.+!&']+", " ", fold(phrase.rstrip().rstrip("*"))).strip()
    if not p:
        return False
    return f" {p}" in h if prefix else f" {p} " in h
