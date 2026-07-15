"""
mailer.py — Transactional email over SMTP (aiosmtplib).

Sends the three security emails: verify-your-address, password reset, and the
login OTP. Transport only — what goes *in* the messages, and the one-time
secrets they carry, live in services/verification.py.

On Nodemailer
-------------
Nodemailer is a Node.js library and this backend is Python, so it cannot be
used here. aiosmtplib is its direct equivalent: same SMTP relays (Gmail app
password, SendGrid, Mailtrap, SES), and async so a send never blocks the event
loop the way stdlib smtplib would.

Dev without SMTP
----------------
With no relay configured outside production, messages are logged instead of
sent (see `settings.email_enabled`) so the whole flow is runnable on a fresh
clone. In production that fallback is refused: it would put login codes in the
logs and silently downgrade the security these emails exist to provide.
"""

import logging
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from config import settings

logger = logging.getLogger(__name__)


class EmailNotConfigured(Exception):
    """No SMTP relay is set up on this server."""


class EmailDeliveryError(Exception):
    """The relay was there but refused or dropped the message."""


def _assert_header_safe(value: str, field: str) -> None:
    """Reject CR/LF in anything that becomes a header.

    Addresses are validated on the way in (auth.EMAIL_RE forbids whitespace),
    so this is defence in depth against a smuggled header injection.
    """
    if "\r" in value or "\n" in value:
        raise EmailDeliveryError(f"Illegal newline in email {field}")


def _build(to: str, subject: str, text: str, html: str | None) -> EmailMessage:
    _assert_header_safe(to, "recipient")
    _assert_header_safe(subject, "subject")

    msg = EmailMessage()
    msg["From"] = formataddr((settings.smtp_from_name, settings.mail_from_address))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


async def send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
    """Deliver one message, or raise.

    Raises EmailNotConfigured / EmailDeliveryError so callers can decide what a
    failure means — for a login OTP it has to be fatal (no code, no login),
    while a best-effort notice can be swallowed.
    """
    if not settings.smtp_configured:
        if settings.is_production:
            raise EmailNotConfigured(
                "SMTP is not configured on this server, so this email cannot be sent."
            )
        # Dev fallback: show the operator what would have gone out, codes and all.
        logger.warning(
            "SMTP not configured — logging email instead of sending it.\n"
            f"  To:      {to}\n"
            f"  Subject: {subject}\n"
            f"{text}"
        )
        return

    msg = _build(to, subject, text, html)

    # Implicit TLS (465) and STARTTLS (587) are alternatives, not a stack —
    # aiosmtplib rejects being asked for both.
    tls_args = (
        {"use_tls": True, "start_tls": False}
        if settings.smtp_ssl
        else {"use_tls": False, "start_tls": settings.smtp_starttls}
    )

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            timeout=settings.smtp_timeout,
            **tls_args,
        )
    except (aiosmtplib.SMTPException, OSError) as e:
        # Never log the body — these carry reset links and login codes.
        logger.error(f"SMTP delivery to {to} failed: {type(e).__name__}: {e}")
        raise EmailDeliveryError("Could not send the email. Please try again.") from e

    logger.info(f"Sent '{subject}' to {to}")
