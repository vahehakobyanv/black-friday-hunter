"""Statistics over the app's OWN recorded prices for one product + currency.

Nothing is ever back-filled or invented. An average is only reported when the
recorded history is long enough to make it meaningful:
  30-day avg  -> needs >= 7 days of history and >= `min_days` distinct days
  90-day avg  -> needs >= 30 days of history
  180-day avg -> needs >= 90 days of history
Otherwise the value is None and the report shows "unavailable".
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean

from app.models.schemas import HistoryStats


def daily_min(observations: list[tuple[datetime, float]]) -> dict[date, float]:
    days: dict[date, float] = defaultdict(lambda: float("inf"))
    for ts, price in observations:
        d = ts.date()
        days[d] = min(days[d], price)
    return dict(days)


def _window_avg(days: dict[date, float], today: date, n: int) -> float | None:
    vals = [p for d, p in days.items() if today - timedelta(days=n) < d <= today]
    return round(mean(vals), 2) if vals else None


def _trend_pct_per_week(days: dict[date, float], today: date) -> float | None:
    pts = sorted((d, p) for d, p in days.items() if today - timedelta(days=14) < d <= today)
    if len(pts) < 5:
        return None
    xs = [(d - pts[0][0]).days for d, _ in pts]
    ys = [p for _, p in pts]
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0 or my == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den  # price units per day
    return round(slope * 7 / my * 100, 2)


def compute_stats(observations: list[tuple[datetime, float]], currency: str, now: datetime,
                  min_days: int = 3, black_friday: date | None = None) -> HistoryStats:
    stats = HistoryStats(currency=currency, observations=len(observations))
    if not observations:
        return stats
    obs = sorted(observations)
    days = daily_min(obs)
    today = now.date()
    stats.distinct_days = len(days)
    stats.first_seen = obs[0][0]
    stats.span_days = round((now - obs[0][0]).total_seconds() / 86400, 1)
    low_ts, low = min(obs, key=lambda x: (x[1], x[0]))
    stats.lowest, stats.lowest_at = low, low_ts
    prior = [p for ts, p in obs if ts.date() < today]
    stats.prior_lowest = min(prior) if prior else None

    if stats.span_days >= 7 and stats.distinct_days >= min_days:
        stats.avg_30d = _window_avg(days, today, 30)
    if stats.span_days >= 30:
        stats.avg_90d = _window_avg(days, today, 90)
    if stats.span_days >= 90:
        stats.avg_180d = _window_avg(days, today, 180)
    stats.trend_pct_per_week = _trend_pct_per_week(days, today)

    if black_friday is not None:
        last_bf = black_friday.replace(year=black_friday.year - 1)
        window = [p for d, p in days.items() if last_bf - timedelta(days=3) <= d <= last_bf + timedelta(days=5)]
        stats.last_year_bf_price = min(window) if window else None
    return stats


def pct_below(price: float, reference: float | None) -> float | None:
    """Positive = price is below reference. 0.25 means 25 % cheaper."""
    if not reference or reference <= 0:
        return None
    return (reference - price) / reference
