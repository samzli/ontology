"""End-to-end verification of the two-factor account system.

Runs entirely in-process on a throwaway database, so it never touches the
bundled ``data/onti.db``. Delivery uses ``ONTI_MAIL_ECHO=1``: the API returns
``debug_code`` and the send path is the same one Resend/SMTP use, which keeps
the local run behaviourally identical to Vercel apart from the transport.

Coverage:

  1. sign-up validation (length, character classes, address format)
  2. sign-up is not persisted until the code is verified
  3. wrong / expired / exhausted codes are rejected with clear statuses
  4. resend invalidates the previous code and honours the cooldown
  5. sign-in requires password *and* code — no cookie before both
  6. password rotation: policy, 24-month history, 90-day window
  7. the 90-day reminder state surfaced by ``/api/auth/me``
  8. legacy accounts bind an address once, then use normal 2FA
  9. username derivation and collision suffixes

Usage::

    python tests/verify_auth2fa.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="onti_auth2fa_")
os.environ["ONTI_DATA_DIR"] = _TMP
os.environ["ONTI_MAIL_ECHO"] = "1"
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("SMTP_HOST", None)

from fastapi.testclient import TestClient  # noqa: E402

import main as app_main  # noqa: E402
from modules import auth  # noqa: E402
from modules.db import get_conn, init_db  # noqa: E402

PASS = 0
FAIL = 0
COOKIE = auth.COOKIE_NAME


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  :: {detail}")


def _client() -> TestClient:
    return TestClient(app_main.app)


def _signup_start(client: TestClient, email: str, password: str, name: str = ""):
    return client.post(
        "/api/auth/signup/start",
        json={"email": email, "password": password, "display_name": name},
    )


def _signup(client: TestClient, email: str, password: str, name: str = ""):
    start = _signup_start(client, email, password, name)
    if start.status_code != 200:
        return start
    body = start.json()
    return client.post(
        "/api/auth/signup/verify",
        json={"challenge_id": body["challenge_id"], "code": body["debug_code"]},
    )


def _signin_start(client: TestClient, identifier: str, password: str):
    return client.post(
        "/api/auth/signin/start",
        json={"identifier": identifier, "password": password},
    )


def _signin(client: TestClient, identifier: str, password: str):
    start = _signin_start(client, identifier, password)
    if start.status_code != 200:
        return start
    body = start.json()
    if body.get("status") != "otp_required":
        return start
    return client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": body["challenge_id"], "code": body["debug_code"]},
    )


def _reset_cooldown(email: str, purpose: str) -> None:
    """Backdate the latest challenge so the cooldown does not block a test."""
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_otp SET created_at=? WHERE email=? AND purpose=?",
            (old, email, purpose),
        )


def _count(sql: str, params: tuple = ()) -> int:
    with get_conn() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def test_status(client: TestClient) -> None:
    print("[1] Status and policy contract")
    body = client.get("/api/auth/status").json()
    ok(body.get("mail_configured") is True, "[1] echo transport reported", body)
    ok(
        body.get("password_policy", {}).get("min_length") == 10,
        "[1] min length 10",
        body,
    )
    ok(
        body.get("password_policy", {}).get("history_months") == 24,
        "[1] history window 24 months",
        body,
    )
    ok(
        body.get("password_policy", {}).get("max_age_days") == 90,
        "[1] rotation window 90 days",
        body,
    )


def test_signup_validation(client: TestClient) -> None:
    print("[2] Sign-up validation")
    cases = [
        ("too short", "a@example.com", "Short1!", 422),
        ("one class", "b@example.com", "abcdefghij", 422),
        ("two classes", "c@example.com", "abcdefgh12", 422),
        ("bad address", "not-an-email", "Good@2026xx", 422),
        ("empty address", "", "Good@2026xx", 422),
    ]
    for label, email, pw, want in cases:
        r = _signup_start(client, email, pw)
        ok(
            r.status_code == want,
            f"[2] {label} -> {want}",
            f"{r.status_code} {r.text[:120]}",
        )


def test_signup_flow(client: TestClient) -> None:
    print("[3] Sign-up: verify-before-create")
    email = "alice@example.com"
    start = _signup_start(client, email, "Alice@2026x", "Alice Lee")
    ok(start.status_code == 200, "[3] start -> 200", start.text[:200])
    body = start.json()
    ok(bool(body.get("challenge_id")), "[3] challenge returned")
    ok(body.get("status") == "otp_required", "[3] status=otp_required", body)
    ok(body.get("email_masked") == "a***e@example.com", "[3] address masked", body)
    ok(bool(body.get("debug_code")), "[3] echo transport returns the code")
    ok(
        _count("SELECT COUNT(*) FROM users WHERE email=?", (email,)) == 0,
        "[3] account not created before the code is verified",
    )

    wrong = client.post(
        "/api/auth/signup/verify",
        json={"challenge_id": body["challenge_id"], "code": "000000"},
    )
    ok(wrong.status_code == 400, "[3] wrong code -> 400", wrong.text[:160])
    ok(
        wrong.json().get("attempts_left") == 4,
        "[3] attempts_left decrements",
        wrong.text[:160],
    )

    good = client.post(
        "/api/auth/signup/verify",
        json={"challenge_id": body["challenge_id"], "code": body["debug_code"]},
    )
    ok(good.status_code == 200, "[3] correct code -> 200", good.text[:200])
    user = good.json().get("user") or {}
    ok(user.get("email") == email, "[3] address stored", user)
    ok(user.get("username") == "alice", "[3] username derived from address", user)
    ok(user.get("email_verified") is True, "[3] address marked verified", user)
    ok(COOKIE in (good.headers.get("set-cookie") or ""), "[3] session cookie issued")
    ok(client.get("/api/auth/me").status_code == 200, "[3] session is live")
    ok(client.get("/app").status_code == 200, "[3] portal loads after sign-up")
    pw = good.json().get("password") or {}
    ok(pw.get("password_days_left") == 90, "[3] fresh 90-day window", pw)

    duplicate = _signup_start(client, email, "Alice@2026x")
    ok(
        duplicate.status_code == 409,
        "[3] duplicate address -> 409",
        duplicate.text[:160],
    )
    client.post("/api/auth/logout")
    ok(client.get("/api/auth/me").status_code == 401, "[3] logout ends the session")


def test_signin_flow(client: TestClient) -> None:
    print("[4] Sign-in requires both factors")
    email, password = "alice@example.com", "Alice@2026x"
    start = _signin_start(client, email, password)
    ok(start.status_code == 200, "[4] password accepted", start.text[:160])
    body = start.json()
    ok(body.get("status") == "otp_required", "[4] second factor demanded", body)
    ok(not body.get("user"), "[4] no identity leaked before the code", body)
    ok(COOKIE not in (start.headers.get("set-cookie") or ""), "[4] no cookie yet")
    ok(client.get("/api/auth/me").status_code == 401, "[4] still anonymous")

    wrong = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": body["challenge_id"], "code": "999999"},
    )
    ok(wrong.status_code == 400, "[4] wrong code -> 400", wrong.text[:160])

    good = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": body["challenge_id"], "code": body["debug_code"]},
    )
    ok(good.status_code == 200, "[4] correct code -> 200", good.text[:200])
    ok((good.json().get("user") or {}).get("username") == "alice", "[4] signed in")
    ok(client.get("/app").status_code == 200, "[4] portal loads")

    client.post("/api/auth/logout")
    bad = _signin_start(client, email, "WrongPassword1!")
    ok(bad.status_code == 401, "[4] wrong password -> 401", bad.status_code)


def test_otp_edges(client: TestClient) -> None:
    print("[5] Code lifecycle: expiry, reuse, resend, attempts")
    email, password = "alice@example.com", "Alice@2026x"
    _reset_cooldown(email, "signin")
    start = _signin_start(client, email, password).json()
    challenge, code = start["challenge_id"], start["debug_code"]

    # Expired challenge
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_otp SET expires_at=? WHERE challenge_id=?",
            (past, challenge),
        )
    r = client.post(
        "/api/auth/signin/verify", json={"challenge_id": challenge, "code": code}
    )
    ok(
        r.status_code == 400 and "expired" in r.text.lower(),
        "[5] expired -> 400",
        r.text,
    )

    # Cooldown blocks an immediate resend
    _reset_cooldown(email, "signin")
    fresh = _signin_start(client, email, password).json()
    second = client.post(
        "/api/auth/otp/resend", json={"challenge_id": fresh["challenge_id"]}
    )
    ok(
        second.status_code == 429,
        "[5] resend within cooldown -> 429",
        second.text[:160],
    )
    ok(second.json().get("retry_after", 0) > 0, "[5] retry_after reported", second.text)

    # After the cooldown a resend works and invalidates the old code
    _reset_cooldown(email, "signin")
    again = client.post(
        "/api/auth/otp/resend", json={"challenge_id": fresh["challenge_id"]}
    )
    ok(again.status_code == 200, "[5] resend after cooldown -> 200", again.text[:160])
    new = again.json()
    old_code = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": fresh["challenge_id"], "code": fresh["debug_code"]},
    )
    ok(
        old_code.status_code in (400, 404),
        "[5] superseded code no longer works",
        old_code.text[:160],
    )
    done = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": new["challenge_id"], "code": new["debug_code"]},
    )
    ok(done.status_code == 200, "[5] resent code works once", done.text[:160])
    client.post("/api/auth/logout")

    # Attempt budget: five wrong codes are rejected as 400, the sixth is 429.
    _reset_cooldown(email, "signin")
    third = _signin_start(client, email, password).json()
    for i in range(5):
        last = client.post(
            "/api/auth/signin/verify",
            json={"challenge_id": third["challenge_id"], "code": "000000"},
        )
        ok(
            last.status_code == 400,
            f"[5] wrong code {i + 1} -> 400",
            last.text[:160],
        )
    exhausted = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": third["challenge_id"], "code": "000000"},
    )
    ok(
        exhausted.status_code == 429,
        "[5] sixth attempt -> 429",
        exhausted.text[:160],
    )
    ok(
        exhausted.json().get("restart") is True,
        "[5] restart flag set",
        exhausted.text[:160],
    )
    good = client.post(
        "/api/auth/signin/verify",
        json={"challenge_id": third["challenge_id"], "code": third["debug_code"]},
    )
    ok(
        good.status_code == 429,
        "[5] exhausted challenge cannot be reused",
        good.text[:120],
    )


def test_password_change(client: TestClient) -> None:
    print("[6] Password rotation")
    email, password = "alice@example.com", "Alice@2026x"
    anon = _client()
    r = anon.post(
        "/api/auth/password/change",
        json={"current_password": password, "new_password": "NewAlice@2027"},
    )
    ok(r.status_code == 401, "[6] anonymous change -> 401", r.status_code)

    _reset_cooldown(email, "signin")
    ok(_signin(client, email, password).status_code == 200, "[6] signed in")
    wrong = client.post(
        "/api/auth/password/change",
        json={"current_password": "nope", "new_password": "NewAlice@2027"},
    )
    ok(wrong.status_code == 401, "[6] wrong current password -> 401", wrong.text[:160])

    short = client.post(
        "/api/auth/password/change",
        json={"current_password": password, "new_password": "Short1!"},
    )
    ok(short.status_code == 422, "[6] too short -> 422", short.text[:160])

    same = client.post(
        "/api/auth/password/change",
        json={"current_password": password, "new_password": password},
    )
    ok(same.status_code == 422, "[6] reuse of current password -> 422", same.text[:160])

    changed = client.post(
        "/api/auth/password/change",
        json={"current_password": password, "new_password": "NewAlice@2027"},
    )
    ok(changed.status_code == 200, "[6] valid change -> 200", changed.text[:200])
    state = changed.json().get("password") or {}
    ok(state.get("password_days_left") == 90, "[6] window reset to 90 days", state)

    ok(
        _signin_start(anon, email, password).status_code == 401,
        "[6] old password rejected",
    )
    _reset_cooldown(email, "signin")
    ok(
        _signin(anon, email, "NewAlice@2027").status_code == 200,
        "[6] new password signs in",
    )

    # Rotate again, then try to go back to the first password (24-month rule)
    second = client.post(
        "/api/auth/password/change",
        json={"current_password": "NewAlice@2027", "new_password": "ThirdAlice@2028"},
    )
    ok(second.status_code == 200, "[6] second rotation -> 200", second.text[:160])
    back = client.post(
        "/api/auth/password/change",
        json={"current_password": "ThirdAlice@2028", "new_password": "Alice@2026x"},
    )
    ok(back.status_code == 422, "[6] 24-month history blocks reuse", back.text[:160])
    anon.cookies.clear()


def test_expiry_reminder(client: TestClient) -> None:
    print("[7] 90-day reminder state")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email='alice@example.com'"
        ).fetchone()
        uid = row["id"]
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("UPDATE users SET password_changed_at=? WHERE id=?", (fresh, uid))
    body = client.get("/api/auth/me").json()
    state = body.get("password") or {}
    ok(state.get("password_expired") is False, "[7] fresh password not expired", state)
    ok(state.get("password_warning") is False, "[7] no reminder when fresh", state)

    with get_conn() as conn:
        warn = (datetime.now(timezone.utc) - timedelta(days=80)).isoformat(
            timespec="seconds"
        )
        conn.execute("UPDATE users SET password_changed_at=? WHERE id=?", (warn, uid))
    state = client.get("/api/auth/me").json()["password"]
    ok(state.get("password_warning") is True, "[7] reminder inside 14 days", state)
    ok(state.get("password_days_left") == 10, "[7] days-left is accurate", state)

    with get_conn() as conn:
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(
            timespec="seconds"
        )
        conn.execute("UPDATE users SET password_changed_at=? WHERE id=?", (old, uid))
    state = client.get("/api/auth/me").json()["password"]
    ok(state.get("password_expired") is True, "[7] expired after 90 days", state)
    ok(state.get("password_days_left") == 0, "[7] no negative days", state)

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_changed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), uid),
        )


def test_legacy_bind() -> None:
    print("[8] Legacy account binds an address once")
    anon = _client()
    with get_conn() as conn:
        auth.create_user(conn, "legacyuser", "Legacy@2026x", "Legacy User")
    start = _signin_start(anon, "legacyuser", "Legacy@2026x")
    ok(
        start.status_code == 200,
        "[8] legacy sign-in reaches the bind step",
        start.text[:160],
    )
    body = start.json()
    ok(body.get("status") == "bind_required", "[8] bind_required", body)
    token = body.get("bind_token") or ""
    ok(bool(token), "[8] bind token issued")
    ok(anon.get("/api/auth/me").status_code == 401, "[8] no session before binding")

    bound = anon.post(
        "/api/auth/bind/start",
        json={"bind_token": token, "email": "legacy@example.com"},
    )
    ok(bound.status_code == 200, "[8] bind code sent", bound.text[:160])
    code = bound.json()["debug_code"]
    done = anon.post(
        "/api/auth/bind/verify",
        json={"challenge_id": bound.json()["challenge_id"], "code": code},
    )
    ok(done.status_code == 200, "[8] bind completes the sign-in", done.text[:200])
    ok(
        (done.json().get("user") or {}).get("email") == "legacy@example.com",
        "[8] address attached",
    )
    anon.post("/api/auth/logout")

    _reset_cooldown("legacy@example.com", "signin")
    again = _signin(anon, "legacy@example.com", "Legacy@2026x")
    ok(
        again.status_code == 200,
        "[8] subsequent sign-in is normal 2FA",
        again.text[:160],
    )

    forged = _client().post(
        "/api/auth/bind/start",
        json={"bind_token": "not-a-token", "email": "another@example.com"},
    )
    ok(forged.status_code == 401, "[8] forged bind token -> 401", forged.status_code)


def test_username_collision() -> None:
    print("[9] Username derivation and collisions")
    with get_conn() as conn:
        auth.create_user(conn, "bob", "Bob@2026xxx", "Historical Bob")
    anon = _client()
    done = _signup(anon, "bob@example.com", "Bobby@2026x")
    ok(done.status_code == 200, "[9] sign-up still succeeds", done.text[:160])
    user = done.json().get("user") or {}
    ok(user.get("username") == "bob-2", "[9] collision suffixed", user)
    with get_conn() as conn:
        ok(
            conn.execute("SELECT 1 FROM users WHERE username='bob'").fetchone()
            is not None,
            "[9] the legacy 'bob' is untouched",
        )
    ok(
        auth.derive_username("alice.w+news@example.com") == "alice.w-news",
        "[9] sanitising",
    )


def test_registration_toggle() -> None:
    print("[10] Administrator can close registration")
    admin = _client()
    with get_conn() as conn:
        auth.create_user(conn, "admin2", "Admin@2026xx", "Admin Two", "admin")
        token = auth.authenticate(conn, "admin2", "Admin@2026xx")["token"]
    admin.cookies.set(COOKIE, token)
    off = admin.put("/api/admin/settings", json={"allow_registration": False})
    ok(off.status_code == 200, "[10] setting updated", off.status_code)
    anon = _client()
    r = _signup_start(anon, "closed@example.com", "Closed@2026x")
    ok(r.status_code == 403, "[10] closed registration -> 403", r.text[:160])
    admin.put("/api/admin/settings", json={"allow_registration": True})


def test_pages(client: TestClient) -> None:
    print("[11] Pages: sign-up, sign-in, password, reminder banner")
    anon = _client()
    login = anon.get("/login")
    ok(login.status_code == 200, "[11] /login renders", login.status_code)
    ok("/signup" in login.text, "[11] /login links to sign-up")
    ok("verification code" in login.text.lower(), "[11] /login mentions the code")

    signup = anon.get("/signup")
    ok(signup.status_code == 200, "[11] /signup renders", signup.status_code)
    ok('id="fEmail"' in signup.text, "[11] /signup asks for an address")
    ok("10" in signup.text, "[11] /signup shows the length rule")

    gate = anon.get("/account/password", follow_redirects=False)
    ok(
        gate.status_code in (302, 307) and "/login" in gate.headers.get("location", ""),
        "[11] password page redirects anonymous visitors",
        f"{gate.status_code} {gate.headers.get('location')}",
    )

    pw = client.get("/account/password")
    ok(pw.status_code == 200, "[11] /account/password renders", pw.status_code)
    ok("Change password" in pw.text, "[11] password page has its heading")
    app = client.get("/app")
    ok(app.status_code == 200, "[11] portal renders", app.status_code)
    ok('id="pwNotice"' in app.text, "[11] portal has the reminder slot")
    ok(
        "/account/password" in app.text,
        "[11] portal sidebar links to the password page",
    )


def main() -> int:
    init_db()
    client = _client()
    test_status(client)
    test_signup_validation(client)
    test_signup_flow(client)
    test_signin_flow(client)
    test_otp_edges(client)
    test_password_change(client)
    test_expiry_reminder(client)
    test_legacy_bind()
    test_username_collision()
    test_registration_toggle()
    test_pages(client)
    print()
    print(f"Auth 2FA: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
