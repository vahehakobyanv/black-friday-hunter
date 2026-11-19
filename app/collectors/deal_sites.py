"""More US deal sources (public RSS / schema.org data, allowed by robots.txt).

- DealCatcher RSS: one product per item, title "Store - Product $price".
- 9to5Toys RSS: editor-written posts; only single-product posts with one clear
  price are used, roundups ("sale", "up to", "from $X", "deals") are skipped.
- BlackFriday.com ad pages: the schema.org ItemList on each store's Black Friday /
  Cyber Monday ad page (store, product, advertised price, link to the shop).
"""
from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from app.analysis.categorize import NON_PRODUCT, categorize
from app.collectors.base import CollectContext, Collector, build_offer, normalize_retailer
from app.config import source_feeds
from app.models.schemas import PriceKind, RawOffer, ReferenceKind
from app.utils.http import BlockedError
from app.utils.text import strip_html

log = logging.getLogger(__name__)

_MONEY = r"\$\s?([\d,]+(?:\.\d{1,2})?)"


def _money(s: str) -> float:
    return float(s.replace(",", ""))


def _rss_items(xml_text: str) -> list[ET.Element]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    return list(root.iter("item"))


def _pub(it: ET.Element):
    p = it.findtext("pubDate")
    try:
        return parsedate_to_datetime(p) if p else None
    except (TypeError, ValueError):
        return None


def hint_from_text(text: str) -> str | None:
    """Rough category hint from a site's own category label/path (title keywords still win)."""
    t = text.lower()
    rules = [
        (("laptop", "notebook"), "laptops"),
        (("video-game", "video game", "gaming", "console"), "gaming"),
        (("kitchen",), "kitchen"),
        (("smart home", "smart-home"), "smart_home"),
        (("appliance", "vacuum"), "appliances"),
        (("tv", "audio", "headphone", "speaker", "home theater", "camera"), "tv_electronics"),
        (("cell phone", "smartphone"), "smartphones"),
        (("furniture", "mattress", "bedding", "home decor"), "home_furniture"),
        (("clothing", "shoes", "apparel"), "clothing"),
        (("luggage", "travel"), "travel"),
    ]
    for words, cat in rules:
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in words):
            return cat
    return None


class _FeedCollector(Collector):
    """Read every feed of the source, de-duplicate, convert with `_to_offer`."""

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        seen: set[str] = set()
        offers: list[RawOffer] = []
        skipped = 0
        errors = []
        for feed in source_feeds(self.source):
            try:
                resp = ctx.http.get(feed.url)
                resp.raise_for_status()
                raw = self._parse(resp.text, feed.url)
            except BlockedError:
                raise
            except Exception as exc:
                errors.append(f"{feed.url}: {exc}")
                log.warning("%s feed failed %s: %s", self.id, feed.url, exc)
                continue
            for key, item in raw:
                if key in seen:
                    continue
                seen.add(key)
                offer = self._to_offer(item, key, ctx, feed.category)
                if offer is None:
                    skipped += 1
                else:
                    offers.append(offer)
        if errors and not offers:
            raise RuntimeError("; ".join(errors)[:300])
        return offers, skipped

    def _parse(self, text: str, url: str) -> list[tuple[str, object]]:
        return [((it.findtext("guid") or it.findtext("link") or "").strip(), it) for it in _rss_items(text)]

    def _to_offer(self, item, key: str, ctx: CollectContext, hint: str | None) -> RawOffer | None:
        raise NotImplementedError


# ------------------------------------------------------------------ DealCatcher
_DC_SKIP_SECTIONS = ("grocery/", "health-beauty/", "household/pets", "baby-kids/diapers")


def parse_dealcatcher_title(title: str) -> tuple[str, str, float] | None:
    """'Amazon - Marshall Middleton Speaker $149.99' -> (store, product, price)."""
    m = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s+" + _MONEY + r"\s*$", title)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip(), _money(m.group(3))


class DealCatcherCollector(_FeedCollector):
    type = "dealcatcher"

    def _to_offer(self, it: ET.Element, key: str, ctx: CollectContext, hint: str | None) -> RawOffer | None:
        title = html.unescape((it.findtext("title") or "").strip())
        parsed = parse_dealcatcher_title(title)
        if not parsed:
            return None  # store-wide sales have no single price
        store, product, price = parsed
        link = (it.findtext("link") or "").split("?")[0]
        section = link.split("/deals/", 1)[1].rsplit("/", 1)[0] if "/deals/" in link else ""
        if section.startswith(_DC_SKIP_SECTIONS):
            return None  # groceries, cosmetics, pet food: not worth forwarding to Armenia
        cat = categorize(product, section.replace("-", " "), ctx.cfg.categories, hint or hint_from_text(section))
        if cat == NON_PRODUCT:
            return None
        img = re.search(r'src="([^"]+)"', it.findtext("description") or "")
        return build_offer(
            source=self.id, source_item_id=key.rsplit("-", 1)[-1].split("?")[0] or key,
            title=product, url=link, retailer=normalize_retailer(store, ctx.cfg),
            region=self.source.region, currency="USD", price=price,
            price_kind=PriceKind.ADVERTISED, image_url=img.group(1) if img else None,
            feed_category=section or None, category_hint=hint or hint_from_text(section),
            availability="unknown", published_at=_pub(it), observed_at=ctx.now,
        )


# ------------------------------------------------------------------ 9to5Toys
_ROUNDUP = re.compile(
    r"\b(sales?|deals|up to|from(?: just)? \$|starting|starts? (?:at|from)|freebies|under \$|each|"
    r"\d+% off|bundles?|gift guide|best .* of|roundup|\+ more|more from)\b", re.I)
_PRICE_PHRASE = re.compile(
    r"\b(?:at|to|now|for|hits|just|only|low of|lows? at|price of)\s+(?:an? |the |new |exclusive |all-time |"
    r"(?:\w+-)?time |annual |amazon |black friday )*(?:low (?:of |at )?)?" + _MONEY, re.I)
_OFF = re.compile(r"\(\s*" + _MONEY + r"\s+off\s*\)", re.I)
_VERB_CUT = re.compile(
    r"\s+(?:just|dropped|drops?|hits?|falls?|plunges?|returns?|gets?|sees?|is|are|now|down|on sale|scores?|lands?|"
    r"goes|tumbles?|slashed|marked|discounted|at)\b.*$", re.I)

_IMPERATIVE = re.compile(
    r"^(?:power|step|convert|grab|get|score|upgrade|add|don.?t|save|pick|take|keep|make|turn|bring|gift|"
    r"treat|stock|level|charge|protect|outfit|refresh|rock|go|check|shop|snag|land|nab)\b", re.I)

RETAILER_HOSTS = [
    (("link.amazon", "amazon.com", "amzn.to"), "Amazon"),
    (("bestbuy",), "Best Buy"),
    (("walmart",), "Walmart"),
    (("target.com", "goto.target"), "Target"),
    (("newegg",), "Newegg"),
    (("bhphotovideo",), "B&H Photo"),
    (("costco",), "Costco"),
    (("woot.com",), "Woot"),
    (("apple.com",), "Apple"),
    (("samsung",), "Samsung"),
    (("dell.com",), "Dell"),
    (("lenovo",), "Lenovo"),
]


def retailer_from_links(body_html: str) -> str | None:
    for host in re.findall(r'href="https?://([^/"]+)', body_html or "")[:4]:
        host = host.lower()
        if host.endswith("9to5toys.com"):
            continue
        for keys, name in RETAILER_HOSTS:
            if any(k in host for k in keys):
                return name
        return None  # first outside link is a shop we don't know
    return None


def parse_editorial_title(title: str) -> tuple[str, float, float | None] | None:
    """Single-product headline -> (product name, price, amount off). Roundups -> None."""
    if _ROUNDUP.search(title) or len(re.findall(_MONEY, title)) > 2:
        return None
    if re.search(r",\s*more\b|\bmore\)?\s*$", title, re.I):
        return None  # "..., more": several products in one post
    m = _PRICE_PHRASE.search(title)
    if not m or re.match(r"\s*(?:off|savings|discount|in savings)\b", title[m.end():], re.I):
        return None  # "$70 off" is a discount, not a price
    price = _money(m.group(1))
    off = _OFF.search(title)
    product = _VERB_CUT.sub("", title[:m.start()]).strip(" ,:-–")
    product = re.sub(r"^(?:rare|first|new|today'?s|all-time low:?)\s+(?:deal\s+)?(?:on\s+|hits\s+)?", "", product, flags=re.I)
    if len(product.split()) < 2 or re.search(r"\bdeals?\b", product, re.I) or _IMPERATIVE.match(product):
        return None  # no clean product name
    return product, price, _money(off.group(1)) if off else None


class EditorialRssCollector(_FeedCollector):
    """Deal blogs written by editors (9to5Toys)."""
    type = "editorial_rss"

    def _to_offer(self, it: ET.Element, key: str, ctx: CollectContext, hint: str | None) -> RawOffer | None:
        title = html.unescape((it.findtext("title") or "").strip())
        parsed = parse_editorial_title(title)
        if not parsed:
            return None
        product, price, off = parsed
        body = it.findtext("description") or ""
        retailer = retailer_from_links(body)
        if retailer is None:
            return None  # unknown shop: we could not estimate delivery honestly
        cat = categorize(product, None, ctx.cfg.categories, hint)
        if cat == NON_PRODUCT:
            return None
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        return build_offer(
            source=self.id, source_item_id=key.rsplit("=", 1)[-1] or key,
            title=product, url=(it.findtext("link") or "").strip(),
            retailer=normalize_retailer(retailer, ctx.cfg), region=self.source.region, currency="USD",
            price=price, reference_price=round(price + off, 2) if off else None,
            reference_kind=ReferenceKind.LIST_PRICE if off else ReferenceKind.NONE,
            price_kind=PriceKind.ADVERTISED, image_url=img.group(1) if img else None,
            category_hint=hint, availability="unknown", description=strip_html(body)[:500],
            published_at=_pub(it), observed_at=ctx.now, extra={"editorial": True},
        )


# ------------------------------------------------------------------ BlackFriday.com ads
def parse_ad_page(page_html: str) -> list[dict]:
    """schema.org ItemList products from a store's ad page."""
    out = []
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page_html, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for el in data.get("itemListElement") or []:
            offer = el.get("offers") or {}
            try:
                price = float(offer.get("price"))
            except (TypeError, ValueError):
                continue
            out.append({
                "name": html.unescape(el.get("name") or "").strip(),
                "category": el.get("category"),
                "price": price,
                "currency": offer.get("priceCurrency") or "USD",
                "image": el.get("image"),
                "url": el.get("url"),
            })
    return out


class BlackFridayAdsCollector(Collector):
    type = "bf_ads"

    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        offers: list[RawOffer] = []
        skipped = 0
        errors = []
        seen: set[str] = set()
        for page in self.source.ad_pages:
            url = f"{self.source.base_url.rstrip('/')}/{page.event}/{page.slug}"
            try:
                resp = ctx.http.get(url)
                resp.raise_for_status()
                items = parse_ad_page(resp.text)
            except BlockedError:
                raise
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                log.warning("ad page failed %s: %s", url, exc)
                continue
            for item in items:
                key = item["url"] or item["name"]
                if not item["name"] or key in seen:
                    continue
                seen.add(key)
                offer = self._to_offer(item, page, url, ctx)
                if offer is None:
                    skipped += 1
                else:
                    offers.append(offer)
        if errors and not offers:
            raise RuntimeError("; ".join(errors)[:300])
        return offers, skipped

    def _to_offer(self, item: dict, page, ad_url: str, ctx: CollectContext) -> RawOffer | None:
        name = re.sub(r"\s+-\s+[\w.]+\.com\s*$", "", item["name"]).strip()
        hint = hint_from_text(item["category"] or "")
        cat = categorize(name, item["category"], ctx.cfg.categories, hint)
        if cat == NON_PRODUCT:
            return None
        link = item["url"] or ad_url
        host = urlsplit(link).netloc
        return build_offer(
            source=self.id, source_item_id=f"{page.event}:{item['url'] or name}"[:300],
            title=name, url=link, retailer=normalize_retailer(page.store or host, ctx.cfg),
            region=self.source.region, currency=item["currency"], price=item["price"],
            price_kind=PriceKind.ADVERTISED, image_url=item["image"], feed_category=item["category"],
            category_hint=hint, availability="unknown", observed_at=ctx.now,
            extra={"ad_scan": True, "ad_event": page.event, "ad_url": ad_url},
        )
