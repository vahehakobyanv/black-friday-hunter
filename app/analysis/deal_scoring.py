"""Deterministic 0–100 deal score.

Default weights (config/settings.yaml → scoring.weights):
  30 history      price vs. OWN recorded history (or labelled proxy while history is young)
  20 discount     real discount, each reference weighted by how credible it is
  20 prediction   is now a good moment (little further drop expected)?
  20 delivered    extra cost of getting it to Yerevan (lower overhead = more points)
  10 reliability  shop reliability + availability + community/editor validation
A component with no data gets 0 points and a note — nothing is assumed.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.analysis.price_history import pct_below
from app.config import ScoringCfg
from app.models.schemas import (
    DeliveryEstimate,
    HistoryStats,
    Prediction,
    ReferenceKind,
    ScoreBreakdown,
)

SUSPICIOUS_LIST_DISCOUNT = 0.70


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class ScoreInput:
    price: float
    price_usd: float
    reference_price: float | None
    reference_kind: ReferenceKind
    stats: HistoryStats
    prediction: Prediction
    delivery: DeliveryEstimate | None
    retailer_reliability: float
    availability: str | None
    community_score: float | None
    staff_pick: bool
    confirmed_price: bool


@dataclass
class DiscountInfo:
    actual_pct: float | None        # best credible discount, 0.25 = 25 %
    basis: str                      # what it is measured against
    advertised_pct: float | None    # what the deal claims vs its reference
    suspicious: bool = False
    credible_pct: float | None = None  # actual_pct x credibility of its basis


def discount_info(inp: ScoreInput, cfg: ScoringCfg) -> DiscountInfo:
    cred = cfg.reference_credibility
    advertised = pct_below(inp.price, inp.reference_price)
    suspicious = (inp.reference_kind == ReferenceKind.LIST_PRICE and advertised is not None
                  and advertised > SUSPICIOUS_LIST_DISCOUNT)
    candidates: list[tuple[float, float, str]] = []  # (credible value, raw value, basis)
    avg = inp.stats.avg_90d or inp.stats.avg_30d
    if avg:
        d = pct_below(inp.price, avg)
        if d is not None:
            label = "90-day average" if inp.stats.avg_90d else "30-day average"
            candidates.append((d * cred.get("own_history", 1.0), d, f"own {label}"))
    if advertised is not None and inp.reference_kind != ReferenceKind.NONE and not suspicious:
        c = cred.get(inp.reference_kind.value, 0.5)
        basis = {
            ReferenceKind.NEXT_BEST_PRICE: "price at the next-cheapest shop",
            ReferenceKind.REGULAR_PRICE: "retailer's regular price",
            ReferenceKind.LIST_PRICE: "advertised list price",
            ReferenceKind.OWN_HISTORY: "own history",
        }.get(inp.reference_kind, inp.reference_kind.value)
        candidates.append((advertised * c, advertised, basis))
    if not candidates:
        return DiscountInfo(None, "no reference price", advertised, suspicious)
    best = max(candidates, key=lambda c: c[0])
    return DiscountInfo(best[1], best[2], advertised, suspicious, credible_pct=best[0])


def score_deal(inp: ScoreInput, cfg: ScoringCfg) -> tuple[ScoreBreakdown, DiscountInfo]:
    w = cfg.weights
    sb = ScoreBreakdown()
    disc = discount_info(inp, cfg)

    # ---------------- history (30)
    st = inp.stats
    if st.available:
        ref_avg = st.avg_90d or st.avg_30d
        below = pct_below(inp.price, ref_avg) or 0.0
        pts = _clamp(below / 0.20) * w["history"] * 0.73
        if st.prior_lowest is not None and inp.price < st.prior_lowest * 0.999:
            pts += w["history"] * 0.27
            sb.notes.append("record low in own history")
        elif st.prior_lowest is not None and inp.price <= st.prior_lowest * 1.03:
            pts += w["history"] * 0.13
            sb.notes.append("within 3% of lowest recorded")
        sb.history = round(pts, 1)
    else:
        cap = w["history"] * cfg.history_proxy_max_share
        pts = 0.0
        if inp.reference_kind == ReferenceKind.NEXT_BEST_PRICE and inp.reference_price:
            pts += _clamp((pct_below(inp.price, inp.reference_price) or 0) / 0.25) * cap * 0.6
        if inp.community_score is not None and inp.community_score > 0:
            pts += _clamp(inp.community_score / 500) * cap * 0.4
        if inp.staff_pick:
            pts += cap * 0.2
        sb.history = round(min(pts, cap), 1)
        sb.notes.append("own price history too short — history score uses market proxy (max "
                        f"{cap:g}/{w['history']:g})")

    # ---------------- discount (20)
    if disc.credible_pct is not None:
        sb.discount = round(_clamp(disc.credible_pct / 0.40) * w["discount"], 1)
    else:
        sb.notes.append("no reference price — discount unverifiable")
    if disc.suspicious:
        sb.notes.append("advertised discount looks inflated (>70% off list price) — ignored")

    # ---------------- prediction (20)
    p = inp.prediction
    if p.expected_further_drop_pct is None or p.advice == "NO DATA":
        sb.notes.append("no price prediction available")
    else:
        sb.prediction = round(_clamp(1 - p.expected_further_drop_pct / 15) * w["prediction"], 1)

    # ---------------- delivered cost (20)
    d = inp.delivery
    if d is not None and d.total_usd and inp.price_usd > 0:
        overhead = (d.total_usd - inp.price_usd) / inp.price_usd
        pts = _clamp(1 - (overhead - 0.05) / 0.45) * w["delivered_cost"]
        if not d.complete:
            pts *= 0.4
            sb.notes.append("delivery cost incomplete — delivered-cost score reduced")
        sb.delivered_cost = round(pts, 1)
    else:
        sb.notes.append("delivery cost unavailable")

    # ---------------- reliability (10)
    rel = _clamp(inp.retailer_reliability) * 0.6
    avail = {"in_stock": 0.2, "unknown": 0.1}.get(inp.availability or "unknown", 0.0)
    validation = 0.0
    if inp.confirmed_price:
        validation = 0.2
    elif inp.staff_pick:
        validation = 0.2
    elif inp.community_score is not None:
        validation = 0.2 if inp.community_score >= 100 else 0.1 if inp.community_score >= 30 else 0.0
    sb.reliability = round((rel + avail + validation) * w["reliability"], 1)
    return sb, disc


def classify(score: float, cfg: ScoringCfg) -> str:
    for label, threshold in sorted(cfg.classes.items(), key=lambda kv: -kv[1]):
        if score >= threshold:
            return label
    return "NOT INTERESTING"
