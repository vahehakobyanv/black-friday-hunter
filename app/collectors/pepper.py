"""Pepper network deal communities: mydealz.de (DE), hotukdeals.com (UK).

Uses the public RSS feeds (allowed by robots.txt). For the most relevant items
it also reads the public deal page (also allowed) to get the community's
"next best price" (same item at another shop), expiry state, shipping and the
actual shop domain. Click-out links (/visit/...) are NOT followed: they are
disallowed by robots.txt, so the report links to the deal page instead.
"""
from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

from app.analysis.categorize import NON_PRODUCT, categorize
from app.collectors.base import CollectContext, Collector, build_offer, normalize_retailer
from app.config import source_feeds
from app.models.schemas import PriceKind, RawOffer, ReferenceKind
from app.utils.http import BlockedError
from app.utils.text import parse_price, strip_html

log = logging.getLogger(__name__)

NS = {
    "pepper": "http://www.pepper.com/rss",
    "media": "http://search.yahoo.com/mrss/",
}
_TEMP_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*°\s*-?\s*")


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    for it in root.iter("item"):
        title_raw = (it.findtext("title") or "").strip()
        m = _TEMP_RE.match(title_raw)
        temperature = float(m.group(1).replace(",", ".")) if m else None
        title = _TEMP_RE.sub("", title_raw).strip()
        merchant = it.find("pepper:merchant", NS)
        media = it.find("media:content", NS)
        pub = it.findtext("pubDate")
        link = (it.findtext("link") or "").strip()
        tid = re.search(r"-(\d+)$", link.rstrip("/"))
        items.append({
            "title": title,
            "temperature": temperature,
            "link": link,
            "thread_id": tid.group(1) if tid else link,
            "category": (it.findtext("category") or "").strip() or None,
            "merchant": merchant.get("name") if merchant is not None else None,
            "price_text": merchant.get("price") if merchant is not None else None,
            "image": media.get("url") if media is not None else None,
            "published": parsedate_to_datetime(pub) if pub else None,
            "description": strip_html(it.findtext("description"))[:500],
        })
    return items


def parse_thread_json(page: str, thread_id: str) -> dict | None:
    """Find the main deal object embedded in the page and return its fields."""
    text = page if '"threadId"' in page else html.unescape(page)
    marker = f'"threadId":"{thread_id}"'
    idx = text.find(marker)
    if idx < 0:
        return None
    decoder = json.JSONDecoder()
    pos = idx
    for _ in range(400):
        pos = text.rfind("{", 0, pos)
        if pos < 0:
            break
        try:
            obj, _end = decoder.raw_decode(text, pos)
        except ValueError:
            continue
        if isinstance(obj, dict) and str(obj.get("threadId")) == str(thread_id) and "price" in obj:
            shipping = obj.get("shipping") or {}
            dispatched = obj.get("dispatchedFrom") or {}
            img = obj.get("mainImage") or {}
            return {
                "price": obj.get("price"),
                "next_best_price": obj.get("nextBestPrice"),
                "temperature": obj.get("temperature"),
                "is_expired": bool(obj.get("isExpired")),
                "link_host": obj.get("linkHost"),
                "merchant": (obj.get("merchant") or {}).get("merchantName") if isinstance(obj.get("merchant"), dict) else None,
                "shipping_price": shipping.get("price") if isinstance(shipping, dict) else None,
                "shipping_free": shipping.get("isFree") if isinstance(shipping, dict) else None,
                "dispatched_from": dispatched.get("iso") if isinstance(dispatched, dict) else None,
                "voucher_code": obj.get("voucherCode") or None,
                "status": obj.get("status"),
                "local": obj.get("isLocal"),
                "image_path": f"{img.get('path')}/{img.get('name')}" if isinstance(img, dict) and img.get("name") else None,
            }
    return None


class PepperCollector(Collector):
    type = "pepper"

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        cfg = ctx.cfg
        items: list[dict] = []
        seen: set[str] = set()
        errors: list[str] = []
        feeds = source_feeds(self.source)
        for feed in feeds:
            try:
                resp = ctx.http.get(feed.url)
                resp.raise_for_status()
                feed_items = parse_feed(resp.text)
            except BlockedError:
                raise
            except Exception as exc:  # one broken feed must not lose the others
                errors.append(f"{feed.url}: {exc}")
                log.warning("pepper feed failed %s: %s", feed.url, exc)
                continue
            for item in feed_items:
                if item["thread_id"] in seen:
                    continue
                seen.add(item["thread_id"])
                item["hint"] = feed.category
                items.append(item)
        if errors and not items:
            raise RuntimeError("; ".join(errors)[:300])
        offers: list[RawOffer] = []
        skipped = 0
        candidates: list[tuple[dict, str]] = []
        for item in items:
            price, cur = parse_price(item["price_text"])
            if price is None:
                skipped += 1  # no price -> voucher/cashback/contract
                continue
            cat = categorize(item["title"], item["category"], cfg.categories, item["hint"])
            if cat == NON_PRODUCT:
                skipped += 1
                continue
            item["price"], item["currency"], item["cat"] = price, cur or self.source.currency, cat
            candidates.append((item, cat))

        # Enrich the most relevant items (product categories first, hottest first).
        budget = self.source.enrich_budget if self.source.enrich_budget is not None else cfg.settings.collection.pepper_enrich_per_site
        refresh = timedelta(hours=cfg.settings.collection.pepper_enrich_refresh_hours)
        ranked = sorted(candidates, key=lambda c: (c[1] in cfg.settings.filters.exclude_categories,
                                                   -(c[0]["temperature"] or 0)))
        details: dict[str, dict] = {}
        for item, _cat in ranked:
            key = f"pepper:{item['link']}"
            cached = ctx.repo.cache_get(key, refresh, ctx.now) if ctx.repo else None
            if cached is not None:
                details[item["link"]] = cached
                continue
            if budget <= 0:
                continue
            budget -= 1
            try:
                page = ctx.http.get(item["link"])
                if page.status_code == 200:
                    data = parse_thread_json(page.text, item["thread_id"]) or {}
                    details[item["link"]] = data
                    if ctx.repo:
                        ctx.repo.cache_put(key, data, ctx.now)
            except BlockedError as exc:
                log.warning("pepper detail blocked: %s", exc)
                budget = 0
            except Exception as exc:  # detail page is optional
                log.warning("pepper detail failed for %s: %s", item["link"], exc)

        for item, _cat in candidates:
            d = details.get(item["link"], {})
            if d.get("local"):
                skipped += 1  # in-store only deals can't be shipped to a forwarder
                continue
            price = float(d["price"]) if d.get("price") else item["price"]
            nbp = d.get("next_best_price")
            ref = float(nbp) if nbp and float(nbp) > price else None
            retailer = normalize_retailer(item["merchant"] or d.get("link_host"), cfg)
            offer = build_offer(
                source=self.id,
                source_item_id=item["thread_id"],
                title=item["title"],
                url=item["link"],
                retailer=retailer,
                region=self.source.region,
                currency=item["currency"],
                price=price,
                reference_price=ref,
                reference_kind=ReferenceKind.NEXT_BEST_PRICE if ref else ReferenceKind.NONE,
                price_kind=PriceKind.ADVERTISED,
                image_url=_bigger_image(item["image"]),
                feed_category=item["category"],
                category_hint=item["hint"],
                availability="expired" if d.get("is_expired") else "unknown",
                shipping_cost=float(d["shipping_price"]) if d.get("shipping_price") not in (None, "") else None,
                free_shipping=d.get("shipping_free"),
                community_score=float(d["temperature"]) if d.get("temperature") is not None else item["temperature"],
                expired=bool(d.get("is_expired")),
                voucher_code=d.get("voucher_code"),
                description=item["description"],
                published_at=item["published"],
                observed_at=ctx.now,
                extra={"enriched": bool(d), "link_host": d.get("link_host"),
                       "dispatched_from": d.get("dispatched_from")},
            )
            if offer is None:
                skipped += 1
            else:
                offers.append(offer)
        return offers, skipped


def _bigger_image(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"/re/\d+x\d+/", "/re/300x300/", url)
