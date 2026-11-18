"""Send the report through Gmail SMTP with an App Password."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class EmailNotConfigured(RuntimeError):
    pass


def build_message(sender: str, recipients: list[str], subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"Black Friday Hunter <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="bf-deal-hunter.local")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_email(sender: str | None, app_password: str | None, recipients: list[str], subject: str, html: str,
               text: str, retries: int = 3, smtp_factory=smtplib.SMTP_SSL) -> None:
    if not sender or not app_password:
        raise EmailNotConfigured("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not set")
    if not recipients:
        raise EmailNotConfigured("no recipients configured")
    msg = build_message(sender, recipients, subject, html, text)
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with smtp_factory(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(sender, app_password)
                smtp.send_message(msg)
            log.info("email sent to %s: %s", recipients, subject)
            return
        except smtplib.SMTPAuthenticationError:
            raise  # wrong password: retrying will not help
        except (smtplib.SMTPException, OSError) as exc:
            last = exc
            log.warning("email attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(min(5 * attempt, 20))
    assert last is not None
    raise last
