"""dealnews.com (US) — editor-verified deals via public RSS (allowed by robots.txt).

Covers Amazon, Walmart, Best Buy, Newegg, B&H, Adorama, Dell, HP, Lenovo,
Samsung, Apple, Nike, Adidas and many more US shops.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from app.analysis.categorize import NON_PRODUCT, categorize
from app.collectors.base import CollectContext, Collector, build_offer, normalize_retailer
from app.config import source_feeds
from app.models.schemas import PriceKind, RawOffer, ReferenceKind
from app.utils.http import BlockedError
from app.utils.text import parse_price, strip_html

log = logging.getLogger(__name__)

NS = {"dealnews": "https://www.dealnews.com/ns/rss/1.0.htm", "media": "http://search.yahoo.com/mrss/"}
_MONEY = r"\$\s?([\d,]+(?:\.\d{1,2})?)"


def extract_reference(text: str, price: float) -> tuple[float | None, ReferenceKind]:
    """Find a 'previous price' claim in the editorial text."""
    t = text.replace(" ", " ")
    m = re.search(r"(?:best|lowest) (?:price|total) we (?:could )?f(?:ou|i)nd by " + _MONEY, t, re.I)
    if m:
        return round(price + float(m.group(1).replace(",", "")), 2), ReferenceKind.NEXT_BEST_PRICE
    m = re.search(r"(?:down from|reg(?:ularly)?\.?|originally|list(?:ed)? (?:price|at)|a savings of .* off) " + _MONEY, t, re.I)
    if m:
        ref = float(m.group(1).replace(",", ""))
        if ref > price:
            return ref, ReferenceKind.LIST_PRICE
    m = re.search(r"that'?s " + _MONEY + r" off", t, re.I)
    if m:
        return round(price + float(m.group(1).replace(",", "")), 2), ReferenceKind.LIST_PRICE
    return None, ReferenceKind.NONE


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for it in root.iter("item"):
        price_el = it.find("dealnews:price", NS)
        media = it.find("media:content", NS)
        pub = it.findtext("pubDate")
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "guid": (it.findtext("guid") or it.findtext("link") or "").strip(),
            "retailer": (it.findtext("dealnews:retailer", namespaces=NS) or "").strip() or None,
            "category": (it.findtext("dealnews:category", namespaces=NS) or "").strip() or None,
            "deal_type": (it.findtext("dealnews:dealType", namespaces=NS) or "deal").strip().lower(),
            "staff_pick": (it.findtext("dealnews:staffPick", namespaces=NS) or "").strip().lower() == "true",
            "price": float(price_el.text) if price_el is not None and price_el.text else None,
            "currency": price_el.get("currency", "USD") if price_el is not None else "USD",
            "image": media.get("url") if media is not None else None,
            "published": parsedate_to_datetime(pub) if pub else None,
            "description": strip_html(it.findtext("description")),
        })
    return out


class DealnewsCollector(Collector):
    type = "dealnews"

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        seen: set[str] = set()
        offers: list[RawOffer] = []
        skipped = 0
        errors = []
        for feed in source_feeds(self.source):
            url = feed.url
            try:
                resp = ctx.http.get(url)
                resp.raise_for_status()
                items = parse_feed(resp.text)
            except BlockedError:
                raise
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                log.warning("dealnews feed failed %s: %s", url, exc)
                continue
            for item in items:
                gid = re.search(r"/(\d+)\.html", item["guid"])
                item_id = gid.group(1) if gid else item["guid"]
                if item_id in seen:
                    continue
                seen.add(item_id)
                offer = self._to_offer(item, item_id, ctx, feed.category)
                if offer is None:
                    skipped += 1
                else:
                    offers.append(offer)
        if errors and not offers:
            raise RuntimeError("; ".join(errors)[:300])
        return offers, skipped

    def _to_offer(self, item: dict, item_id: str, ctx: CollectContext, hint: str | None = None) -> RawOffer | None:
        title = item["title"]
        if item["deal_type"] not in ("deal", "") or item["price"] is None or item["price"] <= 0:
            return None
        if re.match(r"^(up to|extra|save|buy|get|\d+% off)\b", title, re.I) or " sale" in title.lower()[:40]:
            return None  # store-wide sales, not one product
        cat = categorize(title, item["category"], ctx.cfg.categories, hint)
        if cat == NON_PRODUCT:
            return None
        ref, kind = extract_reference(item["description"], item["price"])
        free = bool(re.search(r"free (?:shipping|delivery)", title, re.I))
        ship = None
        m = re.search(r"\+\s*" + _MONEY + r"\s*s(?:&|\+)?h|\+\s*" + _MONEY + r"\s*shipping", title, re.I)
        if m:
            ship, _ = parse_price("$" + (m.group(1) or m.group(2)))
        product_title = re.sub(r"\s+(?:for|from|at)\s+\$[\d,.]+.*$", "", title, flags=re.I).strip()
        return build_offer(
            source=self.id,
            source_item_id=item_id,
            title=product_title,
            url=item["link"].replace("?iref=rss", ""),
            retailer=normalize_retailer(item["retailer"], ctx.cfg),
            region=self.source.region,
            currency=item["currency"],
            price=item["price"],
            reference_price=ref,
            reference_kind=kind,
            price_kind=PriceKind.ADVERTISED,
            image_url=item["image"],
            feed_category=item["category"],
            category_hint=hint,
            availability="unknown",
            shipping_cost=0.0 if free else ship,
            free_shipping=free or None,
            staff_pick=item["staff_pick"],
            description=item["description"][:500],
            published_at=item["published"],
            observed_at=ctx.now,
            extra={"editorial": True},
        )
