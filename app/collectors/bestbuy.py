"""Best Buy official Products API (free developer key: developer.bestbuy.com).

Prices here are CONFIRMED (straight from the retailer) and include the
retailer's regular price, UPC and shipping weight.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from app.analysis.categorize import NON_PRODUCT, categorize
from app.collectors.base import CollectContext, Collector, build_offer
from app.models.schemas import PriceKind, RawOffer, ReferenceKind

log = logging.getLogger(__name__)

API = "https://api.bestbuy.com/v1/products"
FIELDS = ("sku,name,manufacturer,modelNumber,upc,salePrice,regularPrice,onSale,url,image,"
          "onlineAvailability,freeShipping,shippingCost,shippingWeight,categoryPath.name,percentSavings")


def _weight_kg(value) -> float | None:
    """Best Buy gives shippingWeight in pounds (number or '3.5 pounds')."""
    if value in (None, ""):
        return None
    m = re.search(r"[\d.]+", str(value))
    if not m:
        return None
    try:
        return round(float(m.group(0)) * 0.45359237, 2)
    except ValueError:
        return None


def build_query_url(query: str, api_key: str, page_size: int, page: int = 1) -> str:
    # query like "onSale=true&categoryPath.name=Laptops" -> (onSale=true&categoryPath.name=Laptops*)
    parts = []
    for cond in query.split("&"):
        k, _, v = cond.partition("=")
        v = v.strip()
        if k.endswith(".name"):
            v = quote(v + "*", safe="*")    # category names: prefix match
        parts.append(f"{k}={v}")
    return (f"{API}({'&'.join(parts)})?apiKey={api_key}&format=json&pageSize={page_size}&page={page}"
            f"&sort=percentSavings.dsc&show={FIELDS}")


class BestBuyCollector(Collector):
    type = "bestbuy"

    def available(self, ctx: CollectContext) -> tuple[bool, str | None]:
        if not ctx.cfg.secrets.bestbuy_api_key:
            return False, "BESTBUY_API_KEY not set"
        return True, None

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        key = ctx.cfg.secrets.bestbuy_api_key
        assert key
        offers: list[RawOffer] = []
        skipped = 0
        for q in self.source.queries:
            url = build_query_url(q, key, self.source.page_size)
            resp = ctx.http.get(url, check_robots=False)  # official API, not crawling
            resp.raise_for_status()
            for p in resp.json().get("products", []):
                offer = self._to_offer(p, ctx)
                if offer is None:
                    skipped += 1
                else:
                    offers.append(offer)
        return offers, skipped

    def _to_offer(self, p: dict, ctx: CollectContext) -> RawOffer | None:
        name = p.get("name") or ""
        cats = p.get("categoryPath") or []
        feed_cat = cats[-1].get("name") if cats and isinstance(cats[-1], dict) else None
        if categorize(name, feed_cat, ctx.cfg.categories) == NON_PRODUCT:
            return None
        sale, regular = p.get("salePrice"), p.get("regularPrice")
        return build_offer(
            source=self.id,
            source_item_id=str(p.get("sku")),
            title=name,
            url=p.get("url") or f"https://www.bestbuy.com/site/{p.get('sku')}.p",
            retailer="Best Buy",
            region="US",
            currency="USD",
            price=sale,
            reference_price=regular if regular and sale and regular > sale else None,
            reference_kind=ReferenceKind.REGULAR_PRICE if regular and sale and regular > sale else ReferenceKind.NONE,
            price_kind=PriceKind.CONFIRMED,
            image_url=p.get("image"),
            brand=p.get("manufacturer"),
            model=p.get("modelNumber"),
            gtin=p.get("upc"),
            mpn=p.get("modelNumber"),
            feed_category=feed_cat,
            availability="in_stock" if p.get("onlineAvailability") else "out_of_stock",
            free_shipping=p.get("freeShipping"),
            shipping_cost=0.0 if p.get("freeShipping") else None,
            weight_kg=_weight_kg(p.get("shippingWeight")),
            observed_at=ctx.now,
            extra={"percent_savings": p.get("percentSavings")},
        )
