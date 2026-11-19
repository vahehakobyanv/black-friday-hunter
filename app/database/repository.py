"""Database access. All SQL lives here."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.analysis.product_matching import (
    ProductSpecs,
    extract_specs,
    gtin_normalize,
    product_id_for,
    same_product,
)
from app.database.models import (
    Base,
    EnrichmentCache,
    FxRate,
    Listing,
    Notification,
    PriceObservation,
    Product,
    Run,
)
from app.models.schemas import RawOffer

log = logging.getLogger(__name__)


def make_engine(path: Path | str) -> Engine:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=DELETE")  # single file -> safe to commit to git
        cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns that newer code expects to
    tables created by older versions (the database lives on across releases)."""
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for col in table.columns:
                if col.name not in existing:
                    coltype = col.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}")
                    log.info("database migrated: added %s.%s", table.name, col.name)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


class Repository:
    def __init__(self, session: Session):
        self.s = session

    # ------------------------------------------------------------------ products
    def find_or_create_product(self, offer: RawOffer, category: str, now: datetime) -> tuple[Product, ProductSpecs, str]:
        """Return (product, specs, how_matched)."""
        specs = extract_specs(offer.title, offer.brand)
        gtin = gtin_normalize(offer.gtin)
        # 0. this exact listing already known -> keep its product (stable history)
        existing_listing = self.s.scalar(
            select(Listing).where(Listing.source == offer.source, Listing.source_item_id == offer.source_item_id)
        )
        if existing_listing is not None:
            product = self.s.get(Product, existing_listing.product_id)
            # Re-verify every run, so improved matching rules repair old wrong merges.
            if product is not None and (
                product.canonical_name == specs.clean_title
                or same_product(specs, extract_specs(product.canonical_name, product.brand)).same
            ):
                product.last_seen = now
                return product, specs, "known listing"
        # 1. GTIN
        if gtin:
            product = self.s.scalar(select(Product).where(Product.gtin == gtin))
            if product is not None:
                product.last_seen = now
                return product, specs, "gtin"
        # 2. spec match against same-brand candidates
        if specs.brand:
            cutoff = now - timedelta(days=200)
            candidates = self.s.scalars(
                select(Product).where(Product.brand == specs.brand, Product.last_seen >= cutoff)
            ).all()
            for cand in candidates:
                cand_specs = extract_specs(cand.canonical_name, cand.brand)
                if same_product(specs, cand_specs).same:
                    cand.last_seen = now
                    if gtin and not cand.gtin:
                        cand.gtin = gtin
                    return cand, specs, "specs"
        # 3. new product
        pid = product_id_for(specs, gtin)
        product = self.s.get(Product, pid)
        if product is None:
            product = Product(
                id=pid,
                canonical_name=specs.clean_title or offer.title,
                brand=specs.brand,
                model=specs.model_label or None,
                category=category,
                specs_json=json.dumps(specs.as_dict()),
                match_tokens=" ".join(sorted(specs.model_tokens)),
                gtin=gtin,
                first_seen=now,
                last_seen=now,
            )
            self.s.add(product)
            self.s.flush()
            return product, specs, "new"
        product.last_seen = now
        return product, specs, "same id"

    # ------------------------------------------------------------------ listings + observations
    def upsert_listing(self, product: Product, offer: RawOffer, now: datetime, price_usd: float | None,
                       unchanged_every_hours: int) -> tuple[Listing, bool]:
        """Insert/update listing; add a price observation if the price changed or
        the last observation is older than `unchanged_every_hours`.
        Returns (listing, price_changed)."""
        listing = self.s.scalar(
            select(Listing).where(Listing.source == offer.source, Listing.source_item_id == offer.source_item_id)
        )
        changed = False
        if listing is None:
            listing = Listing(
                product_id=product.id, source=offer.source, source_item_id=offer.source_item_id,
                first_seen=now, last_price=offer.price, currency=offer.currency,
                retailer=offer.retailer, region=offer.region, title=offer.title, url=offer.url, last_seen=now,
            )
            self.s.add(listing)
            changed = True
        else:
            changed = abs(listing.last_price - offer.price) > 0.005 or listing.currency != offer.currency
        if listing.product_id is not None and listing.product_id != product.id:
            # listing was re-matched: move its price history to the right product
            self.s.execute(update(PriceObservation).where(PriceObservation.listing_id == listing.id)
                           .values(product_id=product.id))
        listing.product_id = product.id
        listing.retailer = offer.retailer
        listing.region = offer.region
        listing.title = offer.title
        listing.url = offer.url
        listing.image_url = offer.image_url or listing.image_url
        listing.currency = offer.currency
        listing.last_price = offer.price
        listing.last_reference_price = offer.reference_price
        listing.reference_kind = offer.reference_kind.value
        listing.price_kind = offer.price_kind.value
        listing.availability = offer.availability
        listing.free_shipping = offer.free_shipping
        listing.shipping_cost = offer.shipping_cost
        listing.weight_kg = offer.weight_kg or listing.weight_kg
        listing.community_score = offer.community_score
        listing.staff_pick = offer.staff_pick
        listing.expired = offer.expired
        listing.voucher_code = offer.voucher_code
        listing.last_seen = now
        listing.extra_json = json.dumps(offer.extra, default=str)
        self.s.flush()

        last_obs = self.s.scalar(
            select(PriceObservation).where(PriceObservation.listing_id == listing.id)
            .order_by(PriceObservation.observed_at.desc()).limit(1)
        )
        due = last_obs is None or changed or (now - last_obs.observed_at) >= timedelta(hours=unchanged_every_hours)
        if due and not offer.expired:
            self.s.add(PriceObservation(
                listing_id=listing.id, product_id=product.id, observed_at=now, price=offer.price,
                currency=offer.currency, price_usd=price_usd, reference_price=offer.reference_price,
                price_kind=offer.price_kind.value,
            ))
        return listing, changed

    def active_listings(self, since: datetime) -> list[Listing]:
        return list(self.s.scalars(select(Listing).where(Listing.last_seen >= since, Listing.expired.is_(False))).all())

    def observations(self, product_id: str, currency: str, since: datetime | None = None) -> list[PriceObservation]:
        q = select(PriceObservation).where(PriceObservation.product_id == product_id,
                                           PriceObservation.currency == currency)
        if since is not None:
            q = q.where(PriceObservation.observed_at >= since)
        return list(self.s.scalars(q.order_by(PriceObservation.observed_at)).all())

    # ------------------------------------------------------------------ fx
    def get_fx(self, rate_date: str) -> dict[str, float]:
        rows = self.s.scalars(select(FxRate).where(FxRate.rate_date == rate_date)).all()
        return {r.currency: r.amd_per_unit for r in rows}

    def latest_fx(self) -> tuple[str | None, dict[str, float], str | None]:
        latest = self.s.scalar(select(func.max(FxRate.rate_date)))
        if latest is None:
            return None, {}, None
        rows = self.s.scalars(select(FxRate).where(FxRate.rate_date == latest)).all()
        return latest, {r.currency: r.amd_per_unit for r in rows}, (rows[0].source if rows else None)

    def save_fx(self, rate_date: str, rates: dict[str, float], source: str, now: datetime) -> None:
        for cur, val in rates.items():
            row = self.s.scalar(select(FxRate).where(FxRate.rate_date == rate_date, FxRate.currency == cur))
            if row is None:
                self.s.add(FxRate(rate_date=rate_date, currency=cur, amd_per_unit=val, source=source, fetched_at=now))
            else:
                row.amd_per_unit, row.source, row.fetched_at = val, source, now

    # ------------------------------------------------------------------ enrichment cache
    def cache_get(self, key: str, max_age: timedelta, now: datetime) -> dict | None:
        row = self.s.get(EnrichmentCache, key)
        if row is None or now - row.fetched_at > max_age:
            return None
        return json.loads(row.data_json)

    def cache_put(self, key: str, data: dict, now: datetime) -> None:
        row = self.s.get(EnrichmentCache, key)
        if row is None:
            self.s.add(EnrichmentCache(key=key, fetched_at=now, data_json=json.dumps(data, default=str)))
        else:
            row.fetched_at, row.data_json = now, json.dumps(data, default=str)

    # ------------------------------------------------------------------ notifications
    def notification(self, product_id: str) -> Notification | None:
        return self.s.get(Notification, product_id)

    def mark_notified(self, product_id: str, price_usd: float, now: datetime, alerted: bool) -> None:
        n = self.s.get(Notification, product_id)
        if n is None:
            self.s.add(Notification(product_id=product_id, first_sent_at=now, last_sent_at=now,
                                    best_price_usd_sent=price_usd, times_sent=1, alerted=alerted))
        else:
            n.last_sent_at = now
            n.times_sent += 1
            n.best_price_usd_sent = min(n.best_price_usd_sent, price_usd)
            n.alerted = n.alerted or alerted

    def mark_alerted(self, product_id: str, price_usd: float, now: datetime) -> None:
        n = self.s.get(Notification, product_id)
        if n is None:
            n = Notification(product_id=product_id, first_sent_at=now, last_sent_at=now,
                             best_price_usd_sent=price_usd, times_sent=1, alerted=True)
            self.s.add(n)
        n.alerted = True
        n.alerted_at = now
        n.alerted_price_usd = price_usd if n.alerted_price_usd is None else min(n.alerted_price_usd, price_usd)

    # ------------------------------------------------------------------ runs
    def start_run(self, now: datetime) -> Run:
        run = Run(started_at=now, status="running")
        self.s.add(run)
        self.s.flush()
        return run

    def last_email_run(self) -> Run | None:
        return self.s.scalar(select(Run).where(Run.email_sent.is_(True)).order_by(Run.started_at.desc()).limit(1))

    def run_count(self) -> int:
        return self.s.scalar(select(func.count(Run.id))) or 0

    # ------------------------------------------------------------------ maintenance
    def prune(self, now: datetime, retention_days: int) -> int:
        cutoff = now - timedelta(days=retention_days)
        res = self.s.execute(delete(PriceObservation).where(PriceObservation.observed_at < cutoff))
        self.s.execute(delete(EnrichmentCache).where(EnrichmentCache.fetched_at < now - timedelta(days=14)))
        self.s.execute(delete(Run).where(Run.started_at < now - timedelta(days=60)))
        return res.rowcount or 0
