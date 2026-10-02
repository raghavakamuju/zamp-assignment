"""Sends the approved draft as a real email.

Only ever called from the admin-approval endpoint (app/main.py) -- this is the
one place in the whole system that actually sends anything.

Two transports, picked automatically:
- Resend (HTTPS API, port 443) -- used if RESEND_API_KEY is set. Required on
  Render's free tier: Render blocks outbound traffic on SMTP ports
  (25/465/587) by silently dropping packets, which makes raw SMTP hang
  rather than fail -- see https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports
- Gmail SMTP -- used otherwise (e.g. local dev, or any host that doesn't
  block SMTP ports). Has an explicit timeout so a blocked/unreachable port
  fails within seconds with a clear error instead of hanging indefinitely.
"""
import os
import smtplib
from email.message import EmailMessage

import httpx

_SMTP_TIMEOUT_SECONDS = 15


def send_email(to_email: str, subject: str, body: str) -> None:
    if os.environ.get("RESEND_API_KEY"):
        _send_via_resend(to_email, subject, body)
    else:
        _send_via_smtp(to_email, subject, body)


def _send_via_resend(to_email: str, subject: str, body: str) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    # Resend's shared onboarding@resend.dev sender works with no domain setup,
    # but can only send to the email address your Resend account was created
    # with until you verify your own sending domain.
    sender = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [to_email], "subject": subject, "text": body},
        timeout=15.0,
    )
    resp.raise_for_status()


def _send_via_smtp(to_email: str, subject: str, body: str) -> None:
    sender = os.environ["SMTP_SENDER_EMAIL"]
    app_password = os.environ["SMTP_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)
