"""Build the hourly HTML (+ plain-text) report."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.analysis.ranking import DealView, ReportSections
from app.config import Config
from app.models.schemas import CollectorStatus
from app.shipping.currency import FxTable

TEMPLATES = Path(__file__).parent / "templates"
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "AMD": "֏"}
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
LABELS_RU = {"EXCELLENT": "ОТЛИЧНО", "VERY GOOD": "ОЧЕНЬ ХОРОШО", "GOOD": "ХОРОШО", "NORMAL": "ОБЫЧНО",
             "NOT INTERESTING": "НЕИНТЕРЕСНО"}
STATUS_RU = {"NEW": "НОВОЕ", "PRICE DROP": "ЦЕНА СНИЖЕНА", "SEEN BEFORE": "УЖЕ БЫЛО"}
ADVICE_RU = {"BUY NOW": "ПОКУПАТЬ СЕЙЧАС", "WAIT": "ЖДАТЬ", "MAYBE WAIT": "ВОЗМОЖНО, ЖДАТЬ", "NO DATA": "НЕТ ДАННЫХ"}
KIND_RU = {"confirmed": "подтверждено", "advertised": "рекламная", "estimated": "оценка", "predicted": "прогноз",
           "unverified": "не проверено"}
CONF_RU = {"low": "низкая", "medium": "средняя", "high": "высокая"}


def ru(table: dict[str, str]):
    return lambda v: table.get(str(v), table.get(str(v).lower(), str(v)))


def rudate(dt, with_time: bool = False) -> str:
    out = f"{dt.day} {MONTHS_SHORT[dt.month - 1]}"
    return out + f" {dt:%H:%M}" if with_time else out


def days_ru(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


SOURCE_LABELS = {
    "mydealz_hot": "mydealz.de (hot)", "mydealz_new": "mydealz.de (new)",
    "hotukdeals_hot": "hotukdeals (hot)", "hotukdeals_new": "hotukdeals (new)",
    "dealnews": "dealnews.com", "bestbuy": "Best Buy API", "ebay": "eBay API",
    "watchlist_pages": "Страницы товаров из списка желаний", "exchange_rates": "Курсы валют",
    "dealcatcher": "DealCatcher", "9to5toys": "9to5Toys", "bf_ads": "Каталоги blackfriday.com",
    "androidpolice": "Android Police", "tomsguide": "Tom's Guide", "wccftech": "Wccftech",
}
REF_LABELS = {
    "next_best_price": "цена в следующем по дешевизне магазине", "regular_price": "обычная цена",
    "list_price": "заявленная «старая» цена", "own_history": "наша история цен", "none": "нет",
}


@dataclass
class RenderedReport:
    subject: str
    html: str
    text: str


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "нет данных"
    sym = SYMBOLS.get(currency.upper())
    if currency.upper() == "AMD":
        return f"{amount:,.0f} ֏"
    value = f"{amount:,.2f}" if amount < 1000 else f"{amount:,.0f}"
    return f"{sym}{value}" if sym else f"{value} {currency}"


def opt_money(amount: float | None, currency: str) -> str:
    return money(amount, currency) if amount is not None else "н/д"


def countdown(today, cfg: Config) -> str:
    bf, cm = cfg.settings.app.black_friday, cfg.settings.app.cyber_monday
    if today < bf:
        n = (bf - today).days
        return f"до Black Friday: {days_ru(n)} ({rudate(bf)})"
    if today == bf:
        return "СЕГОДНЯ BLACK FRIDAY"
    if today < cm:
        n = (cm - today).days
        return f"до Cyber Monday: {days_ru(n)}"
    if today == cm:
        return "СЕГОДНЯ CYBER MONDAY"
    return "Отслеживание после Cyber Monday"


def build_banners(cfg: Config, fx: FxTable, statuses: list[CollectorStatus], deals: list[DealView]) -> list[str]:
    banners = []
    regions = {d.best.listing.region for d in deals}
    for fw in cfg.shipping.forwarders:
        if fw.rate_per_kg is None and fw.origin in regions:
            banners.append(f"{fw.name}: тариф за кг не задан — цены с доставкой из {fw.origin} неполные. "
                           f"Укажите его в config/shipping_rates.yaml.")
    if fx.stale:
        banners.append(f"Не удалось обновить курсы валют; используются сохранённые от {fx.rate_date}.")
    failed = [s for s in statuses if not s.ok]
    if failed:
        banners.append("В этом запуске не сработали источники: "
                       + ", ".join(SOURCE_LABELS.get(s.source, s.source) for s in failed)
                       + ". Их последние данные (в пределах срока свежести) всё равно показаны.")
    if statuses and not any(s.ok and s.offers for s in statuses):
        banners.append("Ни один источник не дал свежих предложений — всё ниже из прошлых запусков (метки «УСТАРЕЛО»).")
    return banners


def render_report(*, sections: ReportSections, deals: list[DealView], statuses: list[CollectorStatus], fx: FxTable,
                  cfg: Config, now: datetime, run_id: int, last_email: datetime | None, offers_count: int,
                  alert_only: bool = False) -> RenderedReport:
    tz = ZoneInfo(cfg.settings.app.timezone)
    local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)

    def local_time(dt: datetime) -> str:
        return rudate(dt.replace(tzinfo=timezone.utc).astimezone(tz), with_time=True)

    cats = cfg.categories.categories
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(
        money=money, opt_money=opt_money, amd=lambda v: money(v, "AMD") if v is not None else "нет данных",
        L=ru(LABELS_RU), ST=ru(STATUS_RU), ADV=ru(ADVICE_RU), K=ru(KIND_RU), CONF=ru(CONF_RU), rudate=rudate,
        local_time=local_time, source_label=lambda s: SOURCE_LABELS.get(s, s),
        category_label=lambda k: cats[k].label if k in cats else k.title(),
        ref_label=lambda k: REF_LABELS.get(k, k),
    )
    n = len(sections.top)
    day = f"{local_now.day} {MONTHS_GEN[local_now.month - 1]}"
    subject = f"{cfg.settings.app.name} | Топ-{n} сделок | {day} | {local_now:%H}:00"
    if sections.alerts:
        subject += f" | оповещений: {len(sections.alerts)}"
    elif not sections.opportunities:
        subject += " | новых сделок нет"
    if alert_only:
        first = sections.alerts[0].best.listing if sections.alerts else None
        subject = (f"🔥 Срочно: сделка | {first.title[:45]} {money(first.last_price, first.currency)}"
                   if first else "🔥 Срочно: сделка")
        if len(sections.alerts) > 1:
            subject += f" и ещё {len(sections.alerts) - 1}"
        subject += f" | {rudate(local_now, with_time=True)}"
    regime = cfg.shipping.customs.regime
    html = env.get_template("report.html.j2").render(
        subject=subject,
        alert_only=alert_only,
        headline=(f"🔥 Срочных оповещений: {len(sections.alerts)}" if alert_only else
                  f"Оповещений: {len(sections.alerts)} · лучшая сделка {sections.top[0].total:.0f}/100" if sections.top and sections.alerts
                  else f"Лучшая сделка {sections.top[0].total:.0f}/100" if sections.top else "Подходящих сделок пока нет"),
        generated_local=f"{rudate(local_now)} {local_now.year}, {local_now:%H:%M} (Ереван)",
        countdown=countdown(local_now.date(), cfg),
        deals_count=len(deals),
        offers_count=offers_count,
        new_count=sections.new_count,
        fx_label=f"{fx.source} {fx.rate_date or ''} (1 USD = {fx.amd_per_unit.get('USD', 0):.2f} AMD)",
        banners=build_banners(cfg, fx, statuses, deals),
        sections=sections,
        watchlist=sections.watchlist,
        category_labels=_ordered_category_labels(cfg),
        statuses=statuses,
        ttl_hours=cfg.settings.filters.offer_ttl_hours,
        customs_label=(f"без пошлины до €{regime.duty_free_value_eur:g} / {regime.duty_free_weight_kg:g} кг, "
                       f"сверх лимита {regime.rate_on_excess_pct:g}% (проверено {cfg.shipping.customs.verified_on})"),
        run_id=run_id,
        last_email=local_time(last_email) if last_email else None,
    )
    text = render_text(sections, subject) if not alert_only else "\n".join(
        [subject, ""] + [f"- {d.best.listing.title} — {money(d.best.listing.last_price, d.best.listing.currency)} "
                         f"({'; '.join(d.alert_reasons)})\n  {d.best.listing.url}" for d in sections.alerts])
    return RenderedReport(subject=subject, html=html, text=text)


def _ordered_category_labels(cfg: Config) -> dict[str, str]:
    """Focus categories first (marked ★), then the rest in config order."""
    cats = cfg.categories.categories
    focus = [k for k in cfg.settings.email.focus_categories if k in cats]
    out = {k: "★ " + cats[k].label for k in focus}
    out.update({k: c.label for k, c in cats.items() if k not in out})
    return out


def render_text(sections: ReportSections, subject: str) -> str:
    lines = [subject, "", "ЛУЧШИЕ СДЕЛКИ"]
    for i, d in enumerate(sections.top, 1):
        l = d.best.listing
        approx = "≈" if d.best.delivery.complete else "≥"
        delivered = f" | с доставкой {approx} {money(d.best.delivery.total_usd, 'USD')}" if d.best.delivery.total_usd else ""
        lines.append(f"{i}. [{d.total:.0f} {LABELS_RU.get(d.label, d.label)}] {l.title} — {money(l.last_price, l.currency)}"
                     f" в {l.retailer}{delivered} [{STATUS_RU.get(d.status, d.status)}]\n   {l.url}")
    if not sections.top:
        lines.append("Подходящих сделок пока нет.")
    lines += ["", "Откройте HTML-версию письма: там фото, прогнозы и расчёт доставки."]
    return "\n".join(lines)
