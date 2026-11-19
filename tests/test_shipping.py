import pytest

from app.config import ForwarderCfg
from app.shipping.calculator import ShipInput, chargeable_weight, customs_duty_eur, estimate_delivery


def test_customs_under_threshold_is_free(cfg):
    duty, note = customs_duty_eur(150, 2, cfg)
    assert duty == 0 and "duty-free" in note


def test_customs_value_over_threshold(cfg):
    duty, _ = customs_duty_eur(500, 2, cfg)          # 15 % of (500 - 200)
    assert duty == pytest.approx(45)


def test_customs_weight_minimum_applies(cfg):
    # value excess 10 € -> 1.5 €, weight excess 9 kg -> 18 € minimum
    duty, _ = customs_duty_eur(210, 40, cfg)
    assert duty == pytest.approx(18)


def test_chargeable_weight_rounding_and_volumetric():
    fw = ForwarderCfg(id="x", name="X", origin="US", rate_per_kg=10, rounding_kg=0.5, volumetric_divisor=5000)
    w, note = chargeable_weight(1.2, fw, (40, 30, 20))     # volumetric 4.8 kg
    assert w == pytest.approx(5.0) and "volumetric" in note
    fw2 = ForwarderCfg(id="y", name="Y", origin="US", rate_per_kg=10, min_chargeable_kg=1, rounding_kg=0.1)
    assert chargeable_weight(0.33, fw2, None)[0] == pytest.approx(1.0)


def test_us_delivery_complete_with_free_shipping(cfg, fx):
    est = estimate_delivery(ShipInput(price=100, currency="USD", region="US", retailer="Amazon", category="laptops",
                                      free_shipping=True), fx, cfg)
    # Onex US: 6000 AMD/kg x 3.5 kg (estimated laptop weight) = 21000 AMD
    forwarding = next(l for l in est.lines if l.label == "Forwarding to Yerevan")
    assert forwarding.amount_usd == pytest.approx(21000 / 360.41, abs=0.01)
    assert est.weight_is_estimate
    assert est.complete
    assert est.total_usd == pytest.approx(100 + 21000 / 360.41, abs=0.02)
    assert est.total_amd == pytest.approx(est.total_usd * 360.41, abs=2)


def test_unknown_rate_is_marked_incomplete_not_guessed(cfg, fx):
    next(f for f in cfg.shipping.forwarders if f.origin == "DE").rate_per_kg = None
    est = estimate_delivery(ShipInput(price=100, currency="EUR", region="DE", retailer="MediaMarkt",
                                      category="laptops", free_shipping=True), fx, cfg)
    assert not est.complete
    forwarding = next(l for l in est.lines if l.label == "Forwarding to Yerevan")
    assert forwarding.amount_usd is None
    assert any("not configured" in w for w in est.warnings)


def test_unknown_domestic_shipping_marked(cfg, fx):
    est = estimate_delivery(ShipInput(price=50, currency="USD", region="US", retailer="Walmart", category="books"),
                            fx, cfg)
    dom = next(l for l in est.lines if l.label == "Shipping to forwarder")
    assert dom.amount_usd is None and not est.complete


def test_expensive_item_pays_customs(cfg, fx):
    est = estimate_delivery(ShipInput(price=1000, currency="USD", region="US", retailer="Best Buy",
                                      category="laptops", free_shipping=True, weight_kg=2.0), fx, cfg)
    customs = next(l for l in est.lines if l.label == "Armenian customs")
    value_eur = 1000 * 360.41 / 395.37
    expected_usd = (value_eur - 200) * 0.15 * 395.37 / 360.41
    assert customs.amount_usd == pytest.approx(expected_usd, abs=0.02)
    assert not est.weight_is_estimate


def test_germany_and_uk_use_configured_rate(cfg, fx):
    for region, cur in (("DE", "EUR"), ("UK", "GBP")):
        est = estimate_delivery(ShipInput(price=100, currency=cur, region=region, retailer="Amazon",
                                          category="laptops", free_shipping=True), fx, cfg)
        assert est.complete
        fwd = next(l for l in est.lines if l.label == "Forwarding to Yerevan")
        assert fwd.amount_usd == pytest.approx(3.5 * 6000 / 360.41, abs=0.01)
