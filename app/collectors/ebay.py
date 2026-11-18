"""eBay Browse API (free developer account: developer.ebay.com).

Searches your watchlist keywords for NEW, fixed-price items. Uses the
application (client-credentials) OAuth flow — no eBay user login needed.
"""
from __future__ import annotations

import base64
import logging
from urllib.parse import quote

from app.analysis.categorize import NON_PRODUCT, categorize
from app.collectors.base import CollectContext, Collector, build_offer
from app.models.schemas import PriceKind, RawOffer, ReferenceKind

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
REGION_BY_MARKETPLACE = {"EBAY_US": ("US", "USD"), "EBAY_DE": ("DE", "EUR"), "EBAY_GB": ("UK", "GBP")}


class EbayCollector(Collector):
    type = "ebay"

    def available(self, ctx: CollectContext) -> tuple[bool, str | None]:
        s = ctx.cfg.secrets
        if not (s.ebay_client_id and s.ebay_client_secret):
            return False, "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set"
        if not ctx.cfg.watchlist.watchlist:
            return False, "watchlist is empty"
        return True, None

    def _token(self, ctx: CollectContext) -> str:
        s = ctx.cfg.secrets
        basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
        resp = ctx.http.post(TOKEN_URL, headers={"Authorization": f"Basic {basic}",
                                                 "Content-Type": "application/x-www-form-urlencoded"},
                             data={"grant_type": "client_credentials", "scope": SCOPE})
        resp.raise_for_status()
        return resp.json()["access_token"]

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        token = self._token(ctx)
        marketplace = self.source.marketplace or "EBAY_US"
        region, currency = REGION_BY_MARKETPLACE.get(marketplace, ("US", "USD"))
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace}
        offers: list[RawOffer] = []
        skipped = 0
        seen: set[str] = set()
        for item in ctx.cfg.watchlist.watchlist:
            for kw in item.keywords:
                url = (f"{SEARCH_URL}?q={quote(kw)}&limit={self.source.max_per_keyword}"
                       f"&filter={quote('buyingOptions:{FIXED_PRICE},conditions:{NEW}')}")
                resp = ctx.http.get(url, headers=headers, check_robots=False)
                resp.raise_for_status()
                for s in resp.json().get("itemSummaries", []) or []:
                    iid = s.get("itemId")
                    if not iid or iid in seen:
                        continue
                    seen.add(iid)
                    offer = self._to_offer(s, region, currency, ctx)
                    if offer is None:
                        skipped += 1
                    else:
                        offers.append(offer)
        return offers, skipped

    def _to_offer(self, s: dict, region: str, currency: str, ctx: CollectContext) -> RawOffer | None:
        title = s.get("title") or ""
        if categorize(title, None, ctx.cfg.categories) == NON_PRODUCT:
            return None
        price = s.get("price") or {}
        orig = ((s.get("marketingPrice") or {}).get("originalPrice") or {})
        try:
            p = float(price.get("value"))
        except (TypeError, ValueError):
            return None
        ref = float(orig["value"]) if orig.get("value") and float(orig["value"]) > p else None
        ship_opts = s.get("shippingOptions") or []
        ship_cost = None
        if ship_opts:
            sc = ship_opts[0].get("shippingCost") or {}
            if sc.get("value") is not None:
                ship_cost = float(sc["value"])
        seller = (s.get("seller") or {})
        return build_offer(
            source=self.id,
            source_item_id=s["itemId"],
            title=title,
            url=s.get("itemWebUrl") or s.get("itemHref"),
            retailer="eBay",
            region=region,
            currency=price.get("currency") or currency,
            price=p,
            reference_price=ref,
            reference_kind=ReferenceKind.REGULAR_PRICE if ref else ReferenceKind.NONE,
            price_kind=PriceKind.CONFIRMED,
            image_url=(s.get("image") or {}).get("imageUrl"),
            gtin=(s.get("gtin") or None),
            availability="in_stock",
            shipping_cost=ship_cost,
            free_shipping=(ship_cost == 0) if ship_cost is not None else None,
            observed_at=ctx.now,
            extra={"seller": seller.get("username"), "seller_feedback_pct": seller.get("feedbackPercentage"),
                   "condition": s.get("condition")},
        )
