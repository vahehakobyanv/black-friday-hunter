from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.config import Secrets, load_config
from app.shipping.currency import FxTable

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("BF_DB_PATH", str(tmp_path / "test.db"))
    c = load_config(secrets=Secrets())
    c.settings.collection.per_host_delay_seconds = 0
    return c


@pytest.fixture
def fx() -> FxTable:
    # Real CBA rates from the fixture day (2026-11-18).
    return FxTable({"USD": 360.41, "EUR": 395.37, "GBP": 474.73}, "2026-11-18", "test")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 11, 19, 10, 0, 0)


def make_transport(fail_hosts: set[str] | None = None, blocked_hosts: set[str] | None = None) -> httpx.MockTransport:
    """Serve recorded real responses instead of the live web."""
    fail_hosts = fail_hosts or set()
    blocked_hosts = blocked_hosts or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host
        if host in fail_hosts:
            raise httpx.ConnectError("simulated outage", request=request)
        if host in blocked_hosts:
            return httpx.Response(403, text="forbidden")
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\nDisallow: /visit/\n")
        if "mydealz.de/rss" in url:
            return httpx.Response(200, text=fixture_text("mydealz_hot.xml"), headers={"content-type": "text/xml"})
        if "hotukdeals.com/rss" in url:
            return httpx.Response(200, text=fixture_text("hotukdeals_hot.xml"), headers={"content-type": "text/xml"})
        if "dealcatcher.com/rss" in url:
            return httpx.Response(200, text=fixture_text("dealcatcher.xml"), headers={"content-type": "text/xml"})
        if "9to5toys.com" in url:
            return httpx.Response(200, text=fixture_text("9to5toys.xml"), headers={"content-type": "application/rss+xml"})
        if "blackfriday.com/ads/" in url:
            return httpx.Response(200, text=fixture_text("bf_ad_walmart.html"), headers={"content-type": "text/html"})
        if "/deals/" in url:
            return httpx.Response(200, text=fixture_text("pepper_deal_page.html"), headers={"content-type": "text/html"})
        if "dealnews.com" in url:
            return httpx.Response(200, text=fixture_text("dealnews.xml"), headers={"content-type": "text/xml"})
        if "api.cba.am" in url:
            return httpx.Response(200, text=fixture_text("cba_rates.xml"), headers={"content-type": "text/xml"})
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)
