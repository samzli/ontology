"""Test helpers for HTTP-level authentication.

The production sign-in path is two-factor and therefore cannot be driven from
a plain ``POST /api/auth/login`` any more. Most suites only need an
authenticated client to exercise *other* features, so they mint a real session
through the auth service and drop the signed cookie into the test client. The
two-factor journey itself is covered end to end in ``verify_auth2fa.py``.
"""

from __future__ import annotations

from typing import Any

from modules import auth
from modules.db import get_conn


def session_token(username: str, password: str) -> str:
    """Return a freshly issued, database-backed session token.

    Args:
        username (str): Account to sign in.
        password (str): Its plaintext password.

    Returns:
        str: Signed session token.

    Example:
        >>> session_token("admin", "Onti@2026")[:1] != ""
        True
    """
    with get_conn() as conn:
        return auth.authenticate(conn, username, password)["token"]


def login(client: Any, username: str, password: str) -> Any:
    """Authenticate ``client`` as ``username`` and return it.

    Args:
        client (Any): ``httpx``/FastAPI test client.
        username (str): Account to sign in.
        password (str): Its plaintext password.

    Returns:
        Any: The same client, for chaining.

    Example:
        >>> login(client, "admin", "Onti@2026")
    """
    client.cookies.set(auth.COOKIE_NAME, session_token(username, password))
    return client


def admin_login(client: Any) -> Any:
    """Authenticate ``client`` as the seeded administrator.

    Args:
        client (Any): ``httpx``/FastAPI test client.

    Returns:
        Any: The same client.

    Example:
        >>> admin_login(client)
    """
    return login(client, "admin", "Onti@2026")
