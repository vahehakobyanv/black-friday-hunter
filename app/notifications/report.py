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
SOURCE_LABELS = {
    "mydealz_hot": "mydealz.de (hot)", "mydealz_new": "mydealz.de (new)",
    "hotukdeals_hot": "hotukdeals (hot)", "hotukdeals_new": "hotukdeals (new)",
    "dealnews": "dealnews.com", "bestbuy": "Best Buy API", "ebay": "eBay API",
    "watchlist_pages": "Watchlist product pages", "exchange_rates": "Exchange rates",
}
REF_LABELS = {
    "next_best_price": "next-cheapest shop", "regular_price": "regular price", "list_price": "list price (claimed)",
    "own_history": "own history", "none": "none",
}


@dataclass
class RenderedReport:
    subject: str
    html: str
    text: str


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "unavailable"
    sym = SYMBOLS.get(currency.upper())
    if currency.upper() == "AMD":
        return f"{amount:,.0f} ֏"
    value = f"{amount:,.2f}" if amount < 1000 else f"{amount:,.0f}"
    return f"{sym}{value}" if sym else f"{value} {currency}"


def opt_money(amount: float | None, currency: str) -> str:
    return money(amount, currency) if amount is not None else "n/a"


def countdown(today, cfg: Config) -> str:
    bf, cm = cfg.settings.app.black_friday, cfg.settings.app.cyber_monday
    if today < bf:
        n = (bf - today).days
        return f"{n} day{'s' if n != 1 else ''} to Black Friday ({bf:%d %b})"
    if today == bf:
        return "BLACK FRIDAY TODAY"
    if today < cm:
        n = (cm - today).days
        return f"Cyber Monday in {n} day{'s' if n != 1 else ''}"
    if today == cm:
        return "CYBER MONDAY TODAY"
    return "Post-Cyber-Monday tracking"


def build_banners(cfg: Config, fx: FxTable, statuses: list[CollectorStatus], deals: list[DealView]) -> list[str]:
    banners = []
    regions = {d.best.listing.region for d in deals}
    for fw in cfg.shipping.forwarders:
        if fw.rate_per_kg is None and fw.origin in regions:
            banners.append(f"{fw.name}: shipping rate per kg is not configured — delivered prices from "
                           f"{fw.origin} are incomplete. Fill it in config/shipping_rates.yaml.")
    if fx.stale:
        banners.append(f"Exchange rates could not be refreshed; using cached rates from {fx.rate_date}.")
    failed = [s for s in statuses if not s.ok]
    if failed:
        banners.append("Sources failed this run: " + ", ".join(SOURCE_LABELS.get(s.source, s.source) for s in failed)
                       + ". Their latest data (up to the freshness limit) is still shown.")
    if statuses and not any(s.ok and s.offers for s in statuses):
        banners.append("No source returned fresh offers this run — everything below is from earlier runs (see STALE badges).")
    return banners


def render_report(*, sections: ReportSections, deals: list[DealView], statuses: list[CollectorStatus], fx: FxTable,
                  cfg: Config, now: datetime, run_id: int, last_email: datetime | None, offers_count: int) -> RenderedReport:
    tz = ZoneInfo(cfg.settings.app.timezone)
    local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)

    def local_time(dt: datetime) -> str:
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d %b %H:%M")

    cats = cfg.categories.categories
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(
        money=money, opt_money=opt_money, amd=lambda v: money(v, "AMD") if v is not None else "unavailable",
        local_time=local_time, source_label=lambda s: SOURCE_LABELS.get(s, s),
        category_label=lambda k: cats[k].label if k in cats else k.title(),
        ref_label=lambda k: REF_LABELS.get(k, k),
    )
    n = len(sections.top)
    subject = f"{cfg.settings.app.name} | Top {n} Deals | {local_now.day} {local_now:%B} | {local_now:%H}:00"
    if sections.alerts:
        subject += f" | {len(sections.alerts)} alert{'s' if len(sections.alerts) != 1 else ''}"
    elif not sections.opportunities:
        subject += " | no new deals"
    regime = cfg.shipping.customs.regime
    html = env.get_template("report.html.j2").render(
        subject=subject,
        headline=(f"{len(sections.alerts)} alert(s) · top deal {sections.top[0].total:.0f}/100" if sections.top and sections.alerts
                  else f"Top deal {sections.top[0].total:.0f}/100" if sections.top else "No qualifying deals right now"),
        generated_local=local_now.strftime("%d %b %Y, %H:%M") + " Yerevan",
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
        customs_label=(f"duty-free up to €{regime.duty_free_value_eur:g} / {regime.duty_free_weight_kg:g} kg, "
                       f"{regime.rate_on_excess_pct:g}% above (verified {cfg.shipping.customs.verified_on})"),
        run_id=run_id,
        last_email=local_time(last_email) if last_email else None,
    )
    return RenderedReport(subject=subject, html=html, text=render_text(sections, subject))


def _ordered_category_labels(cfg: Config) -> dict[str, str]:
    """Focus categories first (marked ★), then the rest in config order."""
    cats = cfg.categories.categories
    focus = [k for k in cfg.settings.email.focus_categories if k in cats]
    out = {k: "★ " + cats[k].label for k in focus}
    out.update({k: c.label for k, c in cats.items() if k not in out})
    return out


def render_text(sections: ReportSections, subject: str) -> str:
    lines = [subject, "", "TOP DEALS"]
    for i, d in enumerate(sections.top, 1):
        l = d.best.listing
        approx = "≈" if d.best.delivery.complete else "≥"
        delivered = f" | delivered {approx} {money(d.best.delivery.total_usd, 'USD')}" if d.best.delivery.total_usd else ""
        lines.append(f"{i}. [{d.total:.0f} {d.label}] {l.title} — {money(l.last_price, l.currency)} at {l.retailer}"
                     f"{delivered} [{d.status}]\n   {l.url}")
    if not sections.top:
        lines.append("No qualifying deals right now.")
    lines += ["", "Open the HTML version for images, predictions and delivery breakdowns."]
    return "\n".join(lines)
