"""HTTP routes for the auth area."""

from __future__ import annotations

import re

from typing import Any
from typing import Dict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from modules import auth
from modules import config as cfg
from modules.db import get_conn
from modules.db import get_setting

router = APIRouter()


class LoginIn(BaseModel):
    """Registration / sign-in payload.

    Attributes:
        username (str): 3-64 characters, letters/digits/``_``/``.``/``-``.
        password (str): 8-256 characters; stored hashed.
        display_name (str): Optional human-readable name (defaults to empty).
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = ""

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        """Validate and normalise the username.

        Args:
            v (str): Raw username from the request body.

        Returns:
            str: The trimmed username.

        Raises:
            ValueError: When the username is not 3-64 allowed characters.

        Example:
            >>> LoginIn._check_username("  alice  ")
            'alice'
        """
        stripped = v.strip()
        if not re.match(r"^[A-Za-z0-9_.\-]{3,64}$", stripped):
            raise ValueError(
                "Username must be 3-64 characters: letters, digits, '_', '.', '-'."
            )
        return stripped

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        """Validate password length.

        Args:
            v (str): Raw password from the request body.

        Returns:
            str: The unchanged password when valid.

        Raises:
            ValueError: When the password is shorter than 8 or longer than 256
                characters.
        """
        if len(v) < 8 or len(v) > 256:
            raise ValueError("Password must be 8-256 characters.")
        return v


@router.get("/api/auth/status")
async def auth_status():
    """Report first-run state and whether self-registration is open.

    Returns:
        dict: ``{"first_run": bool, "allow_registration": bool}``.
    """
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        return {
            "first_run": count == 0,
            "allow_registration": get_setting(conn, "allow_registration", "1") == "1",
        }


@router.post("/api/auth/register")
async def register(data: LoginIn):
    """Create a new account and sign it in immediately.

    Args:
        data (LoginIn): Username, password and optional display name.

    Returns:
        JSONResponse: Body ``{"user": {...}}`` plus the session cookie.
    """
    with get_conn() as conn:
        user = auth.register_account(
            conn, data.username.strip(), data.password, data.display_name.strip(), ""
        )
        result = auth.authenticate(conn, data.username.strip(), data.password)
        resp = JSONResponse({"user": user})
        resp.set_cookie(
            auth.COOKIE_NAME,
            result["token"],
            httponly=True,
            samesite="lax",
            max_age=cfg.SESSION_DAYS * 86400,
        )
        return resp


@router.post("/api/auth/login")
async def login(request: Request, data: LoginIn):
    """Authenticate a user and set the session cookie.

    Args:
        request (Request): Incoming request (client IP and user agent are
            recorded for the audit trail).
        data (LoginIn): Credentials supplied by the client.

    Returns:
        JSONResponse: Body ``{"user": {...}}`` plus the session cookie.
    """
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    with get_conn() as conn:
        result = auth.authenticate(
            conn, data.username.strip(), data.password, ip=ip, user_agent=ua
        )
    resp = JSONResponse({"user": result["user"]})
    resp.set_cookie(
        auth.COOKIE_NAME,
        result["token"],
        httponly=True,
        samesite="lax",
        max_age=cfg.SESSION_DAYS * 86400,
    )
    return resp


@router.post("/api/auth/logout")
async def logout(request: Request):
    """Invalidate the caller's session and clear the cookie.

    Args:
        request (Request): Incoming request carrying the session cookie.

    Returns:
        JSONResponse: Body ``{"ok": true}`` with the cookie deleted.
    """
    token = request.cookies.get(auth.COOKIE_NAME, "")
    with get_conn() as conn:
        auth.logout(conn, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@router.get("/api/auth/me")
async def me(user: Dict[str, Any] = Depends(auth.current_user)):
    """Return the signed-in user's safe profile fields.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"user": {"id", "username", "display_name", "role", "enabled",
        "created_at"}}`` (keys missing from the record are omitted).
    """
    return {
        "user": {
            k: user[k]
            for k in ("id", "username", "display_name", "role", "enabled", "created_at")
            if k in user
        }
    }
