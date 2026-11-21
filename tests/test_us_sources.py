from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.collectors.base import CollectContext
from app.collectors.deal_sites import (
    parse_ad_page,
    parse_dealcatcher_title,
    parse_editorial_title,
    retailer_from_links,
)
from app.collectors.registry import build_collectors
from app.pipeline import run_once
from app.utils.http import PoliteClient
from tests.conftest import fixture_text, make_transport


def _run(cfg, source_id):
    http = PoliteClient(cfg.settings.collection.user_agent, per_host_delay=0, transport=make_transport(),
                        sleep=lambda s: None)
    ctx = CollectContext(cfg=cfg, http=http, repo=None, now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    col = next(c for c in build_collectors(cfg) if c.id == source_id)
    return col.run(ctx)


def test_dealcatcher_title():
    assert parse_dealcatcher_title("Amazon - Marshall Middleton Portable Bluetooth Speaker $149.99") == (
        "Amazon", "Marshall Middleton Portable Bluetooth Speaker", 149.99)
    assert parse_dealcatcher_title("Lululemon - Lululemon Black Friday Sale! ") is None


@pytest.mark.parametrize("title,expected", [
    ("Most affordable M5 MacBook Air drops lowest price in months at $1,050 for Black Friday ($250 off)",
     ("Most affordable M5 MacBook Air", 1050.0, 250.0)),
    ("Govee’s colorful smart hygrometer 3-pack with gateway plunges to $63",
     ("Govee’s colorful smart hygrometer 3-pack with gateway", 63.0, None)),
    # "$70 off" is a discount, never a price
    ("All Apple Watch Series 12 models hit Black Friday all-time lows at $70 off", None),
    # roundups / several products
    ("Rare Black Friday deal hits 2026 Sonos Play Portable Speaker at $60 off, more", None),
    ("Anker 2026 Black Friday sale kicks off with up to 46% off chargers from just $16", None),
    ("Power Starlink Mini in your ride with this 3-in-1 car charger, now $20", None),
])
def test_editorial_title(title, expected):
    assert parse_editorial_title(title) == expected


def test_retailer_from_links():
    assert retailer_from_links('<a href="https://9to5toys.com/x">a</a><a href="https://link.amazon/abc">b</a>') == "Amazon"
    assert retailer_from_links('<a href="https://bestbuycreators.7tiv.net/c/1">b</a>') == "Best Buy"
    assert retailer_from_links('<a href="https://www.awin1.com/x">b</a>') is None


def test_ad_page_itemlist():
    items = parse_ad_page(fixture_text("bf_ad_walmart.html"))
    assert len(items) == 12
    airpods = next(i for i in items if "AirPods 4" in i["name"])
    assert airpods["price"] == 89 and airpods["url"].startswith("https://www.walmart.com/")


def test_new_collectors_on_recorded_feeds(cfg):
    dc = _run(cfg, "dealcatcher")
    assert dc.status.ok and dc.offers
    assert all(o.region == "US" and o.currency == "USD" for o in dc.offers)
    assert not any("Sale!" in o.title for o in dc.offers)
    toys = _run(cfg, "9to5toys")
    assert toys.status.ok and toys.offers
    air = next(o for o in toys.offers if "MacBook Air" in o.title)
    assert (air.price, air.reference_price, air.retailer) == (1050.0, 1300.0, "Amazon")
    ads = _run(cfg, "bf_ads")
    assert ads.status.ok and ads.offers
    assert all(o.extra.get("ad_scan") for o in ads.offers)
    assert {o.retailer for o in ads.offers} >= {"Walmart"}


def test_ads_section_in_report(cfg, now, tmp_path):
    out = run_once(cfg, send=False, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    html = out.report_path.read_text()
    assert "РЕКЛАМНЫЕ КАТАЛОГИ BLACK FRIDAY" in html
    assert "https://blackfriday.com/ads/black-friday/walmart" in html


def test_target_ignores_cheap_matches(cfg):
    from app.analysis.ranking import watch_matches
    ps5 = next(w for w in cfg.watchlist.watchlist if w.name == "PlayStation 5")
    assert ps5.target_price_usd == 500 and ps5.min_price_usd == 300
    xbox = [w.name for w in watch_matches("Xbox Series X Console 1TB Disc", cfg)]
    assert xbox == ["Xbox Series X"]


def test_region_filter_keeps_only_usa(cfg, now, tmp_path):
    cfg.settings.filters.regions = ["US"]
    out = run_once(cfg, send=False, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    html = out.report_path.read_text()
    assert out.deals > 0
    assert "(DE)" not in html and "(UK)" not in html and "(US)" in html
