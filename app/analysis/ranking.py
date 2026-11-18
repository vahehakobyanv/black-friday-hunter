"""Turn active listings into ranked, de-duplicated deals and report sections."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.analysis.deal_scoring import DiscountInfo, ScoreInput, classify, score_deal
from app.analysis.predictions import predict
from app.analysis.price_history import compute_stats
from app.config import Config, WatchItem
from app.database.models import Listing, Product
from app.database.repository import Repository
from app.models.schemas import (
    DeliveryEstimate,
    HistoryStats,
    Prediction,
    PriceKind,
    ReferenceKind,
    ScoreBreakdown,
)
from app.shipping.calculator import ShipInput, estimate_delivery
from app.shipping.currency import FxTable
from app.utils.text import contains_phrase, fold

PRICE_DROP_RENOTIFY = 0.03


@dataclass
class OfferView:
    listing: Listing
    price_usd: float
    delivery: DeliveryEstimate
    reliability: float

    @property
    def delivered_usd(self) -> float:
        return self.delivery.total_usd if self.delivery.total_usd is not None else float("inf")


@dataclass
class DealView:
    product: Product
    best: OfferView
    others: list[OfferView]
    stats: HistoryStats
    prediction: Prediction
    score: ScoreBreakdown
    discount: DiscountInfo
    label: str
    status: str                     # NEW / PRICE DROP / SEEN BEFORE
    stale: bool
    age_hours: float
    explanation: str
    watch: list[str] = field(default_factory=list)
    watch_hit: bool = False
    record_low: bool = False
    in_this_run: bool = False

    @property
    def total(self) -> float:
        return self.score.total


def retailer_reliability(name: str, cfg: Config) -> float:
    r = cfg.stores.retailers.get(name)
    if r is not None:
        return r.reliability
    for canonical, rc in cfg.stores.retailers.items():
        if fold(canonical) == fold(name):
            return rc.reliability
    return cfg.stores.default_reliability


def watch_matches(title: str, cfg: Config) -> list[WatchItem]:
    out = []
    for w in cfg.watchlist.watchlist:
        if any(contains_phrase(title, k) for k in w.keywords) and not any(contains_phrase(title, x) for x in w.exclude):
            out.append(w)
    return out


def _explain(d: DealView, fx: FxTable) -> str:
    parts = []
    l = d.best.listing
    if d.record_low:
        parts.append("lowest price we have recorded")
    if d.discount.actual_pct is not None and d.discount.actual_pct > 0.02:
        parts.append(f"{d.discount.actual_pct * 100:.0f}% below {d.discount.basis}")
    if l.community_score is not None and l.community_score >= 100:
        parts.append(f"{l.community_score:.0f}° community heat on {l.source.split('_')[0]}")
    if l.staff_pick:
        parts.append("dealnews editors' pick")
    if d.others:
        cheaper_than = len([o for o in d.others if o.delivered_usd > d.best.delivered_usd])
        if cheaper_than:
            parts.append(f"cheapest of {len(d.others) + 1} offers compared")
    if d.best.delivery.total_usd is not None and d.best.price_usd:
        overhead = (d.best.delivery.total_usd - d.best.price_usd) / d.best.price_usd * 100
        if d.best.delivery.complete:
            parts.append(f"≈${d.best.delivery.total_usd:,.0f} delivered to Yerevan (+{overhead:.0f}%)")
        else:
            parts.append(f"≥${d.best.delivery.total_usd:,.0f} delivered to Yerevan (some costs unknown)")
    if d.prediction.advice in ("WAIT", "MAYBE WAIT") and d.prediction.black_friday_price:
        parts.append(f"may drop ~{d.prediction.expected_further_drop_pct:.0f}% by Black Friday "
                     f"({d.prediction.confidence} confidence)")
    if d.watch:
        parts.append("on your watchlist: " + ", ".join(d.watch))
    return "; ".join(parts) or "limited data"


def build_deals(repo: Repository, cfg: Config, fx: FxTable, now: datetime, run_started: datetime) -> list[DealView]:
    s = cfg.settings
    since = now - timedelta(hours=s.filters.offer_ttl_hours)
    listings = repo.active_listings(since)
    by_product: dict[str, list[Listing]] = {}
    for l in listings:
        by_product.setdefault(l.product_id, []).append(l)

    deals: list[DealView] = []
    for pid, group in by_product.items():
        product = repo.s.get(Product, pid)
        if product is None or product.category in s.filters.exclude_categories:
            continue
        offers: list[OfferView] = []
        for l in group:
            p_usd = fx.to_usd(l.last_price, l.currency)
            if p_usd is None or p_usd < s.filters.min_price_usd:
                continue
            if l.community_score is not None and l.community_score < s.filters.min_pepper_temperature:
                continue
            delivery = estimate_delivery(ShipInput(
                price=l.last_price, currency=l.currency, region=l.region, retailer=l.retailer,
                category=product.category, free_shipping=l.free_shipping, shipping_cost=l.shipping_cost,
                weight_kg=l.weight_kg, price_kind=PriceKind(l.price_kind)), fx, cfg)
            offers.append(OfferView(l, p_usd, delivery, retailer_reliability(l.retailer, cfg)))
        if not offers:
            continue
        # cheapest TRUSTWORTHY delivered offer; complete estimates beat incomplete ones
        trusted = [o for o in offers if o.reliability >= 0.5] or offers
        best = min(trusted, key=lambda o: (not o.delivery.complete, o.delivered_usd, o.price_usd))
        others = sorted((o for o in offers if o is not best), key=lambda o: o.delivered_usd)
        bl = best.listing

        obs = [(o.observed_at, o.price) for o in repo.observations(pid, bl.currency)]
        stats = compute_stats(obs, bl.currency, now, s.history.min_days_for_stats, s.app.black_friday)
        adv_disc = None
        if bl.last_reference_price and bl.last_reference_price > bl.last_price:
            adv_disc = (bl.last_reference_price - bl.last_price) / bl.last_reference_price
        local_today = now.date()
        prediction = predict(bl.last_price, bl.currency, stats, cfg.categories.categories.get(product.category),
                             local_today, s.app.black_friday, s.app.cyber_monday, adv_disc)
        sb, disc = score_deal(ScoreInput(
            price=bl.last_price, price_usd=best.price_usd, reference_price=bl.last_reference_price,
            reference_kind=ReferenceKind(bl.reference_kind), stats=stats, prediction=prediction,
            delivery=best.delivery, retailer_reliability=best.reliability, availability=bl.availability,
            community_score=bl.community_score, staff_pick=bl.staff_pick,
            confirmed_price=bl.price_kind == PriceKind.CONFIRMED.value), s.scoring)

        notif = repo.notification(pid)
        if notif is None:
            status = "NEW"
        elif best.price_usd < notif.best_price_usd_sent * (1 - PRICE_DROP_RENOTIFY):
            status = "PRICE DROP"
        else:
            status = "SEEN BEFORE"
        age_h = (now - bl.last_seen).total_seconds() / 3600
        watch = watch_matches(product.canonical_name + " " + bl.title, cfg)
        watch_hit = any(
            (w.target_price_usd is not None and best.price_usd <= w.target_price_usd)
            or (w.target_delivered_usd is not None and best.delivery.total_usd is not None
                and best.delivery.total_usd <= w.target_delivered_usd)
            for w in watch)
        d = DealView(
            product=product, best=best, others=others, stats=stats, prediction=prediction, score=sb,
            discount=disc, label=classify(sb.total, s.scoring), status=status,
            stale=age_h > s.filters.stale_after_hours, age_hours=round(age_h, 1), explanation="",
            watch=[w.name for w in watch], watch_hit=watch_hit,
            record_low=bool(stats.prior_lowest is not None and bl.last_price < stats.prior_lowest * 0.999),
            in_this_run=any(o.listing.last_seen >= run_started for o in offers),
        )
        d.explanation = _explain(d, fx)
        deals.append(d)
    deals.sort(key=lambda d: (-d.total, d.best.delivered_usd))
    return deals


@dataclass
class ReportSections:
    top: list[DealView]
    opportunities: list[DealView]
    by_category: dict[str, list[DealView]]
    waiting: list[DealView]
    alerts: list[DealView]
    watchlist: list[tuple[WatchItem, DealView | None]]
    new_count: int


def build_sections(deals: list[DealView], cfg: Config) -> ReportSections:
    e = cfg.settings.email
    normal_min = cfg.settings.scoring.classes.get("NORMAL", 50)
    top = deals[: e.top_n]
    fresh = [d for d in deals if d.in_this_run and d.status in ("NEW", "PRICE DROP")]
    opportunities = fresh[: e.opportunities_n]
    by_cat: dict[str, list[DealView]] = {}
    for d in deals:
        lst = by_cat.setdefault(d.product.category, [])
        if len(lst) < e.per_category_n:
            lst.append(d)
    waiting = sorted((d for d in deals if d.prediction.advice in ("WAIT", "MAYBE WAIT") and d.total >= normal_min),
                     key=lambda d: (d.prediction.advice != "WAIT", -(d.prediction.expected_further_drop_pct or 0)))[: e.waiting_n]
    alerts = [d for d in deals
              if (d.total >= cfg.settings.scoring.classes.get("EXCELLENT", 90) or d.record_low or d.watch_hit)
              and d.status in ("NEW", "PRICE DROP")][: e.alerts_n]
    wl: list[tuple[WatchItem, DealView | None]] = []
    for w in cfg.watchlist.watchlist:
        matches = [d for d in deals if w.name in d.watch]
        wl.append((w, min(matches, key=lambda d: d.best.price_usd) if matches else None))
    return ReportSections(top, opportunities, by_cat, waiting, alerts, wl,
                          new_count=sum(1 for d in deals if d.status == "NEW"))


def listing_extra(l: Listing) -> dict:
    try:
        return json.loads(l.extra_json or "{}")
    except json.JSONDecodeError:
        return {}
