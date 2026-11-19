"""Command line entry point.

  python -m app.main run                 # full hourly run (collect, analyse, email)
  python -m app.main run --no-email      # everything except sending (writes data/last_report.html)
  python -m app.main test-email          # send a small test email to check Gmail settings
  python -m app.main stats               # print database statistics
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.config import load_config
from app.utils.logging_setup import setup_logging

log = logging.getLogger("app")


def cmd_run(args) -> int:
    from app.pipeline import run_once

    cfg = load_config()
    outcome = run_once(cfg, send=not args.no_email)
    log.info("run finished", extra={"ctx_run": outcome.run_id, "ctx_offers": outcome.offers,
                                    "ctx_deals": outcome.deals, "ctx_email_sent": outcome.email_sent,
                                    "ctx_errors": outcome.errors, "ctx_subject": outcome.subject})
    for st in outcome.statuses:
        print(f"  {'OK ' if st.ok else 'ERR'} {st.source:<18} offers={st.offers:<4} skipped={st.skipped:<4} "
              f"{st.note or st.error or ''}")
    print(f"deals ranked: {outcome.deals} | email sent: {outcome.email_sent} | report: {outcome.report_path}")
    if outcome.errors:
        print("errors:", *outcome.errors, sep="\n  ")
    # Exit non-zero only if the email was required and failed: the workflow still
    # saves the database (that step runs with `if: always()`).
    if outcome.email_skipped:
        print(f"email not due: {outcome.email_skipped}")
    if not args.no_email and not outcome.email_sent and not outcome.email_skipped:
        if any("EmailNotConfigured" in e for e in outcome.errors):
            # Secrets not added yet: data is still collected and saved; don't turn every run red.
            print("::warning::Email not sent: add the GMAIL_APP_PASSWORD secret (see README).")
            return 0
        return 2
    return 0


def cmd_test_email(_args) -> int:
    from app.notifications.gmail import send_email

    cfg = load_config()
    s = cfg.secrets
    send_email(s.gmail_address or cfg.settings.email.sender, s.gmail_app_password,
               s.email_to or cfg.settings.email.recipients, "Black Friday Hunter | test email",
               "<p>✅ Gmail settings work. Hourly reports will arrive at this address.</p>",
               "Gmail settings work. Hourly reports will arrive at this address.")
    print("test email sent")
    return 0


def cmd_stats(_args) -> int:
    from sqlalchemy import func, select

    from app.database.models import Listing, PriceObservation, Product, Run
    from app.database.repository import make_engine, make_session_factory

    cfg = load_config()
    engine = make_engine(cfg.db_path)
    with make_session_factory(engine)() as s:
        for model in (Product, Listing, PriceObservation, Run):
            print(f"{model.__tablename__:<20} {s.scalar(select(func.count()).select_from(model))}")
        first = s.scalar(select(func.min(PriceObservation.observed_at)))
        last = s.scalar(select(func.max(PriceObservation.observed_at)))
        print(f"history from {first} to {last}")
        for r in s.scalars(select(Run).order_by(Run.started_at.desc()).limit(5)):
            print(f"run {r.id} {r.started_at} {r.status} offers={r.offers_seen} email={r.email_sent} {r.error or ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bf-deal-hunter")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--plain-logs", action="store_true", help="human-readable logs instead of JSON")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run")
    p_run.add_argument("--no-email", action="store_true")
    sub.add_parser("test-email")
    sub.add_parser("stats")
    args = parser.parse_args(argv)
    setup_logging(args.log_level, json_format=not args.plain_logs)
    handlers = {"run": cmd_run, "test-email": cmd_test_email, "stats": cmd_stats, None: cmd_run}
    if args.cmd is None:
        args.no_email = False
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
