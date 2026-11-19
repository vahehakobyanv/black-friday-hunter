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
    priority: int | None = None          # 1/2 from watchlist, 3.. from priority categories
    alert_reasons: list[str] = field(default_factory=list)

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


def deal_priority(category: str, watch: list[WatchItem], cfg: Config) -> int | None:
    tiers = [w.priority for w in watch]
    tiers += [tier for tier, cats in cfg.settings.priorities.categories.items() if category in cats]
    return min(tiers) if tiers else None


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
    # The same deal can arrive via two feeds of one site (e.g. "hot" and "new"):
    # keep one listing per URL so it is not counted as two offers.
    by_url: dict[str, Listing] = {}
    for l in listings:
        if l.url not in by_url or l.last_seen > by_url[l.url].last_seen:
            by_url[l.url] = l
    by_product: dict[str, list[Listing]] = {}
    for l in by_url.values():
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
            (w.min_price_usd is None or best.price_usd >= w.min_price_usd) and (
            (w.target_price_usd is not None and best.price_usd <= w.target_price_usd)
            or (w.target_delivered_usd is not None and best.delivery.total_usd is not None
                and best.delivery.total_usd <= w.target_delivered_usd))
            for w in watch)
        d = DealView(
            product=product, best=best, others=others, stats=stats, prediction=prediction, score=sb,
            discount=disc, label=classify(sb.total, s.scoring), status=status,
            stale=age_h > s.filters.stale_after_hours, age_hours=round(age_h, 1), explanation="",
            watch=[w.name for w in watch], watch_hit=watch_hit,
            record_low=bool(stats.prior_lowest is not None and bl.last_price < stats.prior_lowest * 0.999),
            in_this_run=any(o.listing.last_seen >= run_started for o in offers),
        )
        d.priority = deal_priority(product.category, watch, cfg)
        d.explanation = _explain(d, fx)
        deals.append(d)
    deals.sort(key=lambda d: (-d.total, d.best.delivered_usd))
    return deals


@dataclass
class PriorityTier:
    tier: int
    label: str
    groups: list[tuple[str, list[DealView]]]   # (watch item or category label, best deals)


@dataclass
class ParcelRow:
    deal: DealView
    value_eur: float
    duty_eur: float
    duty_free: bool
    max_duty_free_price: float | None      # in the listing currency
    currency: str


@dataclass
class ParcelPlan:
    limit_eur: float
    rows: list[ParcelRow]
    tips: list[str]


@dataclass
class ReportSections:
    top: list[DealView]
    opportunities: list[DealView]
    by_category: dict[str, list[DealView]]
    waiting: list[DealView]
    alerts: list[DealView]
    watchlist: list[tuple[WatchItem, DealView | None]]
    new_count: int
    priorities: list[PriorityTier] = field(default_factory=list)
    parcel: ParcelPlan | None = None
    ads: list["AdGroup"] = field(default_factory=list)


@dataclass
class AdGroup:
    """One store's Black Friday / Cyber Monday ad page and its best items."""
    store: str
    event: str            # black-friday / cyber-monday
    url: str
    items: list[tuple[DealView, OfferView]] = field(default_factory=list)

    @property
    def event_label(self) -> str:
        return "Cyber Monday" if self.event == "cyber-monday" else "Black Friday"


def build_ads(deals: list[DealView], cfg: Config, per_store: int = 3) -> list[AdGroup]:
    groups: list[AdGroup] = []
    index: dict[tuple[str, str], AdGroup] = {}
    for src in cfg.stores.sources:
        if src.type != "bf_ads" or not src.enabled or not src.base_url:
            continue
        for page in src.ad_pages:
            url = f"{src.base_url.rstrip('/')}/{page.event}/{page.slug}"
            g = AdGroup(store=page.store or page.slug, event=page.event, url=url)
            groups.append(g)
            index[(src.id, url)] = g
    if not groups:
        return []
    ad_sources = {src_id for src_id, _ in index}
    for d in deals:  # already sorted best first
        for o in [d.best, *d.others]:
            if o.listing.source not in ad_sources:
                continue
            g = index.get((o.listing.source, listing_extra(o.listing).get("ad_url", "")))
            if g is not None and len(g.items) < per_store and all(x is not d for x, _ in g.items):
                g.items.append((d, o))
    return groups


def build_sections(deals: list[DealView], cfg: Config, alerts: list[DealView] | None = None,
                   fx: FxTable | None = None) -> ReportSections:
    e = cfg.settings.email
    normal_min = cfg.settings.scoring.classes.get("NORMAL", 50)
    top = deals[: e.top_n]
    fresh = [d for d in deals if d.in_this_run and d.status in ("NEW", "PRICE DROP")]
    opportunities = fresh[: e.opportunities_n]
    by_cat: dict[str, list[DealView]] = {}
    for d in deals:
        lst = by_cat.setdefault(d.product.category, [])
        limit = e.focus_per_category if d.product.category in e.focus_categories else e.per_category_n
        if len(lst) < limit:
            lst.append(d)
    waiting = sorted((d for d in deals if d.prediction.advice in ("WAIT", "MAYBE WAIT") and d.total >= normal_min),
                     key=lambda d: (d.prediction.advice != "WAIT", -(d.prediction.expected_further_drop_pct or 0)))[: e.waiting_n]
    if alerts is None:
        alerts = [d for d in deals
                  if (d.total >= cfg.settings.scoring.classes.get("EXCELLENT", 90) or d.record_low or d.watch_hit)
                  and d.status in ("NEW", "PRICE DROP")][: e.alerts_n]
    wl: list[tuple[WatchItem, DealView | None]] = []
    for w in cfg.watchlist.watchlist:
        matches = [d for d in deals if w.name in d.watch]
        wl.append((w, min(matches, key=lambda d: d.best.price_usd) if matches else None))
    priorities = build_priorities(deals, cfg)
    parcel = build_parcel_plan(top, priorities, cfg, fx) if fx is not None else None
    return ReportSections(top, opportunities, by_cat, waiting, alerts, wl,
                          new_count=sum(1 for d in deals if d.status == "NEW"),
                          priorities=priorities, parcel=parcel, ads=build_ads(deals, cfg))


def build_priorities(deals: list[DealView], cfg: Config) -> list[PriorityTier]:
    p = cfg.settings.priorities
    fresh = [d for d in deals if not d.stale]
    tiers: dict[int, PriorityTier] = {}
    for w in sorted(cfg.watchlist.watchlist, key=lambda w: w.priority):
        tier = tiers.setdefault(w.priority, PriorityTier(w.priority, p.labels.get(w.priority, f"Priority {w.priority}"), []))
        matches = [d for d in fresh if w.name in d.watch][: p.per_item]   # deals are already sorted by score
        tier.groups.append((w.name, matches))
    cats = cfg.categories.categories
    for t, cat_keys in sorted(p.categories.items()):
        tier = tiers.setdefault(t, PriorityTier(t, p.labels.get(t, f"Priority {t}"), []))
        best = [d for d in fresh if d.product.category in cat_keys][: p.per_category_tier]
        tier.groups.append((" / ".join(cats[k].label for k in cat_keys if k in cats), best))
    return [tiers[k] for k in sorted(tiers)]


def select_alerts(deals: list[DealView], cfg: Config, repo: Repository) -> list[DealView]:
    """Deals worth an immediate alert, never repeated unless the price fell further."""
    a = cfg.settings.alerts
    if not a.enabled:
        return []
    picked: list[DealView] = []
    for d in deals:
        if d.stale or d.best.listing.expired:
            continue
        reasons = []
        if d.total >= a.min_score:
            reasons.append(f"{d.label} deal ({d.total:.0f}/100)")
        if a.watchlist_target_hit and d.watch_hit:
            reasons.append("your target price reached")
        if d.record_low and d.total >= a.record_low_min_score:
            reasons.append("record low in our history")
        if d.priority == 1 and d.watch and d.total >= a.priority1_min_score:
            reasons.append("priority item: " + ", ".join(d.watch))
        if not reasons:
            continue
        n = repo.notification(d.product.id)
        if n is not None and n.alerted_price_usd is not None and \
                d.best.price_usd > n.alerted_price_usd * (1 - a.realert_drop_pct / 100):
            continue  # already alerted at this (or a lower) price
        d.alert_reasons = reasons
        picked.append(d)
    picked.sort(key=lambda d: (d.priority or 9, -d.total))
    return picked[: a.max_per_message]


def build_parcel_plan(top: list[DealView], priorities: list[PriorityTier], cfg: Config,
                      fx: FxTable) -> ParcelPlan | None:
    """Customs helper for the €200 per-parcel duty-free limit."""
    from app.shipping.calculator import customs_duty_eur

    regime = cfg.shipping.customs.regime
    limit = regime.duty_free_value_eur
    chosen: list[DealView] = []
    seen: set[str] = set()
    for tier in priorities:
        if tier.tier > 2:
            continue
        for _name, lst in tier.groups:
            for d in lst[:2]:
                if d.product.id not in seen:
                    seen.add(d.product.id)
                    chosen.append(d)
    for d in top:
        if d.product.id not in seen:
            seen.add(d.product.id)
            chosen.append(d)
    rows: list[ParcelRow] = []
    for d in chosen[:14]:
        de = d.best.delivery
        if de.customs_value_eur is None:
            continue
        l = d.best.listing
        dom_eur = max(0.0, de.customs_value_eur - (fx.convert(d.best.price_usd, "USD", "EUR") or 0))
        max_price = fx.convert(limit - dom_eur, "EUR", l.currency)
        rows.append(ParcelRow(deal=d, value_eur=de.customs_value_eur, duty_eur=de.duty_eur or 0.0,
                              duty_free=(de.duty_eur or 0) == 0, max_duty_free_price=max_price, currency=l.currency))
    if not rows:
        return None
    tips: list[str] = []
    by_region: dict[str, list[ParcelRow]] = {}
    for r in rows:
        if r.duty_free:
            by_region.setdefault(r.deal.best.listing.region, []).append(r)
    for region, rs in by_region.items():
        if len(rs) < 2:
            continue
        a, b = rs[0], rs[1]
        combined = a.value_eur + b.value_eur
        if combined > limit:
            weight = (a.deal.best.delivery.weight_kg or 0) + (b.deal.best.delivery.weight_kg or 0)
            duty, _ = customs_duty_eur(combined, weight, cfg)
            tips.append(f"{region}: “{a.deal.best.listing.title[:40]}” + “{b.deal.best.listing.title[:40]}” in ONE parcel "
                        f"= €{combined:.0f} → duty ≈ €{duty:.0f}. Ask the forwarder to ship them as separate parcels "
                        f"and both stay duty-free.")
    tips.append(f"Duty-free limit: €{limit:g} and {regime.duty_free_weight_kg:g} kg per parcel; above it you pay "
                f"{regime.rate_on_excess_pct:g}% of the excess. The value counted is the product price plus shipping "
                f"to the forwarder. Confirm the per-parcel rule with your forwarder before ordering several items.")
    return ParcelPlan(limit_eur=limit, rows=rows, tips=tips)


def listing_extra(l: Listing) -> dict:
    try:
        return json.loads(l.extra_json or "{}")
    except json.JSONDecodeError:
        return {}
