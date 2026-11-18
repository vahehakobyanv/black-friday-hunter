import pytest

from app.shipping.currency import FxTable, parse_cba, parse_open_er
from app.utils.text import contains_phrase, parse_price
from tests.conftest import fixture_text


@pytest.mark.parametrize("text,value,cur", [
    ("£241.92", 241.92, "GBP"),
    ("76,76€", 76.76, "EUR"),
    ("1.299,00 €", 1299.0, "EUR"),
    ("1.299€", 1299.0, "EUR"),
    ("$1,299.99", 1299.99, "USD"),
    ("£1,299", 1299.0, "GBP"),
    ("EUR 12", 12.0, "EUR"),
    ("free", None, None),
    ("0,00€", None, "EUR"),
])
def test_parse_price(text, value, cur):
    assert parse_price(text) == (value, cur)


def test_contains_phrase_word_boundaries():
    assert contains_phrase("Samsung 990 PRO 2TB NVMe", "990 pro 2tb")
    assert not contains_phrase("Samsung 9900 PRO", "990 pro")
    assert contains_phrase("Kühlschrank XL", "kuhlschrank")


def test_parse_cba_fixture():
    rate_date, rates = parse_cba(fixture_text("cba_rates.xml"))
    assert rate_date == "2026-11-18"
    assert rates["USD"] == pytest.approx(360.41)
    assert rates["EUR"] == pytest.approx(395.37)
    assert rates["GBP"] == pytest.approx(474.73)


def test_parse_open_er():
    data = {"rates": {"USD": 1, "AMD": 360.0, "EUR": 0.9, "GBP": 0.75},
            "time_last_update_utc": "Wed, 18 Nov 2026 00:02:31 +0000"}
    d, rates = parse_open_er(data)
    assert d == "2026-11-18"
    assert rates["USD"] == 360.0
    assert rates["EUR"] == pytest.approx(400.0)
    assert "AMD" not in rates


def test_currency_conversion(fx):
    assert fx.to_usd(100, "USD") == 100
    assert fx.to_usd(100, "EUR") == pytest.approx(100 * 395.37 / 360.41)
    assert fx.to_amd(1, "GBP") == pytest.approx(474.73)
    assert fx.convert(1000, "AMD", "USD") == pytest.approx(1000 / 360.41)
    assert fx.to_usd(10, "JPY") is None          # unknown currency -> None, never a guess
    assert FxTable({}, None, "x").to_usd(5, "EUR") is None
