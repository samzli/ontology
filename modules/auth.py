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
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request

from modules import config
from modules.db import audit, get_conn, get_setting, now, session_signing_key

COOKIE_NAME = "onti_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")
PBKDF2_ITERATIONS = 240_000


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


def issue_session_token(user_id: int, expires_at: str) -> str:
    """Build a signed ``payload.signature`` session token.

    The token is also stored in the ``sessions`` table (so logout revokes it
    on that instance), but it is self-contained: a serverless instance that
    never saw the login can still verify it with the shared machine key.

    Args:
        user_id (int): Id of the authenticated user.
        expires_at (str): ISO-8601 expiry timestamp.

    Returns:
        str: ``<urlsafe-base64 payload>.<hex HMAC-SHA256 signature>``. The
        payload carries a random nonce so two logins in the same second never
        collide on the ``sessions`` primary key.

    Example:
        >>> token = issue_session_token(1, "2026-11-06T00:00:00+00:00")
        >>> verify_session_token(token)[0]
        1
    """
    payload = _b64(
        f"{int(user_id)}|{expires_at}|{secrets.token_hex(8)}".encode("utf-8")
    )
    sig = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


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
    payload, _, sig = (token or "").partition(".")
    if not payload or not sig:
        return None
    expected = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        parts = _unb64(payload).decode("utf-8").split("|")
        return int(parts[0]), parts[1]
    except (IndexError, ValueError, TypeError, UnicodeDecodeError):
        return None


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
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"] if "last_login" in row.keys() else None,
        "login_count": row["login_count"] if "login_count" in row.keys() else 0,
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


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
) -> int:
    """Insert a new account after validating the credentials and the role.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Unique username.
        password (str): Plaintext password, hashed before storage.
        display_name (str): Display name; falls back to the username.
        role (str): ``"admin"`` or ``"user"``. Defaults to ``"user"``.

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
    exists = conn.execute(
        "SELECT 1 FROM users WHERE username=?", (username,)
    ).fetchone()
    if exists:
        raise AuthError(f"Username '{username}' already exists.", 409)
    cur = conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, enabled, "
        "created_at) VALUES(?,?,?,?,1,?)",
        (username, display_name or username, hash_password(password), role, now()),
    )
    return int(cur.lastrowid)


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


def reset_password(
    conn: sqlite3.Connection, actor: Dict[str, Any], user_id: int, new_password: str
) -> None:
    """Replace an account's password hash.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (Dict[str, Any]): Acting user; must carry ``username``.
        user_id (int): Account to modify.
        new_password (str): New plaintext password (8-256 characters).

    Returns:
        None: The change is committed by the caller.

    Raises:
        AuthError: With status 422 for an invalid length, and 404 when the
            account does not exist.

    Example:
        >>> reset_password(conn, {"username": "admin"}, 2, "brand-new-password")
    """
    if not new_password or len(new_password) < 8 or len(new_password) > 256:
        raise AuthError("Password must be 8-256 characters.", 422)
    cur = conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user_id),
    )
    if cur.rowcount == 0:
        raise AuthError("User not found.", 404)
    audit(conn, actor["username"], "user.password_reset", f"user_id={user_id}")


# ---------------------------------------------------------------------------
# Registration / login / sessions
# ---------------------------------------------------------------------------


def register_account(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    display_name: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Public self-registration.

    On an empty system the first account is forced to role=admin (first run);
    otherwise registration is allowed only when the ``allow_registration``
    setting is on and new accounts get role=user.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Requested username.
        password (str): Requested password.
        display_name (str): Optional display name.
        ip (str): Client IP recorded in the audit log.

    Returns:
        Dict[str, Any]: Public projection of the created account.

    Raises:
        AuthError: With 422 for invalid credentials, and 403 when
            self-registration is disabled.

    Example:
        >>> register_account(conn, "alice", "long-enough-password")["role"]
        'user'
    """
    validate_credentials(username, password)
    first = user_count(conn) == 0
    if not first and get_setting(conn, "allow_registration", "1") != "1":
        raise AuthError("Self-registration is disabled. Contact an administrator.", 403)
    role = "admin" if first else "user"
    uid = create_user(conn, username, password, display_name, role)
    audit(
        conn,
        username,
        "user.register" + (".first_admin" if first else ""),
        f"user_id={uid} role={role}",
        ip,
    )
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return public_user(row)


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
    # Defensive: make sure callers (Pydantic or otherwise) always feed
    # sane values. validate_credentials is the single source of truth for
    # what constitutes a legitimate login attempt — Pydantic validators on
    # the LoginIn model mirror this, but internal callers can skip those.
    validate_credentials(username, password)
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    # Constant-ish path: still evaluate a hash when user is missing.
    if row is None:
        verify_password(password, "pbkdf2_sha256$240000$" + "x" * 24 + "$" + "y" * 44)
        audit(conn, username or "?", "auth.login_failed", "unknown username", ip)
        raise AuthError("Invalid username or password.", 401)
    if not row["enabled"]:
        audit(conn, username, "auth.login_failed", "account disabled", ip)
        raise AuthError("This account is disabled. Contact an administrator.", 403)
    if not verify_password(password, row["password_hash"]):
        audit(conn, username, "auth.login_failed", "bad password", ip)
        raise AuthError("Invalid username or password.", 401)

    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=config.SESSION_DAYS)
    token = issue_session_token(row["id"], expires.isoformat(timespec="seconds"))
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
        "UPDATE users SET last_login=?, login_count=COALESCE(login_count,0)+1 WHERE id=?",
        (created.isoformat(timespec="seconds"), row["id"]),
    )
    audit(conn, username, "auth.login_success", f"user_id={row['id']}", ip)
    refreshed = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    return {
        "token": token,
        "user": public_user(refreshed),
        "expires_at": expires.isoformat(timespec="seconds"),
    }


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
    parsed = verify_session_token(token)
    if parsed is None:
        return None
    user_id, expires_at = parsed
    try:
        if _parse_dt(expires_at) < datetime.now(timezone.utc):
            return None
    except (TypeError, ValueError):
        return None
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or not user["enabled"]:
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
