import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage

from .config import Settings, get_settings

log = logging.getLogger("mailer")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _send_brevo(s: Settings, to: str, name: str, subject: str, text: str) -> None:
    body = {"sender": {"email": s.mail_from}, "to": [{"email": to, "name": name}], "subject": subject, "textContent": text}
    req = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"api-key": s.brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        res.read()


def _send_smtp(s: Settings, to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.mail_from
    msg["To"] = to
    msg.set_content(text)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


def send_reset_link(to: str, name: str, link: str) -> None:
    s = get_settings()
    subject = "Reset your password"
    text = (
        f"Hi {name},\n\n"
        f"Use this link to set a new password. It works once and expires in 1 hour:\n\n{link}\n\n"
        "If you didn't ask for this, ignore this email. Your password stays the same.\n"
    )
    if not (s.brevo_api_key or s.smtp_host):
        log.warning("Mail not configured. Reset link for %s: %s", to, link)
        return
    try:
        if s.brevo_api_key:
            _send_brevo(s, to, name, subject, text)
        else:
            _send_smtp(s, to, subject, text)
    except Exception:  # runs in a background task: log it, never surface it to the caller
        log.exception("Could not send reset email to %s", to)
