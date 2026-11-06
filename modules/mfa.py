"""Time-based one-time password (Authenticator app) second factor.

Wraps :mod:`pyotp` for the crypto and :mod:`qrcode` for the QR image used
by the provisioning URI. Both dependencies live in ``requirements.txt`` and
the module never fails at import time — on a host without them every call
returns ``None`` or raises :class:`MfaUnavailable`.

The schema lives in :mod:`modules.db` (``users.mfa_secret`` /
``users.mfa_enabled``) and is populated through the bind / verify / unbind
endpoints in :mod:`routes.auth`.
"""

from __future__ import annotations

from typing import Optional

try:
    import pyotp  # type: ignore
except Exception:  # pragma: no cover - graceful import fallback
    pyotp = None

try:
    import qrcode  # type: ignore
except Exception:  # pragma: no cover - graceful import fallback
    qrcode = None

from common.logger import AppLogger
from modules import config as cfg

_LOG = AppLogger("mfa")
_ISSUER = str(cfg.CONFIG.get("system_name", "Onti"))


class MfaError(Exception):
    """Problem creating, validating or disarming an MFA binding.

    Attributes:
        message (str): Safe to surface to the caller.
        status_code (int): HTTP status the API should return.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MfaUnavailable(MfaError):
    """The pyotp / qrcode runtime dependencies are missing."""

    def __init__(self) -> None:
        super().__init__(
            "TOTP support is not installed on this host. Contact an administrator.",
            501,
        )


def available() -> bool:
    """Whether TOTP verification can run on this host.

    Returns:
        bool: ``True`` when :mod:`pyotp` imported ok (which is everything
        the sign-in TOTP step needs). QR-code rendering for the bind flow
        is gated separately — see :func:`qr_available`.
    """
    return pyotp is not None


def qr_available() -> bool:
    """Whether we can render the provisioning QR image.

    Returns:
        bool: ``True`` when both :mod:`pyotp` and :mod:`qrcode` are
        present. Missing qrcode is tolerated on hosts where MFA sign-in
        is still useful (operators can type the secret manually instead
        of scanning the QR code).
    """
    return available() and qrcode is not None


def generate_secret() -> str:
    """Return a fresh base32 TOTP key.

    Returns:
        str: 32-character secret consumed by :class:`pyotp.TOTP`.

    Raises:
        MfaUnavailable: When dependencies are missing.
    """
    if not available():
        raise MfaUnavailable
    secret = pyotp.random_base32()
    assert isinstance(secret, str)
    return secret


def provisioning_uri(secret: str, username: str, label: str = "") -> str:
    """Build the ``otpauth://`` URI encoded in the QR code.

    Args:
        secret: Base32 TOTP secret.
        username: Stable account identifier shown inside the QR label.
        label: Optional alternate label; falls back to ``username``.

    Returns:
        str: ``otpauth://totp/Onti:username?secret=…&issuer=Onti``.
    """
    if not available():
        raise MfaUnavailable
    totp = pyotp.TOTP(secret)
    issuer = _ISSUER
    label_text = label or username
    uri = totp.provisioning_uri(name=label_text, issuer_name=issuer)
    assert isinstance(uri, str)
    return uri


def generate_qr_svg(uri: str, box_size: int = 8, border: int = 2) -> str:
    """Render a QR code for ``uri`` as a standalone SVG document.

    Args:
        uri: ``otpauth://`` URI produced by :func:`provisioning_uri`.
        box_size: Pixel width per module.
        border: Quiet-zone width measured in modules.

    Returns:
        str: Complete ``<svg>...</svg>`` string ready to drop into HTML.
    """
    if not available():
        raise MfaUnavailable
    import qrcode.image.svg as _qr_svg  # local import — registered lazily

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    image = qr.make_image(image_factory=_qr_svg.SvgImage)
    svg = image.to_string()
    if isinstance(svg, bytes):
        svg = svg.decode("utf-8", errors="replace")
    return str(svg)


def verify_code(secret: str, code: str, *, window: int = 1) -> bool:
    """Return whether ``code`` matches the current TOTP window.

    Args:
        secret: Base32 secret stored on the user row.
        code: 6-digit string supplied by the Authenticator app.
        window: How many steps of drift to tolerate on either side (1 = 30s
            leeway both ways).

    Returns:
        bool: ``True`` when the code is valid right now.
    """
    if not available() or not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(code, valid_window=window))


def current_code(secret: str) -> Optional[str]:
    """Return the code the app would accept right now — for testing only.

    Args:
        secret: Base32 secret.

    Returns:
        str: 6-digit TOTP, or ``None`` when dependencies are missing.
    """
    if not available() or not secret:
        return None
    totp = pyotp.TOTP(secret)
    return str(totp.now())
