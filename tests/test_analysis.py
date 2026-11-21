from datetime import date, datetime, timedelta

import pytest

from app.analysis.categorize import NON_PRODUCT, categorize
from app.analysis.deal_scoring import ScoreInput, classify, discount_info, score_deal
from app.analysis.predictions import predict
from app.analysis.price_history import compute_stats, pct_below
from app.models.schemas import DeliveryEstimate, HistoryStats, Prediction, ReferenceKind

NOW = datetime(2026, 11, 19, 10)
BF, CM = date(2026, 11, 27), date(2026, 11, 30)


# ------------------------------------------------------------------ discounts / history
def test_pct_below():
    assert pct_below(75, 100) == pytest.approx(0.25)
    assert pct_below(120, 100) == pytest.approx(-0.2)
    assert pct_below(50, None) is None
    assert pct_below(50, 0) is None


def test_no_history_means_unavailable_never_invented():
    st = compute_stats([], "EUR", NOW)
    assert st.lowest is None and st.avg_30d is None and st.avg_90d is None and st.avg_180d is None
    assert not st.available


def test_short_history_does_not_claim_30_day_average():
    obs = [(NOW - timedelta(days=d), 100.0 - d) for d in range(3)]
    st = compute_stats(obs, "EUR", NOW)
    assert st.lowest == 98.0
    assert st.avg_30d is None  # only 2 days of history


def test_history_stats_averages_and_record_low():
    obs = [(NOW - timedelta(days=d), 100.0) for d in range(1, 40)] + [(NOW, 80.0)]
    st = compute_stats(obs, "EUR", NOW, min_days=3)
    assert st.avg_30d == pytest.approx((29 * 100 + 80) / 30, rel=1e-3)
    assert st.avg_90d is not None          # 39 days of history
    assert st.avg_180d is None             # < 90 days
    assert st.prior_lowest == 100.0
    assert st.lowest == 80.0


def test_trend_and_last_year_black_friday():
    obs = [(NOW - timedelta(days=d), 100 + d) for d in range(0, 14)]       # falling price
    obs += [(datetime(2025, 11, 28, 12), 70.0)]                               # recorded last BF
    st = compute_stats(obs, "USD", NOW, black_friday=BF)
    assert st.trend_pct_per_week < 0
    assert st.last_year_bf_price == 70.0


# ------------------------------------------------------------------ predictions
def test_prediction_heuristic_is_low_confidence_and_maybe_wait():
    p = predict(100, "EUR", HistoryStats(currency="EUR"), _cat(7), NOW.date(), BF, CM)
    assert p.confidence == "low"
    assert p.black_friday_price == pytest.approx(93)
    assert p.advice == "MAYBE WAIT"


def test_prediction_already_discounted_buy_now():
    p = predict(100, "EUR", HistoryStats(currency="EUR"), _cat(7), NOW.date(), BF, CM, advertised_discount=0.3)
    assert p.advice == "BUY NOW"   # half the room left: 3.5 % < 5 %


def test_prediction_uses_last_year_with_high_confidence():
    st = HistoryStats(currency="USD", last_year_bf_price=80)
    p = predict(100, "USD", st, _cat(5), NOW.date(), BF, CM)
    assert p.confidence == "high" and p.black_friday_price == 80 and p.advice == "WAIT"


def test_prediction_after_season():
    p = predict(100, "USD", HistoryStats(currency="USD"), _cat(5), date(2026, 12, 5), BF, CM)
    assert p.advice == "NO DATA" and p.black_friday_price is None


# ------------------------------------------------------------------ scoring
def _cat(drop):
    from app.config import CategoryCfg
    return CategoryCfg(label="x", keywords=[], bf_extra_drop_pct=drop, cm_extra_drop_pct=2)


def _inp(**kw):
    base = dict(price=70, price_usd=75, reference_price=100, reference_kind=ReferenceKind.NEXT_BEST_PRICE,
                stats=HistoryStats(currency="EUR"),
                prediction=Prediction(currency="EUR", expected_further_drop_pct=2, advice="BUY NOW", confidence="low"),
                delivery=DeliveryEstimate(route="r", lines=[], total_usd=85, total_amd=30000, complete=True,
                                          weight_kg=1, weight_is_estimate=True),
                retailer_reliability=0.95, availability="unknown", community_score=300, staff_pick=False,
                confirmed_price=False)
    base.update(kw)
    return ScoreInput(**base)


def test_score_is_bounded_and_uses_proxy_without_history(cfg):
    sb, disc = score_deal(_inp(), cfg.settings.scoring)
    assert 0 <= sb.total <= 100
    assert disc.actual_pct == pytest.approx(0.30)
    assert sb.history <= 30 * cfg.settings.scoring.history_proxy_max_share
    assert any("market proxy" in n for n in sb.notes)


def test_inflated_list_price_is_ignored(cfg):
    disc = discount_info(_inp(price=20, reference_price=100, reference_kind=ReferenceKind.LIST_PRICE), cfg.settings.scoring)
    assert disc.suspicious and disc.actual_pct is None


def test_list_price_less_credible_than_next_best(cfg):
    a, _ = score_deal(_inp(reference_kind=ReferenceKind.NEXT_BEST_PRICE), cfg.settings.scoring)
    b, _ = score_deal(_inp(reference_kind=ReferenceKind.LIST_PRICE), cfg.settings.scoring)
    assert a.discount > b.discount


def test_missing_data_gives_zero_points(cfg):
    sb, _ = score_deal(_inp(reference_price=None, reference_kind=ReferenceKind.NONE, delivery=None,
                            community_score=None,
                            prediction=Prediction(currency="EUR", advice="NO DATA")), cfg.settings.scoring)
    assert sb.discount == 0 and sb.delivered_cost == 0 and sb.prediction == 0 and sb.history == 0


def test_own_history_record_low_scores_high(cfg):
    st = HistoryStats(currency="EUR", avg_30d=100, avg_90d=100, prior_lowest=90, lowest=70, distinct_days=40)
    sb, disc = score_deal(_inp(stats=st), cfg.settings.scoring)
    assert sb.history == 30
    assert disc.basis.startswith("нашей средней")
    assert "record low in own history" in sb.notes


def test_incomplete_delivery_is_penalised(cfg):
    full, _ = score_deal(_inp(), cfg.settings.scoring)
    d = _inp().delivery.model_copy(update={"complete": False})
    part, _ = score_deal(_inp(delivery=d), cfg.settings.scoring)
    assert part.delivered_cost < full.delivered_cost


@pytest.mark.parametrize("score,label", [(95, "EXCELLENT"), (90, "EXCELLENT"), (85, "VERY GOOD"), (70, "GOOD"),
                                         (55, "NORMAL"), (49.9, "NOT INTERESTING")])
def test_classify(cfg, score, label):
    assert classify(score, cfg.settings.scoring) == label


# ------------------------------------------------------------------ categories
@pytest.mark.parametrize("title,feed,expected", [
    ("MSI GeForce RTX 5080 Gaming Trio", None, "pc_components"),
    ("Apple MacBook Air 13 M4", None, "laptops"),
    ("Samsung Galaxy S25 Ultra 256GB", None, "smartphones"),
    ("PlayStation 5 Pro Konsole", None, "gaming"),
    ("LG OLED65C5 OLED TV", None, "tv_electronics"),
    ("Philips Hue Starter Set", None, "smart_home"),
    ("Samsonite Spinner Koffer 75cm", None, "travel"),
    ("O2 Netz Sim Only: Allnet Flat 20GB", None, NON_PRODUCT),
    ("30€ Cashback für Stromwechsel", None, NON_PRODUCT),
    ("Some thing", "Home & Living", "home_furniture"),
])
def test_categorize(cfg, title, feed, expected):
    assert categorize(title, feed, cfg.categories) == expected


@pytest.mark.parametrize("title,hint,expected", [
    ("Ninja AF180EU Heißluftfritteuse", None, "kitchen"),
    ("De'Longhi Magnifica S Kaffeevollautomat", None, "kitchen"),
    ("WMF Topfset 5-teilig", None, "kitchen"),
    ("Xbox Series X 1TB Konsole", None, "gaming"),
    ("Nintendo Switch 2 + Mario Kart World Bundle", None, "gaming"),
    ("Samsung 65\" Neo QLED QN90F TV", None, "tv_electronics"),
    ("Bosch Serie 6 Waschmaschine 9 kg", None, "appliances"),
    ("Roborock Q7 Saugroboter", None, "appliances"),
    ("Vileda Bodenwischer Komplettset", "kitchen", "kitchen"),              # hint decides
    ("Vileda Bodenwischer Komplettset", None, "other"),
])
def test_categorize_new_categories_and_hint(cfg, title, hint, expected):
    assert categorize(title, None, cfg.categories, hint) == expected


def test_focus_categories_get_more_room(cfg):
    from app.notifications.report import _ordered_category_labels
    labels = list(_ordered_category_labels(cfg).items())
    assert labels[0][0] == cfg.settings.email.focus_categories[0] and labels[0][1].startswith("★")
