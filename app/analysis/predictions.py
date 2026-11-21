"""Black Friday / Cyber Monday price prediction.

Deterministic and transparent. Confidence reflects how much REAL data backs it:
  high   - we recorded this product's price around last year's Black Friday
  medium - >= 14 days of own history with a measurable trend
  low    - only the category heuristic from categories.yaml (an assumption)
  none   - nothing to base a prediction on
"""
from __future__ import annotations

from datetime import date

from app.config import CategoryCfg
from app.models.schemas import HistoryStats, Prediction

WAIT_THRESHOLD_PCT = 5.0


def predict(price: float, currency: str, stats: HistoryStats, cat: CategoryCfg | None, today: date,
            black_friday: date, cyber_monday: date, advertised_discount: float | None = None) -> Prediction:
    pred = Prediction(currency=currency)
    bf_drop = (cat.bf_extra_drop_pct if cat else 0) / 100
    cm_drop = (cat.cm_extra_drop_pct if cat else 0) / 100

    if today > cyber_monday:
        # Season is over; next prediction needs next year's data.
        pred.method = "после Cyber Monday — следующий прогноз по истории нового сезона"
        pred.confidence = "none"
        pred.advice = "NO DATA"
        return pred

    if black_friday <= today <= cyber_monday:
        # On Black Friday itself the current price IS the Black Friday price.
        pred.black_friday_price = round(price, 2) if today == black_friday else None
        pred.cyber_monday_price = round(price * (1 - cm_drop), 2) if today < cyber_monday else round(price, 2)
        pred.confidence = "medium" if today == cyber_monday else "low"
        pred.method = "неделя Black Friday: текущая цена и есть цена распродажи"
        pred.expected_further_drop_pct = round((price - pred.cyber_monday_price) / price * 100, 1)
        pred.advice = "BUY NOW" if pred.expected_further_drop_pct < WAIT_THRESHOLD_PCT else "WAIT"
        return pred

    # Before Black Friday.
    weeks_left = max((black_friday - today).days, 0) / 7
    # Deals already discounted a lot have less room left.
    if advertised_discount is not None and advertised_discount >= 0.20:
        bf_drop /= 2
    heuristic = price * (1 - bf_drop)
    method, confidence = "типичное поведение категории (допущение, не данные)", "low"
    estimate = heuristic

    if stats.trend_pct_per_week is not None and stats.distinct_days >= 14:
        trend_est = price * (1 + stats.trend_pct_per_week / 100 * weeks_left)
        trend_est = min(max(trend_est, price * 0.7), price)
        estimate = min(heuristic, trend_est)
        method, confidence = f"наш тренд за 14 дней ({stats.trend_pct_per_week:+.1f}%/нед.) + типичное поведение", "medium"

    if stats.last_year_bf_price is not None:
        estimate = min(estimate, stats.last_year_bf_price) if stats.last_year_bf_price < price else estimate
        method, confidence = "цена прошлой Black Friday (наша история)", "high"

    pred.black_friday_price = round(estimate, 2)
    pred.cyber_monday_price = round(estimate * (1 - cm_drop), 2)
    pred.method = method
    pred.confidence = confidence
    pred.expected_further_drop_pct = round(max(0.0, (price - estimate) / price * 100), 1)
    if pred.expected_further_drop_pct < WAIT_THRESHOLD_PCT:
        pred.advice = "BUY NOW"
    else:
        # A heuristic alone is not strong enough to tell you to wait.
        pred.advice = "WAIT" if confidence in ("medium", "high") else "MAYBE WAIT"
    return pred
