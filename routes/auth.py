"""HTTP routes for the auth area.

Two-factor by design: a password alone never yields a session. Sign-up and
sign-in are separate flows, and both end with a six-digit code mailed to the
address on file.

* ``POST /api/auth/signup/start``  → e-mail + password, sends a code
* ``POST /api/auth/signup/verify`` → code, creates the account and signs in
* ``POST /api/auth/signin/start``  → e-mail + password, sends a code
* ``POST /api/auth/signin/verify`` → code, issues the session
* ``POST /api/auth/bind/start`` / ``bind/verify`` → one-time upgrade path for
  accounts created before e-mail sign-in
* ``POST /api/auth/password/change`` → self-service rotation every 90 days
"""

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from modules import auth, mailer, otp, snapshot
from modules import config as cfg
from modules.db import EPHEMERAL_STORAGE, get_conn, get_setting

router = APIRouter()


class SignupIn(BaseModel):
    """Sign-up payload.

    Attributes:
        email (str): Address that will receive the verification code and
            become the sign-in identity.
        password (str): Plaintext password; validated against the policy.
        display_name (str): Optional human-readable name.
    """

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default="", max_length=120)


class VerifyIn(BaseModel):
    """Payload for completing a code challenge.

    Attributes:
        challenge_id (str): Opaque handle returned by the ``start`` call.
        code (str): Six-digit code from the e-mail.
    """

    challenge_id: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=4, max_length=12)


class SigninIn(BaseModel):
    """Sign-in payload (first factor).

    Attributes:
        identifier (str): E-mail address, or the username of a legacy account.
        password (str): Plaintext password.
    """

    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ResendIn(BaseModel):
    """Payload for re-sending a code.

    Attributes:
        challenge_id (str): Challenge being extended.
    """

    challenge_id: str = Field(min_length=8, max_length=128)


class BindStartIn(BaseModel):
    """Payload for binding an address to a legacy account.

    Attributes:
        bind_token (str): Short-lived proof produced by the password step.
        email (str): Address to attach.
    """

    bind_token: str = Field(min_length=8, max_length=512)
    email: str = Field(min_length=3, max_length=254)


class PasswordChangeIn(BaseModel):
    """Payload for a self-service password rotation.

    Attributes:
        current_password (str): Existing password.
        new_password (str): Replacement password.
    """

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def _ip(request: Request) -> str:
    """Best-effort client IP, honouring the Vercel proxy header."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _agent(request: Request) -> str:
    """Client user agent, truncated for storage."""
    return request.headers.get("user-agent", "")[:300]


def _with_cookie(payload: Dict[str, Any], token: str) -> JSONResponse:
    """Build a JSON response that also stores the session cookie."""
    resp = JSONResponse(payload)
    resp.set_cookie(
        auth.COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=cfg.SESSION_DAYS * 86400,
    )
    return resp


@router.get("/api/auth/status")
async def auth_status():
    """Report first-run state, registration availability and storage mode.

    Returns:
        dict: ``{"first_run", "allow_registration", "ephemeral",
        "accounts_persist", "snapshot_sync", "mail_configured",
        "password_policy"}``. ``mail_configured`` is false when neither Resend
        nor SMTP is set, in which case the UI explains that codes cannot be
        delivered instead of showing a dead sign-up form.
    """
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        synced = snapshot.enabled()
        return {
            "first_run": count == 0,
            "allow_registration": get_setting(conn, "allow_registration", "1") == "1",
            "ephemeral": EPHEMERAL_STORAGE,
            "snapshot_sync": synced,
            "accounts_persist": (not EPHEMERAL_STORAGE) or synced,
            "mail_configured": mailer.configured() or mailer.echo_enabled(),
            "password_policy": {
                "min_length": auth.PASSWORD_MIN_LENGTH,
                "min_categories": auth.PASSWORD_MIN_CATEGORIES,
                "history_months": auth.PASSWORD_HISTORY_MONTHS,
                "max_age_days": auth.PASSWORD_MAX_AGE_DAYS,
                "warn_before_days": auth.PASSWORD_WARN_BEFORE_DAYS,
            },
        }


@router.post("/api/auth/signup/start")
async def signup_start(data: SignupIn, request: Request):
    """Validate a sign-up request and mail a verification code.

    Nothing is written to ``users`` yet: the account only exists once the code
    comes back, which removes the "registered but half-created" state that
    used to break the redirect after sign-up.

    Args:
        data (SignupIn): Address, password and optional display name.
        request (Request): Incoming request, used for the client IP.

    Returns:
        dict: ``challenge_id``, ``email_masked``, ``expires_in``, ``provider``
        and, in echo mode only, ``debug_code``.

    Raises:
        auth.AuthError: 422 for a bad address or password policy violation,
            403 when an administrator disabled self-registration, 409 when
            the address is already registered.
    """
    email = auth.normalize_email(data.email)
    auth.validate_password_policy(data.password)
    # Fail before a code is mailed: on a host that cannot keep accounts the
    # only honest answer is 503, and the user should not wait for an e-mail
    # that leads to a dead end.
    auth.require_durable_accounts()
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise auth.AuthError("An account already uses this e-mail address.", 409)
        first = auth.user_count(conn) == 0
        if not first and get_setting(conn, "allow_registration", "1") != "1":
            raise auth.AuthError(
                "Self-registration is disabled. Contact an administrator.", 403
            )
        result = otp.issue(
            conn,
            purpose="signup",
            email=email,
            payload={
                "password_hash": auth.hash_password(data.password),
                "display_name": data.display_name.strip(),
            },
            ip=_ip(request),
        )
    result["status"] = "otp_required"
    result["next"] = "/api/auth/signup/verify"
    return result


@router.post("/api/auth/signup/verify")
async def signup_verify(data: VerifyIn, request: Request):
    """Consume the sign-up code, create the account and sign the user in.

    Args:
        data (VerifyIn): Challenge handle plus the mailed code.
        request (Request): Incoming request, used for the user agent and IP.

    Returns:
        JSONResponse: ``{"user", "password", "provider"}`` plus the session
        cookie. A failed code returns 400 and leaves the pending account
        uncreated, so the browser can simply ask for a new code.
    """
    with get_conn() as conn:
        record = otp.verify(conn, data.challenge_id, data.code)
        if record["purpose"] != "signup":
            raise otp.OtpError("This code is not valid for sign-up.", 400)
        payload = json.loads(record.get("payload") or "{}")
        user = auth.register_hashed(
            conn,
            str(record["email"]),
            str(payload.get("password_hash") or ""),
            str(payload.get("display_name") or ""),
            _ip(request),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (user["username"],)
        ).fetchone()
        session = auth.start_session(
            conn, row, user_agent=_agent(request), ip=_ip(request)
        )
    snapshot.request_sync()
    return _with_cookie(
        {
            "user": session["user"],
            "password": session["password"],
            "provider": record["purpose"],
        },
        session["token"],
    )


@router.post("/api/auth/signin/start")
async def signin_start(data: SigninIn, request: Request):
    """Check the password and prepare the second factor.

    Args:
        data (SigninIn): Address (or legacy username) and password.
        request (Request): Incoming request, used for the client IP.

    Returns:
        dict: ``{"status": "otp_required", ...challenge}`` for a normal
        account, or ``{"status": "bind_required", "bind_token": ...}`` when
        the account predates e-mail sign-in and must bind an address first.

    Raises:
        auth.AuthError: 401 for unknown credentials, 403 for a disabled
            account.
    """
    with get_conn() as conn:
        row = auth.verify_signin(
            conn, data.identifier.strip(), data.password, ip=_ip(request)
        )
        email = str(row["email"] or "")
        if not email or int(row["email_verified"] or 0) != 1:
            return {
                "status": "bind_required",
                "username": row["username"],
                "bind_token": auth.issue_pending_token(
                    {"uid": int(row["id"]), "purpose": "bind", "un": row["username"]}
                ),
                "message": (
                    "This account was created before e-mail sign-in. "
                    "Confirm an address to continue."
                ),
            }
        result = otp.issue(
            conn,
            purpose="signin",
            email=email,
            username=row["username"],
            ip=_ip(request),
        )
    result["status"] = "otp_required"
    result["next"] = "/api/auth/signin/verify"
    return result


@router.post("/api/auth/signin/verify")
async def signin_verify(data: VerifyIn, request: Request):
    """Consume the sign-in code and issue the session.

    Args:
        data (VerifyIn): Challenge handle plus the mailed code.
        request (Request): Incoming request, used for the user agent and IP.

    Returns:
        JSONResponse: ``{"user", "password"}`` plus the session cookie.

    Raises:
        otp.OtpError: 400 for a consumed, expired or wrong code.
        auth.AuthError: 403 when the account was disabled meanwhile.
    """
    with get_conn() as conn:
        record = otp.verify(conn, data.challenge_id, data.code)
        if record["purpose"] != "signin":
            raise otp.OtpError("This code is not valid for sign-in.", 400)
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (record["username"],)
        ).fetchone()
        if row is None:
            raise auth.AuthError(
                "The account is not available on this instance. Try again.", 409
            )
        if str(row["email"] or "") != str(record["email"]):
            raise otp.OtpError("The e-mail address changed. Start sign-in again.", 400)
        if not row["enabled"]:
            raise auth.AuthError(
                "This account is disabled. Contact an administrator.", 403
            )
        session = auth.start_session(
            conn, row, user_agent=_agent(request), ip=_ip(request)
        )
    return _with_cookie(
        {"user": session["user"], "password": session["password"]},
        session["token"],
    )


@router.post("/api/auth/otp/resend")
async def otp_resend(data: ResendIn):
    """Send a fresh code for the same challenge.

    Args:
        data (ResendIn): Challenge handle.

    Returns:
        dict: Same shape as the original ``start`` response.
    """
    with get_conn() as conn:
        return otp.resend(conn, data.challenge_id)


@router.post("/api/auth/bind/start")
async def bind_start(data: BindStartIn, request: Request):
    """Mail a code to the address a legacy account wants to attach.

    Args:
        data (BindStartIn): Password proof token plus the requested address.
        request (Request): Incoming request, used for the client IP.

    Returns:
        dict: Challenge payload for the verification screen.

    Raises:
        auth.AuthError: 401 when the bind token is invalid or expired, 409
            when the address belongs to another account.
    """
    claims = auth.verify_pending_token(data.bind_token, "bind")
    if claims is None:
        raise auth.AuthError(
            "This sign-in attempt expired. Enter your password again.", 401
        )
    email = auth.normalize_email(data.email)
    with get_conn() as conn:
        taken = conn.execute(
            "SELECT id FROM users WHERE email=? AND id<>?",
            (email, int(claims["uid"])),
        ).fetchone()
        if taken is not None:
            raise auth.AuthError("An account already uses this e-mail address.", 409)
        result = otp.issue(
            conn,
            purpose="bind",
            email=email,
            username=str(claims.get("un") or ""),
            payload={"uid": int(claims["uid"])},
            ip=_ip(request),
        )
    result["status"] = "otp_required"
    return result


@router.post("/api/auth/bind/verify")
async def bind_verify(data: VerifyIn, request: Request):
    """Attach the proven address and complete the sign-in.

    Args:
        data (VerifyIn): Challenge handle plus the mailed code.
        request (Request): Incoming request, used for the user agent and IP.

    Returns:
        JSONResponse: ``{"user", "password"}`` plus the session cookie.
    """
    with get_conn() as conn:
        record = otp.verify(conn, data.challenge_id, data.code)
        if record["purpose"] != "bind":
            raise otp.OtpError("This code is not valid for e-mail binding.", 400)
        payload = json.loads(record.get("payload") or "{}")
        uid = int(payload.get("uid") or 0)
        row = auth.bind_email(conn, uid, str(record["email"]), ip=_ip(request))
        session = auth.start_session(
            conn, row, user_agent=_agent(request), ip=_ip(request)
        )
    snapshot.request_sync()
    return _with_cookie(
        {"user": session["user"], "password": session["password"]},
        session["token"],
    )


@router.post("/api/auth/password/change")
async def password_change(
    data: PasswordChangeIn,
    request: Request,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Rotate the signed-in user's password.

    Args:
        data (PasswordChangeIn): Current and replacement passwords.
        request (Request): Incoming request, used for the client IP.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"ok": True, "user", "password"}`` where ``password`` carries
        the refreshed 90-day window.
    """
    with get_conn() as conn:
        result = auth.change_password(
            conn,
            int(user["id"]),
            data.current_password,
            data.new_password,
            ip=_ip(request),
        )
    snapshot.request_sync()
    return {"ok": True, **result}


@router.get("/api/auth/me")
async def me(user: Dict[str, Any] = Depends(auth.current_user)):
    """Return the signed-in account plus its password-age banner state.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"user": ..., "password": ...}``.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"user": auth.public_user(row), "password": auth.password_state(row)}


@router.post("/api/auth/logout")
async def logout(request: Request):
    """Revoke the current session and clear the cookie.

    Args:
        request (Request): Incoming request carrying the session cookie.

    Returns:
        JSONResponse: ``{"ok": True}`` with an expired cookie.
    """
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token:
        with get_conn() as conn:
            # Also record the token as revoked: without that entry the stateless
            # fallback in ``user_by_token`` would happily accept it again, since
            # the ``sessions`` row alone is not the only way a token validates.
            auth.logout(conn, token, _ip(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp
