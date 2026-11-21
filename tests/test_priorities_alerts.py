"""Priorities, instant alerts, Telegram, customs planner, DB migration."""
from datetime import timedelta

import httpx
import pytest

from app.analysis.ranking import deal_priority, watch_matches
from app.notifications import gmail
from app.notifications.telegram import _chunks, format_alerts, send_telegram
from app.pipeline import run_once
from tests.conftest import make_transport


def names(title, cfg):
    return {w.name for w in watch_matches(title, cfg)}


@pytest.mark.parametrize("title,expected", [
    ("Sony PlayStation 5 Slim Konsole Disc Edition", {"PlayStation 5"}),
    ("PS5 Pro Konsole 2TB", {"PlayStation 5 Pro"}),
    ("Ghost of Yōtei (PS5)", {"Игры PlayStation"}),
    ("Marvel's Wolverine for PS5", {"Игры PlayStation"}),
    ("DualSense Wireless Controller PS5", set()),
    ("PS5 Pro Enhanced: Gran Turismo 7 (PS5)", {"Игры PlayStation"}),
    ("PlayStation 5 Pro 2TB", {"PlayStation 5 Pro"}),
    ("Apple MacBook Air 13\" M4 16GB 512GB", {"MacBook Air"}),
    ("Apple MacBook Pro 14\" M5 Pro", {"MacBook Pro"}),
    ("Xbox Series X Console 1TB", {"Xbox Series X"}),
    ("Xbox Series X Wireless Controller", set()),
    ("MacBook Air Hülle 13 Zoll", set()),
    ("Apple iPad Air 11\" M3 128GB", {"iPad", "Планшеты Android"} - {"Планшеты Android"}),
    ("Samsung Galaxy Tab S10 FE 128GB", {"Планшеты Android"}),
    ("Apple AirPods Pro 3", {"AirPods и наушники"}),
])
def test_watchlist_matching(cfg, title, expected):
    assert names(title, cfg) == expected


def test_deal_priority(cfg):
    ws = watch_matches("PS5 Pro Konsole", cfg)
    assert deal_priority("gaming", ws, cfg) == 1
    assert deal_priority("kitchen", [], cfg) == 3
    assert deal_priority("clothing", [], cfg) is None


def _fake_smtp(monkeypatch, cfg, store):
    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, msg): store.append(msg)

    real = gmail.send_email
    monkeypatch.setattr("app.pipeline.send_email", lambda *a, **k: real(*a, **k, smtp_factory=FakeSMTP))
    cfg.secrets.gmail_address = "vahehakobyanv@gmail.com"
    cfg.secrets.gmail_app_password = "x" * 16
    cfg.settings.email.quiet_hours = []
    cfg.settings.email.email_every_hours = 4


def test_instant_alert_email_between_reports_and_no_repeat(cfg, now, tmp_path, monkeypatch):
    sent = []
    _fake_smtp(monkeypatch, cfg, sent)
    t = make_transport()
    cfg.settings.alerts.enabled = False
    run_once(cfg, send=True, transport=t, now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert len(sent) == 1 and "Топ-" in sent[0]["Subject"]                    # regular report

    cfg.settings.alerts.enabled = True
    cfg.settings.alerts.min_score = 0                                          # force alerts
    cfg.settings.alerts.max_per_message = 500
    out = run_once(cfg, send=True, transport=t, now=now + timedelta(hours=1), report_dir=tmp_path,
                   sleep=lambda s: None)
    assert not out.email_sent and out.alert_email_sent and len(sent) == 2      # alert only
    assert sent[1]["Subject"].startswith("🔥 Срочно")
    html = sent[1].get_body(("html",)).get_content()
    assert "ВАЖНЫЕ ОПОВЕЩЕНИЯ" in html and "ТОП-10" not in html

    out = run_once(cfg, send=True, transport=t, now=now + timedelta(hours=2), report_dir=tmp_path,
                   sleep=lambda s: None)
    assert out.alerts == 0 and len(sent) == 2                                  # same prices: no repeat


def test_no_alerts_at_night(cfg, now, tmp_path, monkeypatch):
    sent = []
    _fake_smtp(monkeypatch, cfg, sent)
    cfg.settings.email.quiet_hours = [0, 24]
    cfg.settings.alerts.min_score = 0
    out = run_once(cfg, send=True, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert not sent and not out.alert_email_sent


def test_telegram_sent_with_report_and_alerts(cfg, now, tmp_path, monkeypatch):
    sent, tg = [], []
    _fake_smtp(monkeypatch, cfg, sent)
    monkeypatch.setattr("app.pipeline.send_telegram", lambda tok, chat, text: tg.append(text))
    cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id = "T", "1"
    cfg.settings.alerts.min_score = 0
    out = run_once(cfg, send=True, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    assert out.telegram_sent and len(tg) == 2
    assert tg[0].startswith("🔥 <b>Срочно") and "📬" in tg[1]


def test_telegram_api_call_and_chunking():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"ok": True})

    send_telegram("TOKEN", "42", "hello <b>x</b>", transport=httpx.MockTransport(handler))
    assert calls[0].url.path == "/botTOKEN/sendMessage"
    assert b'"chat_id":"42"' in calls[0].content.replace(b" ", b"")
    long = "\n\n".join(["x" * 1000] * 10)
    parts = _chunks(long)
    assert len(parts) >= 3 and all(len(p) <= 4000 for p in parts)


def test_report_has_priorities_and_customs_planner(cfg, now, tmp_path):
    out = run_once(cfg, send=False, transport=make_transport(), now=now, report_dir=tmp_path, sleep=lambda s: None)
    html = out.report_path.read_text()
    assert "ВАШИ ПРИОРИТЕТЫ" in html and "PlayStation, Xbox и MacBook" in html and "Дом и кухня" in html
    assert "ПЛАНИРОВЩИК ТАМОЖНИ €200" in html
    assert "БЕЗ ПОШЛИНЫ" in html


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    from app.database.repository import make_engine
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notifications (product_id VARCHAR(64) PRIMARY KEY, first_sent_at DATETIME, "
                "last_sent_at DATETIME, best_price_usd_sent FLOAT, times_sent INTEGER, alerted BOOLEAN)")
    con.commit()
    con.close()
    make_engine(db).dispose()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(notifications)")}
    assert {"alerted_price_usd", "alerted_at"} <= cols


@pytest.mark.parametrize("title", [
    "Apple MacBook Black Friday Deals at Best Buy",
    "Werkzeug & Heimwerken Sammeldeal: Wera, Metabo, HAZET",
])
def test_roundups_are_not_products(cfg, title):
    from app.analysis.categorize import NON_PRODUCT, categorize
    assert categorize(title, None, cfg.categories) == NON_PRODUCT


def test_game_mentioning_ps5_pro_is_not_the_console(cfg):
    t = "Mortal Kombat 1 Definitive Edition (PS5 / PS5 Pro Enhanced)"
    assert "PlayStation 5 Pro" not in names(t, cfg) and "Игры PlayStation" in names(t, cfg)
