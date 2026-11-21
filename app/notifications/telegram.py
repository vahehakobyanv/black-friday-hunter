"""Telegram notifications through a personal bot (free Telegram Bot API).

Setup: create a bot with @BotFather → token; send your bot any message; run
`python -m app.main telegram-setup` to print your chat id.
"""
from __future__ import annotations

import html
import logging

import httpx

from app.analysis.ranking import DealView, ReportSections

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram limit is 4096 characters per message


class TelegramNotConfigured(RuntimeError):
    pass


def _money(amount: float | None, currency: str) -> str:
    from app.notifications.report import money

    return money(amount, currency)


def deal_line(d: DealView, with_reasons: bool = False) -> str:
    l = d.best.listing
    e = html.escape
    price = _money(l.last_price, l.currency)
    usd = f" (≈{_money(d.best.price_usd, 'USD')})" if l.currency != "USD" else ""
    dl = d.best.delivery
    delivered = ""
    if dl.total_usd is not None:
        delivered = f"\n   📦 В Ереване: {'≈' if dl.complete else '≥'}{_money(dl.total_usd, 'USD')}"
        if dl.duty_eur:
            delivered += f" (вкл. пошлину ≈€{dl.duty_eur:.0f})"
        elif dl.duty_eur == 0:
            delivered += " · без пошлины"
    reasons = f"\n   ⭐ {e('; '.join(d.alert_reasons))}" if with_reasons and d.alert_reasons else ""
    from app.notifications.report import LABELS_RU

    return (f"<b>{e(l.title[:110])}</b>\n   {price}{usd} в {e(l.retailer)} · {LABELS_RU.get(d.label, d.label)} {d.total:.0f}/100"
            f"{delivered}{reasons}\n   <a href=\"{e(l.url, quote=True)}\">Открыть сделку</a>")


def format_alerts(alerts: list[DealView], when: str) -> str:
    lines = [f"🔥 <b>Срочно: выгодная сделка</b> · {html.escape(when)}"]
    lines += [f"\n{i}. {deal_line(d, with_reasons=True)}" for i, d in enumerate(alerts, 1)]
    return "\n".join(lines)


def format_summary(sections: ReportSections, subject: str, n: int) -> str:
    lines = [f"📬 <b>{html.escape(subject)}</b>"]
    for tier in sections.priorities:
        best = [d for _name, lst in tier.groups for d in lst[:1]]
        if best:
            lines.append(f"\n<b>{html.escape(tier.label)}</b>")
            lines += [f"• {deal_line(d)}" for d in best[:4]]
    if sections.top:
        lines.append("\n<b>Лучшие сделки</b>")
        lines += [f"{i}. {deal_line(d)}" for i, d in enumerate(sections.top[:n], 1)]
    lines.append("\nПолный отчёт с фото и прогнозами — в вашей почте.")
    return "\n".join(lines)


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > MAX_LEN and cur:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + block) if cur else block
    if cur:
        out.append(cur[:MAX_LEN])
    return out


def send_telegram(token: str | None, chat_id: str | None, text: str,
                  transport: httpx.BaseTransport | None = None) -> None:
    if not token or not chat_id:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
    with httpx.Client(timeout=20, transport=transport) as client:
        for part in _chunks(text):
            resp = client.post(API.format(token=token, method="sendMessage"),
                               json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                                     "disable_web_page_preview": True})
            if resp.status_code != 200:
                raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text[:200]}")
    log.info("telegram message sent (%d chars)", len(text))


def discover_chat_ids(token: str) -> list[tuple[str, str]]:
    """Return (chat_id, name) of chats that recently messaged the bot."""
    with httpx.Client(timeout=20) as client:
        resp = client.get(API.format(token=token, method="getUpdates"))
        resp.raise_for_status()
    out = {}
    for upd in resp.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
            out[str(chat["id"])] = name
    return list(out.items())
