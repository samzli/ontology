"""Outbound e-mail delivery for sign-up and sign-in verification codes.

Two transports are supported and selected from the environment, so the same
code path works on a laptop, in Docker and on Vercel:

* **Resend** — set ``RESEND_API_KEY`` (and optionally ``ONTI_MAIL_FROM``).
  Preferred when present because it is a plain HTTPS call and therefore works
  on hosts that block outbound SMTP ports.
* **SMTP** — set ``SMTP_HOST`` plus ``SMTP_PORT`` and, when required,
  ``SMTP_USER`` / ``SMTP_PASSWORD``. ``SMTP_SECURITY`` accepts ``starttls``
  (default), ``ssl`` (implicit TLS, port 465) or ``none``.

If neither transport is configured the module raises :class:`MailError` with
HTTP 503 instead of pretending the message was sent. ``ONTI_MAIL_ECHO=1``
prints the code to the application log and returns it in the API response; it
exists for local testing only and must never be enabled on a deployment.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict

import httpx

from common.logger import AppLogger
from modules import config as cfg

_LOG = AppLogger("mailer")

_MAIL_CFG: Dict[str, object] = cfg.CONFIG.get("mail", {}) or {}
_DEFAULT_FROM = str(_MAIL_CFG.get("from_email", "Onti <onboarding@resend.dev>"))
_RESEND_BASE = str(_MAIL_CFG.get("resend_api_base", "https://api.resend.com"))
_TIMEOUT = float(_MAIL_CFG.get("timeout_s", 20))

SUBJECTS = {
    "signup": "Your Onti sign-up verification code",
    "signin": "Your Onti sign-in verification code",
    "bind": "Confirm your e-mail for Onti",
    "change": "Confirm your Onti password change",
}


class MailError(Exception):
    """Raised when a verification e-mail cannot be delivered.

    Attributes:
        message (str): Human-readable, safe to show to the caller.
        status_code (int): HTTP status the API should return.

    Example:
        >>> MailError("E-mail delivery is not configured.", 503).status_code
        503
    """

    def __init__(self, message: str, status_code: int = 503):
        """Initialise the error.

        Args:
            message (str): Reason shown to the client.
            status_code (int): HTTP status code. Defaults to 503.

        Example:
            >>> MailError("nope").status_code
            503
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _env(name: str, default: str = "") -> str:
    """Read an environment variable and trim surrounding whitespace."""
    return os.environ.get(name, default).strip()


def echo_enabled() -> bool:
    """Whether the code should be echoed instead of delivered.

    Returns:
        bool: ``True`` when ``ONTI_MAIL_ECHO`` is ``1``/``true``/``yes``.

    Example:
        >>> isinstance(echo_enabled(), bool)
        True
    """
    return _env("ONTI_MAIL_ECHO").lower() in ("1", "true", "yes", "on")


def provider() -> str:
    """Return the configured transport.

    Returns:
        str: ``"resend"``, ``"smtp"``, ``"echo"`` or ``""`` when unavailable.

    Example:
        >>> provider() in ("", "resend", "smtp", "echo")
        True
    """
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("SMTP_HOST"):
        return "smtp"
    if echo_enabled():
        return "echo"
    return ""


def configured() -> bool:
    """Whether verification codes can actually reach a mailbox.

    Returns:
        bool: ``True`` when Resend or SMTP is configured. Echo mode is a
            local debugging aid and deliberately does *not* count.

    Example:
        >>> isinstance(configured(), bool)
        True
    """
    return bool(_env("RESEND_API_KEY") or _env("SMTP_HOST"))


def from_address() -> str:
    """Return the sender used in the ``From`` header.

    Returns:
        str: ``ONTI_MAIL_FROM`` when set, otherwise the configured default.

    Example:
        >>> "@" in from_address()
        True
    """
    return _env("ONTI_MAIL_FROM") or _DEFAULT_FROM


def _body(code: str, minutes: int) -> str:
    """Render the plain-text body shared by both transports."""
    return (
        f"Your Onti verification code is {code}.\n\n"
        f"It expires in {minutes} minutes and can be used once. "
        "If you did not request it, ignore this message — "
        "nobody can sign in without the code.\n"
    )


def _html(code: str, minutes: int) -> str:
    """Render the HTML body shared by both transports."""
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;'
        'margin:0 auto;padding:24px;color:#1b2434">'
        '<div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;'
        'color:#7a8496">Onti verification</div>'
        '<p style="font-size:15px;line-height:1.6">Use this code to continue:</p>'
        f'<div style="font-size:34px;font-weight:700;letter-spacing:10px;'
        f'padding:14px 0;color:#0E2A47">{code}</div>'
        f'<p style="font-size:13px;color:#5c6779;line-height:1.6">The code expires '
        f"in {minutes} minutes and works once. If you did not request it, you can "
        "safely ignore this e-mail.</p>"
        "</div>"
    )


def _send_resend(to: str, subject: str, text: str, html: str) -> None:
    """Deliver one message through the Resend HTTPS API."""
    try:
        resp = httpx.post(
            f"{_RESEND_BASE}/emails",
            headers={
                "authorization": f"Bearer {_env('RESEND_API_KEY')}",
                "content-type": "application/json",
            },
            json={
                "from": from_address(),
                "to": [to],
                "subject": subject,
                "text": text,
                "html": html,
            },
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - network failures are user-facing
        _LOG.warn(f"resend request failed: {exc}")
        raise MailError(
            "The verification e-mail could not be sent. Please retry in a moment.",
            502,
        ) from exc
    if resp.status_code >= 300:
        _LOG.warn(f"resend rejected: {resp.status_code} {resp.text[:200]}")
        raise MailError(
            "The verification e-mail was rejected by the mail provider. "
            "Check RESEND_API_KEY and the verified sender domain.",
            502,
        )


def _send_smtp(to: str, subject: str, text: str, html: str) -> None:
    """Deliver one message over SMTP."""
    msg = EmailMessage()
    msg["From"] = from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host = _env("SMTP_HOST")
    try:
        port = int(_env("SMTP_PORT", "587") or 587)
    except ValueError:
        port = 587
    security = (_env("SMTP_SECURITY", "starttls") or "starttls").lower()
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                host, port, timeout=_TIMEOUT, context=context
            )
        else:
            client = smtplib.SMTP(host, port, timeout=_TIMEOUT)
        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if user:
                client.login(user, password)
            client.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - surface a clean 502 to the caller
        _LOG.warn(f"smtp delivery failed: {exc}")
        raise MailError(
            "The verification e-mail could not be sent. Check the SMTP settings.",
            502,
        ) from exc


def send_code(email: str, code: str, purpose: str, minutes: int) -> str:
    """Deliver a verification code and report which transport was used.

    Args:
        email (str): Normalised recipient address.
        code (str): Six-digit verification code.
        purpose (str): ``signup``, ``signin``, ``bind`` or ``change``.
        minutes (int): Validity window mentioned in the message.

    Returns:
        str: ``"resend"``, ``"smtp"`` or ``"echo"``.

    Raises:
        MailError: With status 503 when no transport is configured, or 502
            when the provider rejects the message.

    Example:
        >>> send_code("a@example.com", "123456", "signin", 10)
        'resend'
    """
    subject = SUBJECTS.get(purpose, SUBJECTS["signin"])
    text = _body(code, minutes)
    html = _html(code, minutes)
    kind = provider()
    if kind == "resend":
        _send_resend(email, subject, text, html)
        return "resend"
    if kind == "smtp":
        _send_smtp(email, subject, text, html)
        return "smtp"
    if kind == "echo":
        _LOG.warn(f"[ONTI_MAIL_ECHO] {purpose} code for {email}: {code}")
        return "echo"
    raise MailError(
        "E-mail delivery is not configured on this deployment. "
        "Set RESEND_API_KEY (or SMTP_HOST) to enable verification codes.",
        503,
    )
