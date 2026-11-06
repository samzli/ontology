"""E-mail verification challenges used by sign-up and two-factor sign-in.

A challenge is a short-lived record keyed by an opaque id:

* the six-digit code is never stored — only ``SHA-256(challenge_id:code)``;
* the record is written to SQLite *and* mirrored through
  :mod:`modules.snapshot`, so the instance that verifies the code does not
  have to be the instance that sent it (Vercel spins up several);
* a resend cooldown and an attempt counter make brute force impractical.

Verification is single-use: a successful check marks the row consumed and the
row is deleted from the durable store.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from common.logger import AppLogger
from modules import config as cfg
from modules import mailer, snapshot
from modules.db import now

_LOG = AppLogger("otp")

_MAIL = cfg.CONFIG.get("mail", {}) or {}
CODE_TTL_MINUTES = int(_MAIL.get("code_ttl_minutes", 10))
RESEND_COOLDOWN_S = int(_MAIL.get("resend_cooldown_s", 60))
MAX_ATTEMPTS = int(_MAIL.get("max_attempts", 5))

PURPOSES = ("signup", "signin", "bind", "change")


class OtpError(Exception):
    """Raised when a challenge cannot be issued or verified.

    Attributes:
        message (str): Human-readable reason.
        status_code (int): HTTP status to return.
        extra (Dict[str, Any]): Additional JSON fields (e.g. ``retry_after``).

    Example:
        >>> OtpError("Code expired.", 400).status_code
        400
    """

    def __init__(self, message: str, status_code: int = 400, **extra: Any) -> None:
        """Initialise the error.

        Args:
            message (str): Reason shown to the caller.
            status_code (int): HTTP status code. Defaults to 400.
            **extra (Any): Extra JSON fields merged into the response.

        Example:
            >>> OtpError("Wait.", 429, retry_after=30).extra["retry_after"]
            30
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra


def _code_hash(challenge_id: str, code: str) -> str:
    """Hash a code together with its challenge id.

    Binding the two means a hash captured from one challenge cannot be replayed
    against another even when both used the same six digits.
    """
    return hashlib.sha256(f"{challenge_id}:{code}".encode("utf-8")).hexdigest()


def _new_code() -> str:
    """Return a uniformly random six-digit code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _parse(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning ``None`` when malformed."""
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def mask_email(email: str) -> str:
    """Mask an address for display: ``alice@example.com`` → ``a***e@example.com``.

    Args:
        email (str): Normalised address.

    Returns:
        str: Masked address safe to show on the verification screen.

    Example:
        >>> mask_email("alice@example.com")
        'a***e@example.com'
    """
    name, _, domain = (email or "").partition("@")
    if not name:
        return "***"
    if len(name) <= 2:
        masked = name[0] + "***"
    else:
        masked = name[0] + "***" + name[-1]
    return f"{masked}@{domain}" if domain else masked


def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a database row into the dict shape used by the store."""
    return {
        "challenge_id": row["challenge_id"],
        "purpose": row["purpose"],
        "email": row["email"],
        "username": row["username"],
        "code_hash": row["code_hash"],
        "payload": row["payload"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "attempts": int(row["attempts"]),
        "consumed_at": row["consumed_at"],
        "request_ip": row["request_ip"],
    }


def _insert_local(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    """Insert or replace a challenge in the local database."""
    conn.execute(
        "INSERT OR REPLACE INTO email_otp(challenge_id, purpose, email, username, "
        "code_hash, payload, created_at, expires_at, attempts, consumed_at, "
        "request_ip) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            record["challenge_id"],
            record["purpose"],
            record["email"],
            record.get("username", ""),
            record["code_hash"],
            record.get("payload", "{}"),
            record["created_at"],
            record["expires_at"],
            int(record.get("attempts", 0)),
            record.get("consumed_at"),
            record.get("request_ip", ""),
        ),
    )


def _sync_remote(record: Dict[str, Any]) -> None:
    """Best-effort mirror to the durable store.

    A missing store is fine on a persistent host. On an ephemeral host without
    a store the challenge still works while the user stays on this instance,
    which is why the mirror failure is logged rather than raised.
    """
    if not snapshot.otp_store():
        return
    if not snapshot.save_otp(record["challenge_id"], record):
        _LOG.warn("verification challenge could not be mirrored to the store")


def _load(conn: sqlite3.Connection, challenge_id: str) -> Optional[Dict[str, Any]]:
    """Load a challenge locally, falling back to the durable store."""
    row = conn.execute(
        "SELECT * FROM email_otp WHERE challenge_id=?", (challenge_id,)
    ).fetchone()
    if row is not None:
        return _row_to_record(row)
    record = snapshot.load_otp(challenge_id)
    if record and record.get("challenge_id") == challenge_id:
        _insert_local(conn, record)
        return record
    return None


def _cooldown_left(conn: sqlite3.Connection, email: str, purpose: str) -> int:
    """Seconds the caller must wait before another code for this address."""
    row = conn.execute(
        "SELECT created_at FROM email_otp WHERE email=? AND purpose=? "
        "ORDER BY created_at DESC LIMIT 1",
        (email, purpose),
    ).fetchone()
    if row is None:
        return 0
    created = _parse(row["created_at"])
    if created is None:
        return 0
    elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    return max(0, int(RESEND_COOLDOWN_S - elapsed))


def issue(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    email: str,
    username: str = "",
    payload: Optional[Dict[str, Any]] = None,
    ip: str = "",
) -> Dict[str, Any]:
    """Create a challenge and deliver its code.

    Args:
        conn (sqlite3.Connection): Open database connection.
        purpose (str): One of :data:`PURPOSES`.
        email (str): Normalised recipient address.
        username (str): Account the challenge belongs to (may be empty).
        payload (Optional[Dict[str, Any]]): Extra state carried to the verify
            step (e.g. the pending password hash for a sign-up).
        ip (str): Client IP, stored for diagnostics.

    Returns:
        Dict[str, Any]: ``challenge_id``, ``email_masked``, ``expires_in``,
        ``provider`` and, in echo mode only, ``debug_code``.

    Raises:
        OtpError: 429 during the resend cooldown, 503 when mail is not
            configured or the challenge cannot be stored, 502 when the
            provider rejects the message.
    """
    if purpose not in PURPOSES:
        raise OtpError("Unknown verification purpose.", 422)
    wait = _cooldown_left(conn, email, purpose)
    if wait > 0:
        raise OtpError(
            f"Please wait {wait}s before requesting another code.",
            429,
            retry_after=wait,
        )

    challenge_id = secrets.token_urlsafe(24)
    code = _new_code()
    created = datetime.now(timezone.utc)
    record: Dict[str, Any] = {
        "challenge_id": challenge_id,
        "purpose": purpose,
        "email": email,
        "username": username,
        "code_hash": _code_hash(challenge_id, code),
        "payload": json.dumps(payload or {}, ensure_ascii=False),
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(
            timespec="seconds"
        ),
        "attempts": 0,
        "consumed_at": None,
        "request_ip": ip,
    }
    _insert_local(conn, record)
    _sync_remote(record)
    try:
        used = mailer.send_code(email, code, purpose, CODE_TTL_MINUTES)
    except mailer.MailError as exc:
        # Never leave a challenge behind whose code nobody received.
        conn.execute("DELETE FROM email_otp WHERE challenge_id=?", (challenge_id,))
        snapshot.delete_otp(challenge_id)
        raise OtpError(exc.message, exc.status_code) from exc

    result: Dict[str, Any] = {
        "challenge_id": challenge_id,
        "email_masked": mask_email(email),
        "expires_in": CODE_TTL_MINUTES * 60,
        "provider": used,
    }
    if used == "echo":
        result["debug_code"] = code
    return result


def resend(conn: sqlite3.Connection, challenge_id: str) -> Dict[str, Any]:
    """Issue a fresh code for an existing challenge.

    Args:
        conn (sqlite3.Connection): Open database connection.
        challenge_id (str): Challenge being extended.

    Returns:
        Dict[str, Any]: Same shape as :func:`issue`.

    Raises:
        OtpError: 404 when the challenge is unknown or already consumed, 429
            during the cooldown.
    """
    record = _load(conn, challenge_id)
    if record is None or record.get("consumed_at"):
        raise OtpError("This verification step has expired. Start again.", 400)
    # Retire the previous code first: a resend must not leave two valid codes
    # for the same step, otherwise an old e-mail stays usable.
    record["consumed_at"] = now()
    _insert_local(conn, record)
    snapshot.delete_otp(challenge_id)
    return issue(
        conn,
        purpose=str(record["purpose"]),
        email=str(record["email"]),
        username=str(record.get("username") or ""),
        payload=json.loads(record.get("payload") or "{}"),
        ip=str(record.get("request_ip") or ""),
    )


def verify(conn: sqlite3.Connection, challenge_id: str, code: str) -> Dict[str, Any]:
    """Check a submitted code and consume the challenge.

    Args:
        conn (sqlite3.Connection): Open database connection.
        challenge_id (str): Handle returned by :func:`issue`.
        code (str): Six-digit code from the e-mail.

    Returns:
        Dict[str, Any]: The consumed challenge record (``payload`` still
        JSON-encoded).

    Raises:
        OtpError: 400 for an unknown, consumed, expired or wrong code, and 429
            once the attempt budget is exhausted.
    """
    record = _load(conn, challenge_id)
    if record is None or record.get("consumed_at"):
        raise OtpError(
            "This verification step is no longer valid. Request a new code.", 400
        )
    expires = _parse(record.get("expires_at", ""))
    if expires is None or expires < datetime.now(timezone.utc):
        raise OtpError("The verification code has expired. Request a new one.", 400)
    attempts = int(record.get("attempts", 0))
    if attempts >= MAX_ATTEMPTS:
        raise OtpError(
            "Too many incorrect attempts. Request a new code.", 429, restart=True
        )

    if not secrets.compare_digest(
        _code_hash(challenge_id, (code or "").strip()), str(record["code_hash"])
    ):
        record["attempts"] = attempts + 1
        _insert_local(conn, record)
        # The caller wraps this in ``get_conn()``, which rolls back on the
        # exception below. Commit first so the attempt really counts — without
        # this the budget would reset on every request.
        conn.commit()
        _sync_remote(record)
        left = max(0, MAX_ATTEMPTS - record["attempts"])
        raise OtpError("Incorrect verification code.", 400, attempts_left=left)

    record["consumed_at"] = now()
    _insert_local(conn, record)
    snapshot.delete_otp(challenge_id)
    return record
