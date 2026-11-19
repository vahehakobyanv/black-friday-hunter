"""One hourly run: collect → normalise/match → store history → score → report → email.

Failure rules:
- a failing source is logged and skipped; the others continue;
- data is committed after every source, so a later crash loses nothing;
- if analysis/rendering crashes, the previous saved report is emailed with a warning;
- the run record (and the database) is always saved.
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.analysis.categorize import NON_PRODUCT, OTHER, categorize
from app.analysis.ranking import build_deals, build_sections
from app.collectors.base import CollectContext
from app.collectors.registry import build_collectors
from app.config import Config
from app.database.models import Run
from app.database.repository import Repository, make_engine, make_session_factory
from app.models.schemas import CollectorStatus
from app.notifications.gmail import send_email
from app.notifications.report import RenderedReport, render_report
from app.shipping.currency import FxTable, load_rates
from app.utils.http import PoliteClient

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    run_id: int
    statuses: list[CollectorStatus]
    offers: int
    deals: int
    email_sent: bool
    report_path: Path | None
    subject: str | None
    errors: list[str] = field(default_factory=list)
    email_skipped: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def in_quiet_hours(cfg: Config, now: datetime) -> bool:
    q = cfg.settings.email.quiet_hours
    if len(q) != 2:
        return False
    hour = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(cfg.settings.app.timezone)).hour
    start, end = q
    return start <= hour < end if start < end else (hour >= start or hour < end)


EMAIL_SLACK = timedelta(minutes=20)  # tolerate GitHub start-time jitter


def should_send_email(cfg: Config, now: datetime, last_email: datetime | None) -> tuple[bool, str]:
    """Decide whether this run sends the report.

    Not during quiet hours; otherwise when at least `email_every_hours` passed
    since the last sent email (minus a little slack for late GitHub starts).
    The first run after quiet hours always sends, so mornings start on time."""
    if in_quiet_hours(cfg, now):
        return False, "quiet hours"
    every = cfg.settings.email.email_every_hours
    if last_email is None or every <= 1:
        return True, "due"
    if in_quiet_hours(cfg, last_email):  # can't happen normally, but be safe
        return True, "due"
    tz = ZoneInfo(cfg.settings.app.timezone)
    q = cfg.settings.email.quiet_hours
    if len(q) == 2:
        # a quiet period started after the last email -> first run after it sends
        local_last = last_email.replace(tzinfo=timezone.utc).astimezone(tz)
        local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
        quiet_start = local_now.replace(hour=q[0], minute=0, second=0, microsecond=0)
        if quiet_start > local_now:
            quiet_start -= timedelta(days=1)
        if local_last < quiet_start:
            return True, "first email after quiet hours"
    if now - last_email >= timedelta(hours=every) - EMAIL_SLACK:
        return True, "due"
    return False, f"next email due {every:g} h after the last one"


def run_once(cfg: Config, *, send: bool = True, transport: httpx.BaseTransport | None = None,
             now: datetime | None = None, report_dir: Path | None = None, sleep=None) -> RunOutcome:
    now = now or utcnow()
    engine = make_engine(cfg.db_path)
    Session = make_session_factory(engine)
    report_dir = report_dir or cfg.db_path.parent
    report_path = report_dir / "last_report.html"
    errors: list[str] = []
    statuses: list[CollectorStatus] = []
    offers_total = 0
    email_sent = False
    skipped_reason: str | None = None
    subject = None

    http_kw = {}
    if sleep is not None:
        http_kw["sleep"] = sleep
    http = PoliteClient(cfg.settings.collection.user_agent, cfg.settings.collection.timeout_seconds,
                        cfg.settings.collection.max_retries, cfg.settings.collection.per_host_delay_seconds,
                        transport=transport, **http_kw)

    with Session() as session:
        repo = Repository(session)
        run = repo.start_run(now)
        session.commit()
        run_id = run.id

        # ---------------------------------------------------------------- exchange rates
        try:
            fx = load_rates(cfg, http, repo, now)
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"exchange rates: {exc}")
            statuses.append(CollectorStatus(source="exchange_rates", ok=False, error=str(exc)[:200]))
            fx = FxTable({}, None, "unavailable", stale=True)

        # ---------------------------------------------------------------- collectors
        ctx = CollectContext(cfg=cfg, http=http, repo=repo, now=now)
        for collector in build_collectors(cfg):
            result = collector.run(ctx)
            status = result.status
            stored = 0
            for offer in result.offers:
                try:
                    category = categorize(offer.title, offer.feed_category, cfg.categories, offer.category_hint)
                    if category == NON_PRODUCT:
                        continue
                    product, _specs, _how = repo.find_or_create_product(offer, category, now)
                    if category != OTHER and product.category != category:
                        product.category = category
                    repo.upsert_listing(product, offer, now, fx.to_usd(offer.price, offer.currency),
                                        cfg.settings.history.unchanged_observation_every_hours)
                    session.commit()  # per offer: one bad item can never roll back the others
                    stored += 1
                except Exception as exc:  # bad single offer must not break the source
                    session.rollback()
                    log.warning("could not store offer %s/%s: %s", offer.source, offer.source_item_id, exc)
            # enrichment cache entries written by the collector
            try:
                session.commit()
            except Exception:
                session.rollback()
            status.offers = stored
            offers_total += stored
            statuses.append(status)
            log.info("source done", extra={"ctx_source": status.source, "ctx_ok": status.ok,
                                           "ctx_offers": status.offers, "ctx_error": status.error})
        http.close()

        # ---------------------------------------------------------------- analysis + report
        report: RenderedReport | None = None
        deals = []
        try:
            deals = build_deals(repo, cfg, fx, now, run_started=now)
            sections = build_sections(deals, cfg)
            last = repo.last_email_run()
            report = render_report(sections=sections, deals=deals, statuses=statuses, fx=fx, cfg=cfg, now=now,
                                   run_id=run_id, last_email=last.started_at if last else None,
                                   offers_count=offers_total)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report.html, encoding="utf-8")
            subject = report.subject
        except Exception as exc:
            log.exception("analysis/report failed")
            errors.append(f"report: {exc}")
            session.rollback()
            if report_path.exists():
                prev = report_path.read_text(encoding="utf-8")
                warn = ('<div style="background:#fee2e2;padding:12px;font-family:sans-serif;">'
                        "⚠ This hour's analysis failed; this is the PREVIOUS report. Error: "
                        f"{type(exc).__name__}</div>")
                report = RenderedReport(subject=f"{cfg.settings.app.name} | previous report (analysis error)",
                                        html=warn + prev, text="This hour's analysis failed; see HTML for the previous report.")
                subject = report.subject

        # ---------------------------------------------------------------- email
        if report is not None and send:
            last = repo.last_email_run()
            send_now, why = should_send_email(cfg, now, last.started_at if last else None)
            if not send_now:
                log.info("email skipped: %s", why)
                skipped_reason = why
            else:
                s = cfg.secrets
                recipients = s.email_to or cfg.settings.email.recipients
                try:
                    send_email(s.gmail_address or cfg.settings.email.sender, s.gmail_app_password, recipients,
                               report.subject, report.html, report.text)
                    email_sent = True
                    if deals:
                        sections_obj = build_sections(deals, cfg)
                        notified = {d.product.id: d for d in sections_obj.top + sections_obj.opportunities
                                    + sections_obj.alerts + sections_obj.waiting}
                        for lst in sections_obj.by_category.values():
                            notified.update({d.product.id: d for d in lst})
                        alert_ids = {d.product.id for d in sections_obj.alerts}
                        for pid, d in notified.items():
                            repo.mark_notified(pid, d.best.price_usd, now, alerted=pid in alert_ids)
                except Exception as exc:
                    log.error("email failed: %s", exc)
                    errors.append(f"email: {type(exc).__name__}: {exc}")

        # ---------------------------------------------------------------- finish
        try:
            repo.prune(now, cfg.settings.history.retention_days)
            run_row = session.get(Run, run_id)
            run_row.finished_at = utcnow()
            run_row.offers_seen = offers_total
            run_row.email_sent = email_sent
            run_row.collectors_json = json.dumps([st.model_dump() for st in statuses])
            run_row.status = "ok" if not errors and all(st.ok for st in statuses) else "partial"
            run_row.error = "\n".join(errors)[:2000] or None
            session.commit()
        except Exception:
            session.rollback()
            log.error("could not finalise run: %s", traceback.format_exc())
    engine.dispose()
    return RunOutcome(run_id=run_id, statuses=statuses, offers=offers_total, deals=len(deals), email_sent=email_sent,
                      report_path=report_path if report_path.exists() else None, subject=subject, errors=errors,
                      email_skipped=skipped_reason)
