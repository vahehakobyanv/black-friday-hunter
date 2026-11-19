"""Pydantic data models shared by collectors, analysis and reporting."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


class PriceKind(str, Enum):
    """How trustworthy a price figure is. Shown in the email next to prices."""

    CONFIRMED = "confirmed"      # from an official API or the product page itself
    ADVERTISED = "advertised"    # from a deal post / feed (community or editorial)
    ESTIMATED = "estimated"      # computed from assumptions (e.g. delivered cost)
    PREDICTED = "predicted"      # forecast (Black Friday / Cyber Monday)
    UNVERIFIED = "unverified"    # could not be checked


class ReferenceKind(str, Enum):
    """What the 'previous price' actually is."""

    OWN_HISTORY = "own_history"
    NEXT_BEST_PRICE = "next_best_price"   # same item at another shop (Pepper)
    REGULAR_PRICE = "regular_price"       # retailer's own regular price via API
    LIST_PRICE = "list_price"             # "down from"/MSRP claim
    NONE = "none"


class RawOffer(BaseModel):
    """One offer exactly as a collector saw it, already validated."""

    source: str
    source_item_id: str
    title: str
    url: str
    retailer: str
    region: str                      # US / DE / UK / ...
    currency: str                    # ISO 4217
    price: float = Field(gt=0)
    reference_price: float | None = None
    reference_kind: ReferenceKind = ReferenceKind.NONE
    price_kind: PriceKind = PriceKind.ADVERTISED
    image_url: str | None = None
    brand: str | None = None
    model: str | None = None
    gtin: str | None = None
    mpn: str | None = None
    feed_category: str | None = None
    category_hint: str | None = None  # from the feed it came from (e.g. a "kitchen" feed)
    availability: str | None = None  # in_stock / out_of_stock / unknown
    shipping_cost: float | None = None
    free_shipping: bool | None = None
    weight_kg: float | None = None
    dimensions_cm: tuple[float, float, float] | None = None
    community_score: float | None = None   # Pepper temperature
    staff_pick: bool = False
    expired: bool = False
    voucher_code: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    observed_at: datetime
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3:
            raise ValueError(f"bad currency {v!r}")
        return v

    @field_validator("reference_price")
    @classmethod
    def _ref_positive(cls, v: float | None) -> float | None:
        return v if v and v > 0 else None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        HttpUrl(v)  # validation only
        return v


class CollectorStatus(BaseModel):
    source: str
    ok: bool
    offers: int = 0
    skipped: int = 0
    error: str | None = None
    duration_s: float = 0
    note: str | None = None


class HistoryStats(BaseModel):
    """Own observed price history for ONE product in ONE currency."""

    currency: str
    observations: int = 0
    distinct_days: int = 0
    first_seen: datetime | None = None
    lowest: float | None = None
    lowest_at: datetime | None = None
    prior_lowest: float | None = None        # lowest BEFORE today (for record-low detection)
    span_days: float = 0
    avg_30d: float | None = None
    avg_90d: float | None = None
    avg_180d: float | None = None
    trend_pct_per_week: float | None = None
    last_year_bf_price: float | None = None

    @property
    def available(self) -> bool:
        return self.avg_30d is not None


class Prediction(BaseModel):
    black_friday_price: float | None = None
    cyber_monday_price: float | None = None
    currency: str
    confidence: str = "none"         # none / low / medium / high
    method: str = ""
    advice: str = ""                 # BUY NOW / WAIT / NO DATA
    expected_further_drop_pct: float | None = None


class CostLine(BaseModel):
    label: str
    amount_usd: float | None
    kind: PriceKind = PriceKind.ESTIMATED
    note: str = ""


class DeliveryEstimate(BaseModel):
    route: str
    lines: list[CostLine]
    total_usd: float | None
    total_amd: float | None
    complete: bool                   # False if any part is unknown
    weight_kg: float | None
    weight_is_estimate: bool
    warnings: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    history: float = 0
    discount: float = 0
    prediction: float = 0
    delivered_cost: float = 0
    reliability: float = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return round(self.history + self.discount + self.prediction + self.delivered_cost + self.reliability, 1)
