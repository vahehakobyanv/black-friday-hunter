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
    for section in ("TOP", "BEST OPPORTUNITIES", "CATEGORY SUMMARY", "PRICE PREDICTIONS", "SOURCES"):
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
    assert {s.source for s in failed} == {"mydealz_hot", "mydealz_new"}
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
    out = run_once(cfg, send=True, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert not out.email_sent
    assert any("GMAIL_APP_PASSWORD" in e for e in out.errors)
    assert _count(cfg, Product) > 0      # collected data was still stored
