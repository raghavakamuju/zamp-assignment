"""Sends the approved draft as a real email via Gmail's SMTP server.

Only ever called from the admin-approval endpoint (app/main.py) -- this is the
one place in the whole system that actually sends anything. Credentials come
from environment variables, never hardcoded (see .env.example).
"""
import os
import smtplib
from email.message import EmailMessage


def send_email(to_email: str, subject: str, body: str) -> None:
    sender = os.environ["SMTP_SENDER_EMAIL"]
    app_password = os.environ["SMTP_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)
