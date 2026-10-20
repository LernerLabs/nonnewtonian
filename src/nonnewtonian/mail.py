"""Plain-text outbound mail for the optional /recover flow.

Synchronous with a 10s timeout, which is simpler than a background
thread. The cost is a timing side channel: a POST /recover for an address
that some class uses waits for the SMTP round trip, one that matches
nothing returns at once, so timing reveals whether an address has a class
here. Accepted: it reveals only that (no link, no class name), and the
/recover rate limits bound how fast anyone can probe. Errors propagate: callers log only type(exc).__name__, never the address or a
link. Not exercised against a real SMTP server in the test suite; tests
monkeypatch send().
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage


def send(config, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()  # header assignment rejects CR/LF (header injection)
    msg["From"] = config["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=10) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        smtp.send_message(msg)
