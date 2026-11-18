"""Watchlist product pages: reads schema.org Product JSON-LD from URLs you put
in watchlist.json — only when the site's robots.txt allows it. Many big shops
block bots or require JavaScript; those are reported as "blocked", never forced.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.collectors.base import CollectContext, Collector, build_offer, normalize_retailer
from app.models.schemas import PriceKind, RawOffer, ReferenceKind
from app.utils.http import BlockedError

log = logging.getLogger(__name__)

REGION_BY_TLD = {"de": ("DE", "EUR"), "uk": ("UK", "GBP"), "com": ("US", "USD")}


def extract_jsonld_product(html_text: str) -> dict | None:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    return node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _first_offer(product: dict) -> dict:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if offers.get("@type") == "AggregateOffer":
        return {"price": offers.get("lowPrice"), "priceCurrency": offers.get("priceCurrency"),
                "availability": offers.get("availability")}
    return offers


class GenericStoreCollector(Collector):
    type = "generic_store"

    def available(self, ctx: CollectContext) -> tuple[bool, str | None]:
        if not any(w.urls for w in ctx.cfg.watchlist.watchlist):
            return False, "no product URLs in watchlist.json"
        return True, None

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        offers: list[RawOffer] = []
        skipped = 0
        for w in ctx.cfg.watchlist.watchlist:
            for url in w.urls:
                try:
                    resp = ctx.http.get(url)  # checks robots.txt, raises BlockedError if disallowed
                    resp.raise_for_status()
                except BlockedError as exc:
                    log.info("watchlist page not permitted: %s", exc)
                    skipped += 1
                    continue
                except Exception as exc:
                    log.warning("watchlist page failed %s: %s", url, exc)
                    skipped += 1
                    continue
                product = extract_jsonld_product(resp.text)
                offer = self._to_offer(product, url, ctx) if product else None
                if offer is None:
                    skipped += 1
                else:
                    offers.append(offer)
        return offers, skipped

    def _to_offer(self, product: dict, url: str, ctx: CollectContext) -> RawOffer | None:
        o = _first_offer(product)
        try:
            price = float(str(o.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            return None
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        region, cur = REGION_BY_TLD.get(host.rsplit(".", 1)[-1], ("US", "USD"))
        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        image = product.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        avail = str(o.get("availability") or "").lower()
        return build_offer(
            source=self.id,
            source_item_id=url,
            title=product.get("name") or url,
            url=url,
            retailer=normalize_retailer(host, ctx.cfg),
            region=region,
            currency=o.get("priceCurrency") or cur,
            price=price,
            reference_kind=ReferenceKind.NONE,
            price_kind=PriceKind.CONFIRMED,
            image_url=image if isinstance(image, str) else None,
            brand=brand if isinstance(brand, str) else None,
            gtin=product.get("gtin13") or product.get("gtin12") or product.get("gtin"),
            mpn=product.get("mpn"),
            availability="in_stock" if "instock" in avail else ("out_of_stock" if "outofstock" in avail else "unknown"),
            observed_at=ctx.now,
        )
