"""Account system: PBKDF2 password hashing, sessions, roles and FastAPI deps.

Passwords use PBKDF2-HMAC-SHA256 (stdlib hashlib). Sessions are opaque random
tokens stored server-side and delivered via an HttpOnly SameSite=Lax cookie.

Roles: ``admin`` and ``user``. The very first account created on an empty
system becomes the administrator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request

from modules import config, snapshot
from modules.db import (
    EPHEMERAL_STORAGE,
    audit,
    get_conn,
    get_setting,
    now,
    session_signing_key,
)

COOKIE_NAME = "onti_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PBKDF2_ITERATIONS = 240_000

_POLICY = config.CONFIG.get("password_policy", {}) or {}
PASSWORD_MIN_LENGTH = int(_POLICY.get("min_length", 10))
PASSWORD_MAX_LENGTH = int(_POLICY.get("max_length", 256))
PASSWORD_MIN_CATEGORIES = int(_POLICY.get("min_categories", 3))
PASSWORD_HISTORY_MONTHS = int(_POLICY.get("history_months", 24))
PASSWORD_MAX_AGE_DAYS = int(_POLICY.get("max_age_days", 90))
PASSWORD_WARN_BEFORE_DAYS = int(_POLICY.get("warn_before_days", 14))

PASSWORD_RULE_TEXT = (
    f"Password must be at least {PASSWORD_MIN_LENGTH} characters and contain "
    f"at least {PASSWORD_MIN_CATEGORIES} of: uppercase letters, lowercase "
    "letters, digits and special characters."
)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    """Encode bytes as unpadded url-safe base64.

    Args:
        raw (bytes): Bytes to encode.

    Returns:
        str: URL-safe base64 text without ``=`` padding.

    Example:
        >>> _b64(b"abc")
        'YWJj'
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    """Decode an unpadded url-safe base64 string back to bytes.

    Args:
        raw (str): URL-safe base64 text, with or without padding.

    Returns:
        bytes: Decoded bytes.

    Raises:
        binascii.Error: When the input is not valid base64.

    Example:
        >>> _unb64("YWJj")
        b'abc'
    """
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def issue_session_token(
    user_id: int,
    expires_at: str,
    *,
    username: str = "",
    display_name: str = "",
    role: str = "",
) -> str:
    """Build a signed ``payload.signature`` session token.

    The payload is a small JSON record holding the user id and expiry plus,
    when available, the identity claims (username, display name, role). The
    claims let a stateless serverless instance that never saw this login
    rebuild the session instead of bouncing the user back to sign-in (see
    :func:`user_by_token`). The token is also stored in the ``sessions``
    table so logout revokes it on the instance that issued it.

    Args:
        user_id (int): Id of the authenticated user.
        expires_at (str): ISO-8601 expiry timestamp.
        username (str): Login name, embedded as the ``un`` claim.
        display_name (str): Human label, embedded as the ``dn`` claim.
        role (str): ``admin`` or ``user``, embedded as the ``r`` claim.

    Returns:
        str: ``<urlsafe-base64 payload>.<hex HMAC-SHA256 signature>``. The
        payload carries a random nonce so two logins in the same second never
        collide on the ``sessions`` primary key.

    Example:
        >>> token = issue_session_token(1, "2026-11-06T00:00:00+00:00")
        >>> verify_session_token(token)[0]
        1
    """
    record: Dict[str, Any] = {
        "u": int(user_id),
        "e": expires_at,
        "n": secrets.token_hex(8),
    }
    if username:
        record["un"] = username
        record["dn"] = display_name or username
        record["r"] = role
    payload = _b64(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_claims(token: str) -> Optional[Dict[str, Any]]:
    """Verify the signature and return the full token record.

    Args:
        token (str): Token produced by :func:`issue_session_token`.

    Returns:
        Optional[Dict[str, Any]]: ``{"u": user_id, "e": expires_at, ...}``
            plus any identity claims, or ``None`` when the signature or the
            payload is invalid. Tokens issued before the JSON payload change
            (``user_id|expires_at|nonce``) are still accepted and yield a
            record without claims.

    Example:
        >>> verify_session_claims("garbage") is None
        True
    """
    payload, _, sig = (token or "").partition(".")
    if not payload or not sig:
        return None
    expected = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        raw = _unb64(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if isinstance(record, dict) and "u" in record and "e" in record:
        try:
            record["u"] = int(record["u"])
        except (TypeError, ValueError):
            return None
        return record
    parts = raw.split("|")
    try:
        return {"u": int(parts[0]), "e": parts[1]}
    except (IndexError, TypeError, ValueError):
        return None


def verify_session_token(token: str) -> Optional[Tuple[int, str]]:
    """Validate a signed token.

    Args:
        token (str): Token produced by :func:`issue_session_token`.

    Returns:
        Optional[Tuple[int, str]]: ``(user_id, expires_at)`` on success, or
            ``None`` when the signature or the payload is invalid.

    Example:
        >>> verify_session_token("garbage") is None
        True
    """
    claims = verify_session_claims(token)
    if claims is None:
        return None
    return claims["u"], claims["e"]


PENDING_TTL_MINUTES = 15


def issue_pending_token(
    payload: Dict[str, Any], minutes: int = PENDING_TTL_MINUTES
) -> str:
    """Sign a short-lived token for a step in progress.

    Used by the sign-in route to prove the password factor while the user
    fetches the e-mail code, and by legacy accounts while they bind an
    address. It is *not* a session: :func:`user_by_token` rejects it because
    it carries no ``u``/``e`` claims.

    Args:
        payload (Dict[str, Any]): Claims to carry, e.g. ``{"uid": 3,
            "purpose": "bind"}``.
        minutes (int): Validity window. Defaults to 15.

    Returns:
        str: ``<payload>.<signature>`` token.

    Example:
        >>> issue_pending_token({"uid": 1, "purpose": "bind"})[:1] != ""
        True
    """
    record = dict(payload)
    record["exp"] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )
    body = _b64(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = hmac.new(
        session_signing_key(), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


def verify_pending_token(token: str, purpose: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a token produced by :func:`issue_pending_token`.

    Args:
        token (str): Token from the browser.
        purpose (str): Expected ``purpose`` claim.

    Returns:
        Optional[Dict[str, Any]]: The claims, or ``None`` when the token is
        invalid, expired or meant for another step.

    Example:
        >>> verify_pending_token("garbage", "bind") is None
        True
    """
    body, _, sig = (token or "").partition(".")
    if not body or not sig:
        return None
    expected = hmac.new(
        session_signing_key(), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        record = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("purpose") != purpose:
        return None
    try:
        expires = datetime.fromisoformat(str(record.get("exp", "")))
    except ValueError:
        return None
    if expires < datetime.now(timezone.utc):
        return None
    return record


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a random salt.

    Args:
        password (str): Plaintext password.
        iterations (int): PBKDF2 iteration count. Defaults to
            ``PBKDF2_ITERATIONS`` (240000).

    Returns:
        str: ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` with base64 salt
            and digest.

    Example:
        >>> hash_password("correct horse").startswith("pbkdf2_sha256$")
        True
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a plaintext password against a stored hash.

    Args:
        password (str): Plaintext candidate.
        stored (str): Hash string produced by :func:`hash_password`.

    Returns:
        bool: ``True`` when the password matches, ``False`` otherwise,
            including malformed or unsupported hash strings.

    Example:
        >>> verify_password("correct horse", hash_password("correct horse"))
        True
    """
    try:
        scheme, iter_s, salt_s, hash_s = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_s),
            int(iter_s),
        )
        return hmac.compare_digest(base64.b64encode(dk).decode(), hash_s)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Validation / user CRUD
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Authentication or account-management failure raised by auth services.

    Attributes:
        message (str): Human-readable error description.
        status_code (int): HTTP status code to return.

    Example:
        >>> raise AuthError("Invalid username or password.", 401)
    """

    def __init__(self, message: str, status_code: int = 400):
        """Initialize the error.

        Args:
            message (str): Human-readable error description.
            status_code (int): HTTP status code to return. Defaults to 400.

        Example:
            >>> AuthError("Account disabled.", 403).status_code
            403
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_credentials(username: str, password: str) -> None:
    """Validate a username/password pair for registration or login.

    Args:
        username (str): Username candidate.
        password (str): Password candidate.

    Returns:
        None: Returns ``None`` when both values are acceptable.

    Raises:
        AuthError: With status 422 when the username does not match
            ``USERNAME_RE`` or the password length is outside 8-256.

    Example:
        >>> validate_credentials("alice", "long-enough-password")
    """
    if not USERNAME_RE.match(username or ""):
        raise AuthError(
            "Username must be 3-64 characters: letters, digits, '_', '.', '-'.", 422
        )
    if not password or len(password) < 8 or len(password) > 256:
        raise AuthError("Password must be 8-256 characters.", 422)


def normalize_email(value: str) -> str:
    """Validate and lower-case an e-mail address.

    Args:
        value (str): Address as typed by the user.

    Returns:
        str: Trimmed, lower-cased address.

    Raises:
        AuthError: With status 422 when the address is malformed or longer
            than 254 characters.

    Example:
        >>> normalize_email("  Alice@Example.COM ")
        'alice@example.com'
    """
    email = (value or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError("Enter a valid e-mail address.", 422)
    return email


def derive_username(email: str) -> str:
    """Build the display username from the part before ``@``.

    Characters outside the username alphabet become ``-``; a short or empty
    local part is padded so the result always satisfies :data:`USERNAME_RE`.

    Args:
        email (str): Normalised address.

    Returns:
        str: Candidate username (3-32 characters).

    Example:
        >>> derive_username("alice.w+news@example.com")
        'alice.w-news'
    """
    local = (email or "").split("@", 1)[0]
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "-", local)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    if len(cleaned) < 3:
        cleaned = (cleaned + "user")[:32]
    return cleaned[:32]


def _unique_username(conn: sqlite3.Connection, base: str) -> str:
    """Return a username that is not taken, appending ``-2``, ``-3``, ….

    Args:
        conn (sqlite3.Connection): Open database connection.
        base (str): Preferred username (already sanitised).

    Returns:
        str: The first free variant.

    Example:
        >>> _unique_username(conn, "alice")  # doctest: +SKIP
        'alice'
    """
    candidate = base
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
        suffix += 1
        tail = f"-{suffix}"
        candidate = (base[: 64 - len(tail)] + tail)[:64]
    return candidate


def password_categories(password: str) -> int:
    """Count how many of the four character classes a password uses.

    Args:
        password (str): Candidate password.

    Returns:
        int: 0-4, counting lowercase, uppercase, digits and other characters.

    Example:
        >>> password_categories("Abcdef1234!")
        4
    """
    checks = (str.islower, str.isupper, str.isdigit)
    used = 0
    for predicate in checks:
        if any(predicate(ch) for ch in password or ""):
            used += 1
    if any(not ch.isalnum() for ch in password or ""):
        used += 1
    return used


def password_reused(conn: sqlite3.Connection, user_id: int, password: str) -> bool:
    """Whether a password matches the current or any 24-month-old hash.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Account whose history is checked.
        password (str): Plaintext candidate.

    Returns:
        bool: ``True`` when the candidate was used within the retention
            window and must be rejected.

    Example:
        >>> password_reused(conn, 1, "another-password")
        False
    """
    current = conn.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()
    hashes = [current["password_hash"]] if current else []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=PASSWORD_HISTORY_MONTHS * 31)
    ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT password_hash FROM password_history WHERE user_id=? AND changed_at>=?",
        (user_id, cutoff),
    ).fetchall()
    hashes.extend(r["password_hash"] for r in rows)
    return any(verify_password(password, stored) for stored in hashes if stored)


def validate_password_policy(
    password: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
    user_id: Optional[int] = None,
) -> None:
    """Enforce length, character-class and reuse rules.

    Args:
        password (str): Candidate password.
        conn (Optional[sqlite3.Connection]): Connection used for the reuse
            check. When omitted the reuse rule is skipped.
        user_id (Optional[int]): Account whose history is checked.

    Returns:
        None: Returns when the password is acceptable.

    Raises:
        AuthError: With status 422 describing the first failed rule.

    Example:
        >>> validate_password_policy("Abcdef1234!")
    """
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", 422
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise AuthError(
            f"Password must be at most {PASSWORD_MAX_LENGTH} characters.", 422
        )
    if password_categories(password) < PASSWORD_MIN_CATEGORIES:
        raise AuthError(PASSWORD_RULE_TEXT, 422)
    if conn is not None and user_id is not None:
        if password_reused(conn, user_id, password):
            raise AuthError(
                "This password was used in the last "
                f"{PASSWORD_HISTORY_MONTHS} months. Choose a new one.",
                422,
            )


def record_password_history(
    conn: sqlite3.Connection, user_id: int, password_hash: str, changed_at: str
) -> None:
    """Append a hash to the account's password history.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Account the password belongs to.
        password_hash (str): Hash produced by :func:`hash_password`.
        changed_at (str): ISO-8601 change timestamp.

    Returns:
        None: The row is written by the caller's transaction.

    Example:
        >>> record_password_history(conn, 1, hash_password("Abcdef1234!"), now())
    """
    conn.execute(
        "INSERT INTO password_history(user_id, password_hash, changed_at) "
        "VALUES(?,?,?)",
        (user_id, password_hash, changed_at),
    )


def password_state(row: sqlite3.Row) -> Dict[str, Any]:
    """Describe how old the current password is and when it expires.

    Args:
        row (sqlite3.Row): User row.

    Returns:
        Dict[str, Any]: ``password_changed_at``, ``password_expires_at``,
        ``password_days_left``, ``password_expired`` and
        ``password_warning`` (``True`` inside the reminder window).

    Example:
        >>> password_state(row)["password_expired"]
        False
    """
    keys = row.keys()
    changed = row["password_changed_at"] if "password_changed_at" in keys else ""
    if not changed:
        changed = row["created_at"] if "created_at" in keys else ""
    base = _parse_dt(changed) if changed else datetime.now(timezone.utc)
    expires = base + timedelta(days=PASSWORD_MAX_AGE_DAYS)
    now_dt = datetime.now(timezone.utc)
    seconds_left = (expires - now_dt).total_seconds()
    # Whole calendar days so a freshly rotated password reports exactly 90.
    days_left = max(0, (expires.date() - now_dt.date()).days)
    return {
        "password_changed_at": changed,
        "password_expires_at": expires.isoformat(timespec="seconds"),
        "password_days_left": days_left,
        "password_expired": seconds_left <= 0,
        "password_warning": seconds_left <= PASSWORD_WARN_BEFORE_DAYS * 86400,
        "password_max_age_days": PASSWORD_MAX_AGE_DAYS,
    }


def public_user(row: sqlite3.Row) -> Dict[str, Any]:
    """Project a user row onto the fields safe to expose through the API.

    Args:
        row (sqlite3.Row): Row from the ``users`` table.

    Returns:
        Dict[str, Any]: Public fields, with ``enabled`` coerced to bool and
            ``last_login`` / ``login_count`` defaulted when the columns are
            absent.

    Example:
        >>> public_user(row)["username"]
        'alice'
    """
    keys = row.keys()
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"] if "last_login" in keys else None,
        "login_count": row["login_count"] if "login_count" in keys else 0,
        "email": row["email"] if "email" in keys else "",
        "email_verified": bool(row["email_verified"])
        if "email_verified" in keys
        else False,
        "mfa_enabled": bool(row["mfa_enabled"])
        if "mfa_enabled" in keys
        else False,
        "mfa_secret_present": bool(row["mfa_secret"])
        if "mfa_secret" in keys
        else False,
    }


def user_count(conn: sqlite3.Connection) -> int:
    """Count the registered accounts.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        int: Number of rows in the ``users`` table.

    Example:
        >>> user_count(conn)
        2
    """
    return conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


# Default administrator created on first run so the platform is usable
# immediately without a manual account-creation step.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Onti@2026"
DEFAULT_ADMIN_NAME = "Administrator"


def seed_default_admin(conn: sqlite3.Connection) -> bool:
    """Create the default administrator account if no users exist yet.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        bool: ``True`` if a new default admin was created, ``False``
            otherwise.

    Raises:
        AuthError: When the default credentials are rejected by
            :func:`create_user`, e.g. a concurrent insert took the username.

    Example:
        >>> seed_default_admin(conn)
        True
    """
    if user_count(conn) > 0:
        return False
    create_user(
        conn,
        username=DEFAULT_ADMIN_USERNAME,
        password=DEFAULT_ADMIN_PASSWORD,
        display_name=DEFAULT_ADMIN_NAME,
        role="admin",
    )
    audit(
        conn,
        "system",
        "user.default_admin_created",
        f"Default admin '{DEFAULT_ADMIN_USERNAME}' provisioned on first run",
    )
    return True


def _insert_user(
    conn: sqlite3.Connection,
    username: str,
    password_hash: str,
    display_name: str,
    role: str,
    email: str,
    email_verified: int,
) -> int:
    """Insert a validated account row and seed its password history.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Unique, already-sanitised username.
        password_hash (str): Hash produced by :func:`hash_password`.
        display_name (str): Display name; falls back to the username.
        role (str): ``admin`` or ``user``.
        email (str): Normalised address, or ``""`` for a legacy account.
        email_verified (int): ``1`` when the address was proven by a code.

    Returns:
        int: Primary key of the new account.

    Raises:
        AuthError: 409 when the username or the address is already taken.
    """
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise AuthError(f"Username '{username}' already exists.", 409)
    if email and conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise AuthError("An account already uses this e-mail address.", 409)
    created = now()
    cur = conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, enabled, "
        "created_at, email, email_verified, password_changed_at) "
        "VALUES(?,?,?,?,1,?,?,?,?)",
        (
            username,
            display_name or username,
            password_hash,
            role,
            created,
            email,
            int(email_verified),
            created,
        ),
    )
    user_id = int(cur.lastrowid)
    record_password_history(conn, user_id, password_hash, created)
    return user_id


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    *,
    email: str = "",
    email_verified: int = 0,
) -> int:
    """Insert a new account after validating the credentials and the role.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Unique username.
        password (str): Plaintext password, hashed before storage.
        display_name (str): Display name; falls back to the username.
        role (str): ``"admin"`` or ``"user"``. Defaults to ``"user"``.
        email (str): Optional sign-in address, already normalised.
        email_verified (int): ``1`` when the address was proven by a code.

    Returns:
        int: Primary key of the new account.

    Raises:
        AuthError: With status 422 for invalid credentials or an unknown
            role, and 409 when the username already exists.

    Example:
        >>> create_user(conn, "alice", "long-enough-password")
        2
    """
    validate_credentials(username, password)
    if role not in ("admin", "user"):
        raise AuthError("Role must be 'admin' or 'user'.")
    return _insert_user(
        conn,
        username,
        hash_password(password),
        display_name,
        role,
        email,
        email_verified,
    )


def list_users(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """List all accounts in creation order.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        List[Dict[str, Any]]: Public projections (see :func:`public_user`).

    Example:
        >>> [u["username"] for u in list_users(conn)]
        ['admin', 'alice']
    """
    rows = conn.execute(
        "SELECT id, username, display_name, role, enabled, created_at "
        "FROM users ORDER BY id"
    ).fetchall()
    return [public_user(r) for r in rows]


def _persist_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Mirror one account row to the durable store, best effort.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Account whose row changed.

    Returns:
        None: Upload failures are logged by :mod:`modules.snapshot` and do
            not abort the administrative change.

    Example:
        >>> _persist_user(conn, 2)  # doctest: +SKIP
    """
    row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if row is not None:
        snapshot.persist_account(conn, row["username"])


def set_user_role(
    conn: sqlite3.Connection, actor: Dict[str, Any], user_id: int, role: str
) -> None:
    """Change another account's role.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (Dict[str, Any]): Acting user; must carry ``id`` and
            ``username``.
        user_id (int): Account to modify.
        role (str): ``"admin"`` or ``"user"``.

    Returns:
        None: The change is committed by the caller.

    Raises:
        AuthError: For an unknown role, when the actor targets their own
            account (400), or when the account does not exist (404).

    Example:
        >>> set_user_role(conn, {"id": 1, "username": "admin"}, 2, "admin")
    """
    if role not in ("admin", "user"):
        raise AuthError("Role must be 'admin' or 'user'.")
    if int(actor["id"]) == int(user_id):
        raise AuthError("You cannot change your own role.", 400)
    cur = conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    if cur.rowcount == 0:
        raise AuthError("User not found.", 404)
    audit(conn, actor["username"], "user.role_change", f"user_id={user_id} role={role}")
    _persist_user(conn, user_id)


def set_user_enabled(
    conn: sqlite3.Connection, actor: Dict[str, Any], user_id: int, enabled: bool
) -> None:
    """Enable or disable an account.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (Dict[str, Any]): Acting user; must carry ``id`` and
            ``username``.
        user_id (int): Account to modify.
        enabled (bool): Desired state.

    Returns:
        None: The change is committed by the caller.

    Raises:
        AuthError: When the actor disables their own account (400) or the
            account does not exist (404).

    Example:
        >>> set_user_enabled(conn, {"id": 1, "username": "admin"}, 2, False)
    """
    if int(actor["id"]) == int(user_id) and not enabled:
        raise AuthError("You cannot disable your own account.", 400)
    cur = conn.execute(
        "UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id)
    )
    if cur.rowcount == 0:
        raise AuthError("User not found.", 404)
    audit(
        conn,
        actor["username"],
        "user.enable" if enabled else "user.disable",
        f"user_id={user_id}",
    )
    _persist_user(conn, user_id)


def reset_password(
    conn: sqlite3.Connection, actor: Dict[str, Any], user_id: int, new_password: str
) -> None:
    """Replace an account's password hash.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (Dict[str, Any]): Acting user; must carry ``username``.
        user_id (int): Account to modify.
        new_password (str): New plaintext password; must satisfy the policy
            (≥ 10 characters, 3 of 4 character classes, not reused within
            24 months).

    Returns:
        None: The change is committed by the caller.

    Raises:
        AuthError: With status 422 for a password that breaks the policy, and
            404 when the account does not exist.

    Example:
        >>> reset_password(conn, {"username": "admin"}, 2, "brand-new-password")
    """
    validate_password_policy(new_password)
    changed = now()
    cur = conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
        (hash_password(new_password), changed, user_id),
    )
    if cur.rowcount == 0:
        raise AuthError("User not found.", 404)
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    record_password_history(conn, user_id, str(row["password_hash"]), changed)
    audit(conn, actor["username"], "user.password_reset", f"user_id={user_id}")
    _persist_user(conn, user_id)


# ---------------------------------------------------------------------------
# Registration / login / sessions
# ---------------------------------------------------------------------------


def require_durable_accounts() -> None:
    """Refuse self-registration when the host cannot keep the account.

    A stateless host without a configured snapshot backend copies a fresh
    database on every cold start, so a new account would exist only on the
    single instance that handled the request: the next page load lands
    elsewhere and bounces the user straight back to sign-in. Rejecting the
    registration up front is far cheaper to explain than a phantom account.

    Returns:
        None: Returns when the host can keep accounts.

    Raises:
        AuthError: With status 503 on an ephemeral host that has no durable
            account store configured.

    Example:
        >>> require_durable_accounts()
    """
    if not EPHEMERAL_STORAGE:
        return
    if snapshot.account_store():
        return
    raise AuthError(
        "New accounts cannot be kept on this deployment: connect a durable "
        "store (Vercel Blob or ONTI_SNAPSHOT_URL) or ask an administrator for "
        "a pre-provisioned account.",
        503,
    )


def _register_login_row(
    conn: sqlite3.Connection, username: str
) -> Optional[sqlite3.Row]:
    """Return the local user row, adopting the account record when missing.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Username supplied at sign-in.

    Returns:
        Optional[sqlite3.Row]: The local row, or ``None`` when neither the
            database nor the durable account store knows the username.

    Example:
        >>> _register_login_row(conn, "alice") is None  # doctest: +SKIP
        False
    """
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row is not None:
        return row
    record = snapshot.load_account(username)
    if record is None:
        return None
    if not snapshot.adopt_account(conn, record):
        return None
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def _refresh_login_row(
    conn: sqlite3.Connection, row: sqlite3.Row, password: str
) -> Optional[sqlite3.Row]:
    """Adopt a newer password/role/enabled state from the durable store.

    An administrator may reset a password or disable an account on a
    different instance, so this instance's copy can be stale. When the local
    hash rejects the supplied password but the stored record accepts it, the
    record wins and the local row is updated.

    Args:
        conn (sqlite3.Connection): Open database connection.
        row (sqlite3.Row): Current local user row.
        password (str): Plaintext password supplied at sign-in.

    Returns:
        Optional[sqlite3.Row]: The refreshed row, or ``None`` when the stored
            record does not accept the password either.

    Example:
        >>> _refresh_login_row(conn, row, "wrong-password") is None  # doctest: +SKIP
        True
    """
    record = snapshot.load_account(row["username"])
    stored = str((record or {}).get("password_hash") or "")
    if not stored or not verify_password(password, stored):
        return None
    role = record.get("role")
    email = str(record.get("email") or "")
    changed_at = str(record.get("password_changed_at") or "")
    conn.execute(
        "UPDATE users SET password_hash=?, role=?, enabled=?, "
        "email=CASE WHEN ?<>'' THEN ? ELSE email END, password_changed_at=? "
        "WHERE id=?",
        (
            stored,
            role if role in ("admin", "user") else row["role"],
            int(record.get("enabled", 1)),
            email,
            email,
            changed_at,
            row["id"],
        ),
    )
    return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()


def register_account(
    conn: sqlite3.Connection,
    identifier: str,
    password: str,
    display_name: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Public self-registration.

    ``identifier`` is an e-mail address for the supported flow; a bare
    username is still accepted so provisioning scripts and legacy tests keep
    working. On an empty system the first account is forced to role=admin
    (first run); otherwise registration is allowed only when the
    ``allow_registration`` setting is on and new accounts get role=user.

    Args:
        conn (sqlite3.Connection): Open database connection.
        identifier (str): E-mail address (or, for compatibility, a username).
        password (str): Requested password.
        display_name (str): Optional display name.
        ip (str): Client IP recorded in the audit log.

    Returns:
        Dict[str, Any]: Public projection of the created account.

    Raises:
        AuthError: With 422 for invalid credentials or a password that breaks
            the policy, 403 when self-registration is disabled by an
            administrator, and 503 when the host cannot persist new accounts.

    Example:
        >>> register_account(conn, "alice@example.com", "LongEnough1!")["role"]
        'user'
    """
    email = ""
    if "@" in (identifier or ""):
        email = normalize_email(identifier)
        username = _unique_username(conn, derive_username(email))
    else:
        username = (identifier or "").strip()
    validate_credentials(username, password)
    validate_password_policy(password)
    require_durable_accounts()
    first = user_count(conn) == 0
    if not first and get_setting(conn, "allow_registration", "1") != "1":
        raise AuthError("Self-registration is disabled. Contact an administrator.", 403)
    role = "admin" if first else "user"
    uid = _insert_user(
        conn,
        username,
        hash_password(password),
        display_name,
        role,
        email,
        1 if email else 0,
    )
    audit(
        conn,
        username,
        "user.register" + (".first_admin" if first else ""),
        f"user_id={uid} role={role} email={email}",
        ip,
    )
    # Mirror the new row to the durable store before this request commits:
    # raising rolls the transaction back, so a failed upload never leaves a
    # phantom account that the next instance cannot resolve.
    if not snapshot.persist_account(conn, username):
        raise AuthError(
            "The account could not be saved to durable storage. Please retry.", 503
        )
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return public_user(row)


def register_hashed(
    conn: sqlite3.Connection,
    email: str,
    password_hash: str,
    display_name: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Create the account behind a verified sign-up challenge.

    The password was validated and hashed when the code was requested, so it
    never travels to the server twice.

    Args:
        conn (sqlite3.Connection): Open database connection.
        email (str): Already-normalised address proven by a code.
        password_hash (str): Hash captured at the start of the sign-up.
        display_name (str): Optional display name.
        ip (str): Client IP recorded in the audit log.

    Returns:
        Dict[str, Any]: Public projection of the created account.

    Raises:
        AuthError: 409 when the address was taken meanwhile, 403 when
            self-registration is closed, 503 when storage is not durable.
    """
    require_durable_accounts()
    first = user_count(conn) == 0
    if not first and get_setting(conn, "allow_registration", "1") != "1":
        raise AuthError("Self-registration is disabled. Contact an administrator.", 403)
    username = _unique_username(conn, derive_username(email))
    role = "admin" if first else "user"
    uid = _insert_user(conn, username, password_hash, display_name, role, email, 1)
    audit(
        conn,
        username,
        "user.register" + (".first_admin" if first else ""),
        f"user_id={uid} role={role} email={email}",
        ip,
    )
    if not snapshot.persist_account(conn, username):
        raise AuthError(
            "The account could not be saved to durable storage. Please retry.", 503
        )
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return public_user(row)


def find_account(conn: sqlite3.Connection, identifier: str) -> Optional[sqlite3.Row]:
    """Resolve an e-mail address or username to a local account row.

    Args:
        conn (sqlite3.Connection): Open database connection.
        identifier (str): Address or username supplied at sign-in.

    Returns:
        Optional[sqlite3.Row]: The row, or ``None`` when unknown.

    Example:
        >>> find_account(conn, "alice@example.com")["username"]  # doctest: +SKIP
        'alice'
    """
    value = (identifier or "").strip()
    if not value:
        return None
    if "@" in value:
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (value.lower(),)
        ).fetchone()
        if row is not None:
            return row
        return None
    return _register_login_row(conn, value)


def verify_signin(
    conn: sqlite3.Connection, identifier: str, password: str, *, ip: str = ""
) -> sqlite3.Row:
    """Check the password factor of a sign-in attempt.

    Args:
        conn (sqlite3.Connection): Open database connection.
        identifier (str): E-mail address or username.
        password (str): Plaintext password.
        ip (str): Client IP recorded in the audit log.

    Returns:
        sqlite3.Row: The authenticated account row.

    Raises:
        AuthError: 401 for an unknown account or a bad password, 403 when the
            account is disabled.

    Example:
        >>> verify_signin(conn, "alice@example.com", "LongEnough1!")
    """
    row = find_account(conn, identifier)
    if row is None:
        verify_password(password, "pbkdf2_sha256$240000$" + "x" * 24 + "$" + "y" * 44)
        audit(conn, identifier or "?", "auth.login_failed", "unknown account", ip)
        raise AuthError(_unknown_account_message(), 401)
    if not row["enabled"]:
        audit(conn, row["username"], "auth.login_failed", "account disabled", ip)
        raise AuthError("This account is disabled. Contact an administrator.", 403)
    if not verify_password(password, row["password_hash"]):
        refreshed = _refresh_login_row(conn, row, password)
        if refreshed is None:
            audit(conn, row["username"], "auth.login_failed", "bad password", ip)
            raise AuthError("Invalid username or password.", 401)
        row = refreshed
        if not row["enabled"]:
            audit(conn, row["username"], "auth.login_failed", "account disabled", ip)
            raise AuthError("This account is disabled. Contact an administrator.", 403)
    return row


def start_session(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    user_agent: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Create a session for an already-authenticated account.

    Args:
        conn (sqlite3.Connection): Open database connection.
        row (sqlite3.Row): Account that passed every factor.
        user_agent (str): Client user agent, truncated to 300 characters.
        ip (str): Client IP recorded in the audit log.

    Returns:
        Dict[str, Any]: ``token``, ``user`` and ``expires_at``.

    Example:
        >>> start_session(conn, row)["user"]["username"]
        'alice'
    """
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=config.SESSION_DAYS)
    token = issue_session_token(
        row["id"],
        expires.isoformat(timespec="seconds"),
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
    )
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, user_agent) "
        "VALUES(?,?,?,?,?)",
        (
            token,
            row["id"],
            created.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            (user_agent or "")[:300],
        ),
    )
    conn.execute(
        "UPDATE users SET last_login=?, login_count=COALESCE(login_count,0)+1 "
        "WHERE id=?",
        (created.isoformat(timespec="seconds"), row["id"]),
    )
    audit(conn, row["username"], "auth.login_success", f"user_id={row['id']}", ip)
    refreshed = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    return {
        "token": token,
        "user": public_user(refreshed),
        "expires_at": expires.isoformat(timespec="seconds"),
        "password": password_state(refreshed),
    }


def bind_email(
    conn: sqlite3.Connection, user_id: int, email: str, *, ip: str = ""
) -> sqlite3.Row:
    """Attach a proven address to an account created before e-mail sign-in.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Account being upgraded.
        email (str): Address already proven by a verification code.
        ip (str): Client IP recorded in the audit log.

    Returns:
        sqlite3.Row: The refreshed account row.

    Raises:
        AuthError: 409 when another account already uses the address.

    Example:
        >>> bind_email(conn, 1, "alice@example.com")["email"]
        'alice@example.com'
    """
    normalised = normalize_email(email)
    other = conn.execute(
        "SELECT id FROM users WHERE email=? AND id<>?", (normalised, user_id)
    ).fetchone()
    if other is not None:
        raise AuthError("An account already uses this e-mail address.", 409)
    conn.execute(
        "UPDATE users SET email=?, email_verified=1 WHERE id=?",
        (normalised, user_id),
    )
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    snapshot.persist_account(conn, row["username"])
    audit(conn, row["username"], "auth.email_bound", f"email={normalised}", ip)
    return row


def change_password(
    conn: sqlite3.Connection,
    user_id: int,
    current_password: str,
    new_password: str,
    *,
    ip: str = "",
) -> Dict[str, Any]:
    """Rotate an account password after checking policy and history.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Account being updated.
        current_password (str): Existing password, used as the second factor.
        new_password (str): Replacement password.
        ip (str): Client IP recorded in the audit log.

    Returns:
        Dict[str, Any]: ``password`` state for the refreshed account.

    Raises:
        AuthError: 401 when the current password is wrong, 422 when the new
            password breaks the length/class/reuse policy.

    Example:
        >>> change_password(conn, 1, "OldPassword1!", "NewPassword2!")
    """
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("Account not found.", 404)
    if not verify_password(current_password, row["password_hash"]):
        audit(conn, row["username"], "auth.password_change_failed", "bad current", ip)
        raise AuthError("Current password is incorrect.", 401)
    validate_password_policy(new_password, conn=conn, user_id=user_id)
    if verify_password(new_password, row["password_hash"]):
        raise AuthError(
            f"This password was used in the last {PASSWORD_HISTORY_MONTHS} months. "
            "Choose a new one.",
            422,
        )
    changed = now()
    new_hash = hash_password(new_password)
    conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
        (new_hash, changed, user_id),
    )
    record_password_history(conn, user_id, new_hash, changed)
    snapshot.persist_account(conn, row["username"])
    audit(conn, row["username"], "auth.password_changed", "self-service", ip)
    refreshed = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return {"password": password_state(refreshed), "user": public_user(refreshed)}


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-8601 timestamp into a datetime.

    Args:
        s (str): Timestamp text, e.g. ``"2026-11-06T00:00:00+00:00"``.

    Returns:
        datetime: Parsed datetime (timezone-aware when the offset is present).

    Raises:
        ValueError: When the string is not a valid ISO-8601 timestamp.

    Example:
        >>> _parse_dt("2026-11-06T00:00:00+00:00").year
        2026
    """
    return datetime.fromisoformat(s)


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> Dict[str, Any]:
    """Verify credentials and create a session.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Username supplied by the client.
        password (str): Password supplied by the client.
        ip (str): Client IP recorded in the audit log.
        user_agent (str): Client user agent, truncated to 300 characters.

    Returns:
        Dict[str, Any]: ``token``, ``user`` (public projection) and
            ``expires_at``.

    Raises:
        AuthError: With 422 for malformed credentials, 401 for an unknown
            username or a bad password, and 403 for a disabled account.

    Example:
        >>> session = authenticate(conn, "alice", "correct-horse")
        >>> session["user"]["username"]
        'alice'
    """
    # Defensive: internal callers (tests, provisioning scripts) may skip the
    # Pydantic models, so reject obviously malformed input here. This helper
    # checks the password factor only; the HTTP sign-in route additionally
    # requires a mailed one-time code before it calls it.
    if not username or not password:
        raise AuthError("Enter your e-mail and password.", 422)
    row = verify_signin(conn, username, password, ip=ip)
    return start_session(conn, row, user_agent=user_agent, ip=ip)


def user_by_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """Resolve the user behind a session token.

    Looks the token up in the ``sessions`` table, deletes it when expired, and
    falls back to verifying the stateless signed token when the login happened
    on another instance.

    Args:
        conn (sqlite3.Connection): Open database connection.
        token (str): Session token from the cookie.

    Returns:
        Optional[sqlite3.Row]: The matching, enabled user row, or ``None``
            when the token is empty, unknown, revoked, expired or invalid.

    Example:
        >>> user_by_token(conn, token)["username"]
        'alice'
    """
    if not token:
        return None
    if conn.execute(
        "SELECT 1 FROM revoked_sessions WHERE token=?", (token,)
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token=?",
        (token,),
    ).fetchone()
    if row is not None:
        sess = conn.execute(
            "SELECT expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
        try:
            if _parse_dt(sess["expires_at"]) < datetime.now(timezone.utc):
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                return None
        except (TypeError, ValueError):
            return None
        return row

    # Serverless fallback: the login happened on another instance, so its
    # ``sessions`` row is not visible here. The token itself is signed with
    # the shared machine key, so verify it and load the user directly.
    parsed = verify_session_claims(token)
    if parsed is None:
        return None
    claims = parsed
    user_id = claims["u"]
    expires_at = claims["e"]
    username = claims.get("un") or ""
    try:
        if _parse_dt(expires_at) < datetime.now(timezone.utc):
            return None
    except (TypeError, ValueError):
        return None
    # Ephemeral host: ids collide across instances, so the username claim is
    # the reliable identity. Prefer the local row for that name, adopting the
    # durable account record when this instance has never seen it.
    if EPHEMERAL_STORAGE and username:
        row = _register_login_row(conn, username)
        if row is None:
            return None
        return row if row["enabled"] else None
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user is not None and not user["enabled"]:
        return None
    return user


def logout(conn: sqlite3.Connection, token: str, ip: str = "") -> None:
    """Delete a session, remember it as revoked, and record the logout event.

    The token is written to ``revoked_sessions`` so that an instance which
    never saw the login (serverless host) also rejects it afterwards.

    Args:
        conn (sqlite3.Connection): Open database connection.
        token (str): Session token to revoke.
        ip (str): Client IP recorded in the audit log.

    Returns:
        None: The change is committed by the caller.

    Example:
        >>> logout(conn, token)
    """
    row = user_by_token(conn, token)
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.execute(
        "INSERT OR REPLACE INTO revoked_sessions(token, revoked_at) VALUES(?,?)",
        (token, now()),
    )
    if row:
        audit(conn, row["username"], "auth.logout", f"user_id={row['id']}", ip)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI dependency returning the current user, if any.

    Args:
        request (Request): Incoming request carrying the session cookie.

    Returns:
        Optional[Dict[str, Any]]: The user row as a dict when the session is
            valid, otherwise ``None``.

    Example:
        >>> @app.get("/me")
        ... def me(user=Depends(optional_user)):
        ...     return {"user": user}
    """
    token = request.cookies.get(COOKIE_NAME) or ""
    with get_conn() as conn:
        row = user_by_token(conn, token)
        return dict(row) if row else None


def current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency requiring an authenticated user.

    Args:
        request (Request): Incoming request carrying the session cookie.

    Returns:
        Dict[str, Any]: The authenticated user row.

    Raises:
        HTTPException: With status 401 when no valid session is present.

    Example:
        >>> @app.get("/me")
        ... def me(user=Depends(current_user)):
        ...     return user
    """
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """FastAPI dependency requiring administrator privileges.

    Args:
        user (Dict[str, Any]): Result of :func:`current_user`, injected by
            FastAPI.

    Returns:
        Dict[str, Any]: The same user, whose role is ``admin``.

    Raises:
        HTTPException: With status 403 when the user is not an administrator.

    Example:
        >>> @app.get("/admin")
        ... def admin_home(user=Depends(require_admin)):
        ...     return {"ok": True}
    """
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="Administrator privileges required."
        )
    return user


def _unknown_account_message() -> str:
    """Build the 401 message for an unknown username.

    On a stateless serverless host, an account registered on one instance is
    invisible to the others, so an otherwise-valid colleague is told exactly
    what happened and what to do instead of a bare "invalid password".

    Returns:
        str: Message that keeps the stable ``Invalid username or password.``
            prefix clients already match on, with an actionable hint appended
            only when the host does not persist state.

    Example:
        >>> _unknown_account_message().startswith("Invalid username")
        True
    """
    base = "Invalid username or password."
    if not EPHEMERAL_STORAGE:
        return base
    from modules import snapshot  # local import: keeps module load order free

    if snapshot.enabled():
        # Snapshots carry accounts across instances, so the per-instance
        # hint would be misleading.
        return base
    return (
        base
        + " This demo deployment does not share accounts between instances."
        + " If you registered recently, register again in this browser or ask"
        + " an administrator for a pre-provisioned account."
    )
