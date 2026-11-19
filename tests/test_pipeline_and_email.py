"""End-to-end runs against recorded real responses (no live web, no real email)."""
from datetime import timedelta

from sqlalchemy import func, select

from app.database.models import Listing, Notification, PriceObservation, Product, Run
from app.database.repository import make_engine, make_session_factory
from app.notifications import gmail
from app.pipeline import run_once
from tests.conftest import make_transport


def _count(cfg, model):
    eng = make_engine(cfg.db_path)
    with make_session_factory(eng)() as s:
        n = s.scalar(select(func.count()).select_from(model))
    eng.dispose()
    return n


def test_full_run_without_email(cfg, now, tmp_path):
    out = run_once(cfg, send=False, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert out.offers > 10 and out.deals > 5
    assert out.report_path and out.report_path.exists()
    html = out.report_path.read_text()
    for section in ("TOP", "BEST NEW OPPORTUNITIES", "CATEGORY SUMMARY", "PRICE PREDICTIONS", "SOURCES"):
        assert section in html
    assert "Delivered to Yerevan" in html and "Predicted Black Friday" in html
    assert out.subject.startswith("Black Friday Hunter | Top ")
    assert "19 November | 14:00" in out.subject          # 10:00 UTC = 14:00 Yerevan
    assert {s.source for s in out.statuses} >= {"mydealz_hot", "hotukdeals_hot", "dealnews"}


def test_database_persists_between_runs_without_duplicate_history(cfg, now, tmp_path):
    t = make_transport()
    run_once(cfg, send=False, transport=t, now=now, report_dir=tmp_path, sleep=lambda s: None)
    obs1, listings1 = _count(cfg, PriceObservation), _count(cfg, Listing)
    # One hour later, same prices -> no new observations (only every 6 h when unchanged)
    run_once(cfg, send=False, transport=t, now=now + timedelta(hours=1), report_dir=tmp_path, sleep=lambda s: None)
    assert _count(cfg, PriceObservation) == obs1
    assert _count(cfg, Listing) == listings1
    # Seven hours later -> one new observation per listing still offered
    run_once(cfg, send=False, transport=t, now=now + timedelta(hours=7), report_dir=tmp_path, sleep=lambda s: None)
    assert _count(cfg, PriceObservation) > obs1
    assert _count(cfg, Run) == 3


def test_failed_source_does_not_stop_report(cfg, now, tmp_path):
    out = run_once(cfg, send=False, transport=make_transport(fail_hosts={"www.mydealz.de"}), now=now,
                   report_dir=tmp_path, sleep=lambda s: None)
    failed = [s for s in out.statuses if not s.ok]
    assert {s.source for s in failed} == {"mydealz_hot", "mydealz_new", "mydealz_topics"}
    assert out.deals > 0
    assert "Sources failed this run" in out.report_path.read_text()


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        assert host == "smtp.gmail.com" and port == 465

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        assert pw == "abcdefghijklmnop"

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_email_is_sent_and_repeats_are_not_new(cfg, now, tmp_path, monkeypatch):
    FakeSMTP.sent = []
    real_send = gmail.send_email
    monkeypatch.setattr("app.pipeline.send_email",
                        lambda *a, **k: real_send(*a, **k, smtp_factory=FakeSMTP))
    cfg.secrets.gmail_address = "vahehakobyanv@gmail.com"
    cfg.secrets.gmail_app_password = "abcdefghijklmnop"
    cfg.settings.email.email_every_hours = 1
    cfg.settings.email.quiet_hours = []
    # Keep the data set small enough that email 1 shows every deal (then nothing is new in email 2).
    for src in cfg.stores.sources:
        if src.id in ("dealcatcher", "9to5toys", "bf_ads"):
            src.enabled = False
    t = make_transport()
    out1 = run_once(cfg, send=True, transport=t, now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert out1.email_sent and len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "vahehakobyanv@gmail.com"
    assert msg.get_body(("html",)) is not None and msg.get_body(("plain",)) is not None
    assert _count(cfg, Notification) > 0
    html1 = out1.report_path.read_text()
    assert ">NEW<" in html1

    out2 = run_once(cfg, send=True, transport=t, now=now + timedelta(hours=1), report_dir=tmp_path,
                    sleep=lambda s: None)
    assert out2.email_sent and len(FakeSMTP.sent) == 2   # an email every hour, even with nothing new
    html2 = out2.report_path.read_text()
    assert "SEEN BEFORE" in html2
    assert "no new deals" in out2.subject or "alert" in out2.subject


def test_missing_gmail_secret_fails_cleanly_but_saves_data(cfg, now, tmp_path):
    cfg.settings.email.quiet_hours = []
    out = run_once(cfg, send=True, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert not out.email_sent
    assert any("GMAIL_APP_PASSWORD" in e for e in out.errors)
    assert _count(cfg, Product) > 0      # collected data was still stored


def test_same_deal_from_two_feeds_is_one_offer(cfg, now, tmp_path):
    """mydealz 'hot' and 'new' feeds often contain the same thread."""
    from app.analysis.ranking import build_deals
    from app.database.repository import Repository
    from app.shipping.currency import FxTable

    run_once(cfg, send=False, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    eng = make_engine(cfg.db_path)
    with make_session_factory(eng)() as s:
        repo = Repository(s)
        d, rates, src = repo.latest_fx()
        deals = build_deals(repo, cfg, FxTable(rates, d, src), now, now)
        for deal in deals:
            urls = [deal.best.listing.url] + [o.listing.url for o in deal.others]
            assert len(urls) == len(set(urls)), deal.best.listing.title


def _utc_from_yerevan(h, m=20, day=19):
    from datetime import datetime
    return datetime(2026, 11, day, h, m) - timedelta(hours=4)   # Yerevan = UTC+4


def test_email_schedule_every_4_hours_with_night_off(cfg):
    from app.pipeline import should_send_email
    cfg.settings.email.email_every_hours = 4
    cfg.settings.email.quiet_hours = [0, 8]
    y = _utc_from_yerevan
    # simulate one day of hourly runs (Yerevan 00:20 .. 23:20), starting after an email at 20:20 the day before
    last = y(20, day=18)
    sent_at = []
    for hour in range(24):
        now = y(hour)
        ok, _ = should_send_email(cfg, now, last)
        if ok:
            sent_at.append(hour)
            last = now
    assert sent_at == [8, 12, 16, 20]


def test_email_schedule_tolerates_late_github_start(cfg):
    from app.pipeline import should_send_email
    cfg.settings.email.email_every_hours = 4
    cfg.settings.email.quiet_hours = [0, 8]
    y = _utc_from_yerevan
    assert should_send_email(cfg, y(12, 5), y(8, 22))[0]       # 3h43m later: still sends
    assert not should_send_email(cfg, y(11, 20), y(8, 22))[0]
    assert not should_send_email(cfg, y(3, 20), y(20, 22, day=18))[0]   # night


def test_skipped_email_still_collects_and_saves(cfg, now, tmp_path):
    cfg.settings.email.quiet_hours = [0, 24]     # always quiet
    out = run_once(cfg, send=True, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert not out.email_sent and out.email_skipped == "quiet hours" and not out.errors
    assert _count(cfg, Product) > 0
