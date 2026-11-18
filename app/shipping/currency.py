"""Currency conversion through AMD using Central Bank of Armenia rates.

Rates are cached per day in the database. If both providers fail, the last
cached rates are used and the report says so (stale_since).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from app.config import Config
from app.database.repository import Repository
from app.utils.http import PoliteClient

log = logging.getLogger(__name__)

CBA_SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body><ExchangeRatesLatest xmlns="http://www.cba.am/" /></soap:Body></soap:Envelope>"""


@dataclass
class FxTable:
    """amd_per_unit[ccy] = how many AMD one unit of ccy costs."""

    amd_per_unit: dict[str, float]
    rate_date: str | None
    source: str
    stale: bool = False

    def convert(self, amount: float | None, src: str, dst: str) -> float | None:
        if amount is None:
            return None
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return amount
        a = 1.0 if src == "AMD" else self.amd_per_unit.get(src)
        b = 1.0 if dst == "AMD" else self.amd_per_unit.get(dst)
        if not a or not b:
            return None
        return amount * a / b

    def to_usd(self, amount: float | None, src: str) -> float | None:
        return self.convert(amount, src, "USD")

    def to_amd(self, amount: float | None, src: str) -> float | None:
        return self.convert(amount, src, "AMD")


def parse_cba(xml_text: str) -> tuple[str | None, dict[str, float]]:
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    ns = {"c": "http://www.cba.am/"}
    date_el = root.find(".//c:CurrentDate", ns)
    rate_date = date_el.text[:10] if date_el is not None and date_el.text else None
    rates: dict[str, float] = {}
    for er in root.iterfind(".//c:ExchangeRate", ns):
        iso = (er.findtext("c:ISO", namespaces=ns) or "").strip().upper()
        try:
            amount = float(er.findtext("c:Amount", namespaces=ns) or 1)
            rate = float(er.findtext("c:Rate", namespaces=ns) or 0)
        except ValueError:
            continue
        if iso and rate > 0 and amount > 0:
            rates[iso] = rate / amount
    return rate_date, rates


def parse_open_er(data: dict) -> tuple[str | None, dict[str, float]]:
    """open.er-api.com / exchangerate-api: base USD -> rates[ccy] = units per USD."""
    rates = data.get("rates") or data.get("conversion_rates") or {}
    amd_per_usd = rates.get("AMD")
    if not amd_per_usd:
        return None, {}
    out = {"USD": float(amd_per_usd)}
    for ccy, per_usd in rates.items():
        if per_usd:
            out[ccy] = float(amd_per_usd) / float(per_usd)
    out.pop("AMD", None)
    ts = data.get("time_last_update_utc") or ""
    m = re.search(r"(\d{1,2}) (\w{3}) (\d{4})", ts)
    rate_date = datetime.strptime(" ".join(m.groups()), "%d %b %Y").strftime("%Y-%m-%d") if m else None
    return rate_date, out


def load_rates(cfg: Config, http: PoliteClient, repo: Repository, now: datetime) -> FxTable:
    today = now.strftime("%Y-%m-%d")
    cached_today = repo.get_fx(today)
    if {"USD", "EUR", "GBP"} <= cached_today.keys():
        return FxTable(cached_today, today, "cache")

    ex = cfg.shipping.exchange_rates
    try:
        resp = http.post(ex.cba_url, content=CBA_SOAP,
                         headers={"Content-Type": "text/xml; charset=utf-8",
                                  "SOAPAction": '"http://www.cba.am/ExchangeRatesLatest"'})
        resp.raise_for_status()
        rate_date, rates = parse_cba(resp.text)
        if {"USD", "EUR", "GBP"} <= rates.keys():
            repo.save_fx(today, rates, "cba", now)
            return FxTable(rates, rate_date or today, "Central Bank of Armenia")
        log.warning("CBA response missing currencies: %s", sorted(rates)[:10])
    except Exception as exc:
        log.warning("CBA rates failed: %s", exc)

    try:
        key = cfg.secrets.exchange_rate_api_key
        url = f"https://v6.exchangerate-api.com/v6/{key}/latest/USD" if key else ex.fallback_url
        resp = http.get(url, check_robots=False)
        resp.raise_for_status()
        rate_date, rates = parse_open_er(resp.json())
        if {"USD", "EUR", "GBP"} <= rates.keys():
            repo.save_fx(today, rates, "exchangerate-api", now)
            return FxTable(rates, rate_date or today, "ExchangeRate-API")
    except Exception as exc:
        log.warning("fallback FX rates failed: %s", exc)

    last_date, rates, source = repo.latest_fx()
    if rates:
        log.warning("using cached FX rates from %s", last_date)
        return FxTable(rates, last_date, f"{source} (cached)", stale=True)
    raise RuntimeError("no exchange rates available (both providers failed and no cache)")
