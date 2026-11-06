"""Sign-in regression — validation, 2FA gate and token semantics.

Runs against the server started by ``python main.py`` (default port 8010).
The full happy path needs a mail transport; when the server has none (the
normal local case) those steps assert the explicit 503 contract instead of
silently passing, and ``verify_auth2fa.py`` covers the delivered-code journey
in-process with ``ONTI_MAIL_ECHO=1``.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import auth  # noqa: E402
from modules.db import get_conn  # noqa: E402

BASE = "http://127.0.0.1:8010"
PASS = 0
FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL {label}  :: {detail}")


def main() -> int:
    print("=== Backend sign-in regression ===")
    c = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)
    status = httpx.get(f"{BASE}/api/auth/status", timeout=15).json()
    mail_on = bool(status.get("mail_configured"))

    # [1] status advertises the new contract
    ok("mail_configured" in status, "[1] status exposes mail_configured", status)
    policy = status.get("password_policy") or {}
    ok(
        policy.get("min_length") == auth.PASSWORD_MIN_LENGTH
        and policy.get("min_categories") == auth.PASSWORD_MIN_CATEGORIES,
        "[1] status exposes the password policy",
        policy,
    )

    # [2] wrong password -> 401 with the real message
    r = c.post(
        "/api/auth/signin/start",
        json={"identifier": "admin", "password": "wrongpass"},
    )
    body = r.json()
    ok(r.status_code == 401, "[2] wrong password -> 401", r.status_code)
    ok("Invalid username or password" in str(body), "[2] correct error text", body)

    # [3] unknown account -> 401 + identical message (no user enumeration)
    r = c.post(
        "/api/auth/signin/start",
        json={"identifier": "nobody999@example.com", "password": "Onti@2026"},
    )
    ok(r.status_code == 401, "[3] unknown account -> 401", r.status_code)
    ok("Invalid username or password" in str(r.json()), "[3] same error text", r.text)

    # [4] empty identifier -> 422
    r = c.post("/api/auth/signin/start", json={"identifier": "", "password": "x"})
    ok(r.status_code == 422, "[4] empty identifier -> 422", r.status_code)

    # [5] over-long password -> 422
    r = c.post(
        "/api/auth/signin/start", json={"identifier": "admin", "password": "x" * 300}
    )
    ok(r.status_code == 422, "[5] long password -> 422", r.status_code)

    # [6] legacy account without an address -> one-time binding step
    with get_conn() as conn:
        row = conn.execute("SELECT email FROM users WHERE username='admin'").fetchone()
    if row is not None and not row["email"]:
        r = c.post(
            "/api/auth/signin/start",
            json={"identifier": "admin", "password": "Onti@2026"},
        )
        body = r.json()
        ok(
            r.status_code == 200 and body.get("status") == "bind_required",
            "[6] legacy account asks to bind an e-mail",
            body,
        )
        ok(bool(body.get("bind_token")), "[6] a short-lived bind token is issued")

    # [7] disabled account -> 403 + specific message
    conn = sqlite3.connect("data/onti.db")
    pre = conn.execute("SELECT id FROM users WHERE username='edgebot'").fetchone()
    if pre is None:
        with get_conn() as live:
            auth.register_account(live, "edgebot", "Edge@2026x", "Edge")
    conn.execute("UPDATE users SET enabled=0 WHERE username='edgebot'")
    conn.commit()
    r = c.post(
        "/api/auth/signin/start",
        json={"identifier": "edgebot", "password": "Edge@2026x"},
    )
    body = r.json()
    ok(r.status_code == 403, "[7] disabled account -> 403", r.status_code)
    ok("disabled" in str(body).lower(), "[7] account-disabled message", body)
    conn.execute("UPDATE users SET enabled=1 WHERE username='edgebot'")
    conn.commit()
    conn.close()

    # [8] anonymous /api/auth/me -> 401 (session-expiry path)
    r = httpx.Client(base_url=BASE, timeout=15).get("/api/auth/me")
    ok(r.status_code == 401, "[8] anonymous /me -> 401", r.status_code)

    # [9] sign-up validation happens before any mail is sent
    cases = [
        ("short", "bad@example.com", "short", 422),
        ("two classes", "weak@example.com", "abcdefghij", 422),
        ("bad address", "not-an-email", "Strong@2026x", 422),
    ]
    for label, email, pw, want in cases:
        r = c.post("/api/auth/signup/start", json={"email": email, "password": pw})
        ok(
            r.status_code == want,
            f"[9] {label} -> {want}",
            f"{r.status_code} {r.text[:120]}",
        )

    # [10] duplicate address -> 409 (checked before the code is issued)
    suffix = str(int(time.time() * 1000))[-6:]
    email = f"dupebot_{suffix}@example.com"
    with get_conn() as live:
        auth.register_account(live, email, "Dupe@2026xx")
    r = c.post(
        "/api/auth/signup/start", json={"email": email, "password": "Dupe@2026xx"}
    )
    ok(r.status_code == 409, "[10] duplicate address -> 409", r.status_code)

    # [11] sign-in for an account with an address asks for the mailed code,
    # or reports the missing transport explicitly — never a silent success.
    r = c.post(
        "/api/auth/signin/start", json={"identifier": email, "password": "Dupe@2026xx"}
    )
    body = r.json()
    if mail_on:
        ok(
            r.status_code == 200 and body.get("status") == "otp_required",
            "[11] sign-in requests the second factor",
            body,
        )
        ok(
            "debug_code" in body or body.get("provider") in ("resend", "smtp", "echo"),
            "[11] transport reported",
            body,
        )
    else:
        ok(
            r.status_code == 503 and "not configured" in str(body).lower(),
            "[11] no transport -> explicit 503",
            f"{r.status_code} {body}",
        )

    # [12] logout clears the cookie even without a session
    r = c.post("/api/auth/logout")
    ok(r.status_code == 200, "[12] logout -> 200", r.status_code)

    # [13] unauthenticated password change is rejected
    r = httpx.Client(base_url=BASE, timeout=15).post(
        "/api/auth/password/change",
        json={"current_password": "x", "new_password": "Strong@2026x"},
    )
    ok(r.status_code == 401, "[13] anonymous password change -> 401", r.status_code)

    # [14] password policy units
    ok(auth.password_categories("Abcdef1234!") == 4, "[14] four classes detected")
    ok(auth.password_categories("abcdefghij") == 1, "[14] one class detected")
    for bad in ("Short1!", "alllowercase", "ALLUPPERCASE", "1234567890"):
        try:
            auth.validate_password_policy(bad)
        except auth.AuthError as exc:
            ok(exc.status_code == 422, f"[14] rejects {bad!r}", exc.message)
        else:
            ok(False, f"[14] rejects {bad!r}", "accepted")
    auth.validate_password_policy("Good@Password1")
    ok(True, "[14] accepts a compliant password")

    # [15] cross-instance session semantics
    token = auth.issue_session_token(
        999999,
        "2099-01-01T00:00:00+00:00",
        username="ghost",
        display_name="Ghost",
        role="user",
    )
    claims = auth.verify_session_claims(token) or {}
    ok(claims.get("un") == "ghost", "[15] token carries identity claims", claims)
    with get_conn() as conn:
        ok(
            auth.user_by_token(conn, token) is None,
            "[15] persistent host rejects an unknown user",
        )
        saved = auth.EPHEMERAL_STORAGE
        auth.EPHEMERAL_STORAGE = True
        rebuilt = auth.user_by_token(conn, token)
        auth.EPHEMERAL_STORAGE = saved
    ok(rebuilt is None, "[15] token claims alone do not forge an account", rebuilt)

    # [16] status storage contract
    ok("accounts_persist" in status, "[16] status exposes accounts_persist")
    ok(
        status["accounts_persist"]
        == ((not status["ephemeral"]) or status["snapshot_sync"]),
        "[16] accounts_persist = persistent host or snapshot sync",
        status,
    )

    # ------------------------------------------------------------- clean-up ---
    with sqlite3.connect("data/onti.db") as conn:
        conn.execute("DELETE FROM users WHERE username='edgebot'")
        conn.execute("DELETE FROM users WHERE email=?", (email,))
        conn.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")

    print()
    print(f"Backend: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
