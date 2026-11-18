from datetime import datetime

import pytest

from app.collectors.base import CollectContext, normalize_retailer
from app.collectors.bestbuy import BestBuyCollector, build_query_url
from app.collectors.dealnews import DealnewsCollector, extract_reference
from app.collectors.dealnews import parse_feed as parse_dealnews
from app.collectors.generic_store import extract_jsonld_product
from app.collectors.pepper import PepperCollector, parse_feed, parse_thread_json
from app.models.schemas import PriceKind, ReferenceKind
from app.utils.http import PoliteClient
from tests.conftest import fixture_text, make_transport

NOW = datetime(2026, 11, 19, 10)


def _ctx(cfg, transport=None):
    http = PoliteClient(cfg.settings.collection.user_agent, per_host_delay=0, transport=transport or make_transport(),
                        sleep=lambda s: None)
    return CollectContext(cfg=cfg, http=http, repo=None, now=NOW)


def test_pepper_feed_parsing():
    items = parse_feed(fixture_text("hotukdeals_hot.xml"))
    assert len(items) == 8
    tablet = next(i for i in items if "MagicPad" in i["title"])
    assert tablet["temperature"] == 107
    assert tablet["merchant"] == "AliExpress" and tablet["price_text"] == "£241.92"
    assert tablet["thread_id"] == "5037982"
    assert not tablet["title"].startswith("107")


def test_pepper_thread_json_from_real_page():
    d = parse_thread_json(fixture_text("pepper_deal_page.html"), "5037982")
    assert d["price"] == 241.92
    assert d["next_best_price"] == 320.08
    assert d["is_expired"] is False
    assert d["link_host"] == "www.aliexpress.com"
    assert d["shipping_free"] is True
    assert parse_thread_json(fixture_text("pepper_deal_page.html"), "999") is None


def test_pepper_collector_filters_non_products(cfg):
    src = next(s for s in cfg.stores.sources if s.id == "mydealz_hot")
    offers, skipped = PepperCollector(src).collect(_ctx(cfg))
    titles = " ".join(o.title for o in offers)
    assert "Sim Only" not in titles and "Cashback" not in titles   # contracts/cashback removed
    assert skipped >= 2
    assert all(o.currency == "EUR" and o.region == "DE" for o in offers)
    assert all(o.price_kind == PriceKind.ADVERTISED for o in offers)


def test_pepper_collector_enriches_reference(cfg):
    src = next(s for s in cfg.stores.sources if s.id == "hotukdeals_hot")
    offers, _ = PepperCollector(src).collect(_ctx(cfg))
    tablet = next(o for o in offers if "MagicPad" in o.title)
    assert tablet.reference_price == 320.08
    assert tablet.reference_kind == ReferenceKind.NEXT_BEST_PRICE
    assert tablet.retailer == "AliExpress"
    assert tablet.free_shipping is True


def test_dealnews_parsing_and_reference():
    items = parse_dealnews(fixture_text("dealnews.xml"))
    guitar = items[0]
    assert guitar["price"] == 744.0 and guitar["retailer"] == "Guitar Center"
    ref, kind = extract_reference(guitar["description"], 744.0)
    assert ref == 929.99 and kind == ReferenceKind.LIST_PRICE
    ref, kind = extract_reference("That's the lowest price we could find by $20.", 100)
    assert ref == 120 and kind == ReferenceKind.NEXT_BEST_PRICE
    assert extract_reference("no claim here", 50) == (None, ReferenceKind.NONE)


def test_dealnews_collector_dedupes_across_feeds(cfg):
    src = next(s for s in cfg.stores.sources if s.id == "dealnews")
    offers, _ = DealnewsCollector(src).collect(_ctx(cfg))
    ids = [o.source_item_id for o in offers]
    assert len(ids) == len(set(ids))        # same fixture served for all 12 feeds
    assert all(" for $" not in o.title for o in offers)


def test_blocked_site_is_reported_not_bypassed(cfg):
    src = next(s for s in cfg.stores.sources if s.id == "dealnews")
    result = DealnewsCollector(src).run(_ctx(cfg, make_transport(blocked_hosts={"www.dealnews.com"})))
    assert result.status.ok is False and "BlockedError" in result.status.error


def test_bestbuy_skipped_without_key_and_parses_api(cfg):
    src = next(s for s in cfg.stores.sources if s.id == "bestbuy")
    res = BestBuyCollector(src).run(_ctx(cfg))
    assert res.status.ok and "BESTBUY_API_KEY" in res.status.note
    offer = BestBuyCollector(src)._to_offer({
        "sku": 6575404, "name": "Samsung - 990 PRO 2TB Internal SSD PCIe Gen 4x4 NVMe", "manufacturer": "Samsung",
        "modelNumber": "MZ-V9P2T0B/AM", "upc": "887276643832", "salePrice": 149.99, "regularPrice": 219.99,
        "url": "https://www.bestbuy.com/site/6575404.p", "image": "https://pisces.bbystatic.com/x.jpg",
        "onlineAvailability": True, "freeShipping": True, "shippingWeight": "0.2 pounds",
        "categoryPath": [{"name": "Computers & Tablets"}, {"name": "Hard Drives & Storage"}]}, _ctx(cfg))
    assert offer.price_kind == PriceKind.CONFIRMED and offer.reference_kind == ReferenceKind.REGULAR_PRICE
    assert offer.weight_kg == pytest.approx(0.09) and offer.gtin == "887276643832"
    assert "Laptops*" in build_query_url("onSale=true&categoryPath.name=Laptops", "K", 10)


def test_generic_store_jsonld():
    html = """<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
      {"@type":"Product","name":"Samsung 990 PRO 2TB","gtin13":"8806094215038","brand":{"name":"Samsung"},
       "offers":{"@type":"Offer","price":"169.90","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}]}
    </script></html>"""
    p = extract_jsonld_product(html)
    assert p["name"] == "Samsung 990 PRO 2TB" and p["offers"]["price"] == "169.90"


def test_retailer_normalization(cfg):
    assert normalize_retailer("www.aliexpress.com", cfg) == "AliExpress"
    assert normalize_retailer("Media Markt", cfg) == "MediaMarkt"
    assert normalize_retailer("Woot! An Amazon Company", cfg) == "Woot"
    assert normalize_retailer("Some Indie Shop", cfg) == "Some Indie Shop"
