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
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def issue_session_token(user_id: int, expires_at: str) -> str:
    """Return a signed token ``payload.signature``.

    The token is also stored in the ``sessions`` table (so logout revokes it
    on that instance), but it is self-contained: a serverless instance that
    never saw the login can still verify it with the shared machine key.
    """
    payload = _b64(f"{int(user_id)}|{expires_at}".encode("utf-8"))
    sig = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str) -> Optional[Tuple[int, str]]:
    """Validate a signed token; returns ``(user_id, expires_at)`` or None."""
    payload, _, sig = (token or "").partition(".")
    if not payload or not sig:
        return None
    expected = hmac.new(
        session_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user_id, expires = _unb64(payload).decode("utf-8").split("|", 1)
        return int(user_id), expires
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
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
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_credentials(username: str, password: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise AuthError(
            "Username must be 3-64 characters: letters, digits, '_', '.', '-'.", 422
        )
    if not password or len(password) < 8 or len(password) > 256:
        raise AuthError("Password must be 8-256 characters.", 422)


def public_user(row: sqlite3.Row) -> Dict[str, Any]:
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
    return conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


# Default administrator created on first run so the platform is usable
# immediately without a manual account-creation step.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Onti@2026"
DEFAULT_ADMIN_NAME = "Administrator"


def seed_default_admin(conn: sqlite3.Connection) -> bool:
    """Create the default administrator account if no users exist yet.

    Returns True if a new default admin was created, False otherwise.
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
    rows = conn.execute(
        "SELECT id, username, display_name, role, enabled, created_at "
        "FROM users ORDER BY id"
    ).fetchall()
    return [public_user(r) for r in rows]


def set_user_role(
    conn: sqlite3.Connection, actor: Dict[str, Any], user_id: int, role: str
) -> None:
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

    * On an empty system the first account is forced to role=admin (first-run).
    * Otherwise allowed only when the ``allow_registration`` setting is on;
      new accounts get role=user.
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
    return datetime.fromisoformat(s)


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> Dict[str, Any]:
    """Verify credentials and create a session. Raises AuthError on failure."""
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
    if not token:
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
    row = user_by_token(conn, token)
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    if row:
        audit(conn, row["username"], "auth.logout", f"user_id={row['id']}", ip)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(COOKIE_NAME) or ""
    with get_conn() as conn:
        row = user_by_token(conn, token)
        return dict(row) if row else None


def current_user(request: Request) -> Dict[str, Any]:
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="Administrator privileges required."
        )
    return user
