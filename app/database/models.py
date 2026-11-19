"""SQLAlchemy ORM models (SQLite)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Product(Base):
    """A product model + configuration (e.g. 'Samsung 990 PRO 2TB'). Offers from
    different shops for the same product point to the same row."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), index=True)
    specs_json: Mapped[str] = mapped_column(Text, default="{}")
    match_tokens: Mapped[str] = mapped_column(Text, default="")
    gtin: Mapped[str | None] = mapped_column(String(20), index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime, index=True)

    listings: Mapped[list["Listing"]] = relationship(back_populates="product")


class Listing(Base):
    """One offer (shop/source + URL) for a product."""

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_item_id", name="uq_listing_source_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    source_item_id: Mapped[str] = mapped_column(String(300))
    retailer: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3))
    last_price: Mapped[float] = mapped_column(Float)
    last_reference_price: Mapped[float | None] = mapped_column(Float)
    reference_kind: Mapped[str] = mapped_column(String(30), default="none")
    price_kind: Mapped[str] = mapped_column(String(20), default="advertised")
    availability: Mapped[str | None] = mapped_column(String(30))
    free_shipping: Mapped[bool | None] = mapped_column(Boolean)
    shipping_cost: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    community_score: Mapped[float | None] = mapped_column(Float)
    staff_pick: Mapped[bool] = mapped_column(Boolean, default=False)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
    voucher_code: Mapped[str | None] = mapped_column(String(80))
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime, index=True)
    extra_json: Mapped[str] = mapped_column(Text, default="{}")

    product: Mapped[Product] = relationship(back_populates="listings")


class PriceObservation(Base):
    """A price seen at a point in time. Never invented: one row = one real sighting."""

    __tablename__ = "price_observations"
    __table_args__ = (Index("ix_obs_product_time", "product_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    price_usd: Mapped[float | None] = mapped_column(Float)
    reference_price: Mapped[float | None] = mapped_column(Float)
    price_kind: Mapped[str] = mapped_column(String(20))


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency", name="uq_fx_date_cur"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rate_date: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str] = mapped_column(String(3))
    amd_per_unit: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    """What was already reported, so repeats are never presented as new."""

    __tablename__ = "notifications"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_sent_at: Mapped[datetime] = mapped_column(DateTime)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime)
    best_price_usd_sent: Mapped[float] = mapped_column(Float)
    times_sent: Mapped[int] = mapped_column(Integer, default=1)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    alerted_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EnrichmentCache(Base):
    __tablename__ = "enrichment_cache"

    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    data_json: Mapped[str] = mapped_column(Text)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")
    collectors_json: Mapped[str] = mapped_column(Text, default="[]")
    offers_seen: Mapped[int] = mapped_column(Integer, default=0)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
