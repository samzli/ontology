"""End-to-end smoke test for the MFA + preset-account flow.

Runs against the local server started with ``ONTI_MAIL_ECHO=1`` on port
8010. The preset accounts created by ``seed_preset_accounts.py`` are used
as fixtures.
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8010"
PRESET_PW = "OntiDeloitte@2026"
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
    global PASS, FAIL
    print("=== MFA + preset accounts regression ===")
    c = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)

    # [1] status has mfa_available + mail_configured
    s = c.get("/api/auth/status").json()
    ok(s.get("mfa_available") is True, "[1] mfa_available=true", s)
    ok(s.get("mail_configured") is True, "[1] mail_configured=true (echo)", s)

    # [2] duplicate signup friendly 409 message
    r = c.post(
        "/api/auth/signup/start",
        json={
            "email": "dais_hk_life@deloitte.com.hk",
            "password": PRESET_PW,
            "display_name": "dup",
        },
    )
    ok(r.status_code == 409, "[2] duplicate signup -> 409", r.status_code)
    ok(
        "already" in r.text.lower()
        or "exists" in r.text.lower()
        or "account" in r.text.lower(),
        "[2] friendly 'already exists' message",
        r.text[:120],
    )

    # [3] signin a preset account — shared account, password alone suffices
    r = c.post(
        "/api/auth/signin/start",
        json={
            "identifier": "dais_hk_life",
            "password": PRESET_PW,
        },
    )
    ok(r.status_code == 200, "[3] preset account password ok", r.status_code)
    body = r.json()
    ok(
        body.get("status") == "ok",
        "[3] shared account signs in directly (no e-mail OTP)",
        body,
    )
    cookies = (
        {"onti_session": r.cookies.get("onti_session")}
        if r.cookies.get("onti_session")
        else {}
    )
    ok(bool(cookies), "[3] session cookie set on direct sign-in", dict(r.cookies))

    # [5] /api/auth/me shows mfa disabled by default
    r = c.get("/api/auth/me", cookies=cookies)
    ok(r.status_code == 200, "[5] /me -> 200", r.status_code)
    me = r.json()
    ok(
        me.get("mfa", {}).get("available") is True,
        "[5] mfa.available=true",
        me.get("mfa"),
    )
    ok(
        me.get("mfa", {}).get("enabled") is False,
        "[5] mfa.enabled=false by default",
        me.get("mfa"),
    )

    # [6] start MFA bind -> get QR + token
    r = c.get("/api/auth/mfa/bind", cookies=cookies)
    ok(r.status_code == 200, "[6] /mfa/bind -> 200", r.status_code)
    bind = r.json()
    ok(bool(bind.get("secret")), "[6] secret returned")
    ok(bool(bind.get("uri")), "[6] provisioning URI returned")
    ok(bind.get("uri", "").startswith("otpauth://"), "[6] URI starts with otpauth://")
    ok(
        bool(bind.get("qr_svg")) and "<svg" in bind.get("qr_svg", ""),
        "[6] SVG QR returned",
    )
    ok(bool(bind.get("mfa_token")), "[6] mfa_token returned")

    # [7] verify MFA bind with a real TOTP code
    import pyotp

    totp = pyotp.TOTP(bind["secret"])
    code_now = totp.now()
    r = c.post(
        "/api/auth/mfa/bind/verify",
        json={
            "mfa_token": bind["mfa_token"],
            "code": code_now,
        },
        cookies=cookies,
    )
    ok(r.status_code == 200, f"[7] bind verify -> 200 (code={code_now})", r.text[:100])
    ok(
        r.json().get("ok") is True and r.json().get("mfa_enabled") is True,
        "[7] mfa_enabled flipped true",
        r.json(),
    )

    # [8] wrong code -> 400
    r = c.post(
        "/api/auth/mfa/bind/verify",
        json={
            "mfa_token": bind["mfa_token"],
            "code": "000000",
        },
        cookies=cookies,
    )
    ok(r.status_code == 400, "[8] wrong TOTP -> 400", r.status_code)

    # [9] logout then signin — should take MFA path now
    c.post("/api/auth/logout", cookies=cookies)
    cookies = {}
    r = c.post(
        "/api/auth/signin/start",
        json={
            "identifier": "dais_hk_life",
            "password": PRESET_PW,
        },
    )
    ok(r.status_code == 200, "[9] signin password ok (MFA-enabled account)")
    body = r.json()
    ok(
        body.get("status") == "mfa_required",
        "[9] MFA-enabled account gets mfa_required",
        body,
    )
    mfa_token = body.get("mfa_token")
    ok(bool(mfa_token), "[9] mfa_token returned")

    # [10] verify signin with fresh TOTP code
    code_now2 = pyotp.TOTP(bind["secret"]).now()
    r = c.post(
        "/api/auth/signin/mfa/verify",
        json={
            "mfa_token": mfa_token,
            "code": code_now2,
        },
    )
    ok(
        r.status_code == 200,
        f"[10] MFA verify -> 200 (code={code_now2})",
        r.status_code,
    )
    cookies = (
        {"onti_session": r.cookies.get("onti_session")}
        if r.cookies.get("onti_session")
        else {}
    )
    ok(bool(cookies), "[10] session cookie set after MFA")

    # [11] wrong MFA code -> 400
    r = c.post(
        "/api/auth/signin/mfa/verify",
        json={
            "mfa_token": mfa_token,
            "code": "000000",
        },
    )
    ok(r.status_code == 400, "[11] wrong MFA code -> 400", r.status_code)

    # [12] unbind MFA with password
    r = c.post(
        "/api/auth/mfa/unbind", json={"current_password": PRESET_PW}, cookies=cookies
    )
    ok(r.status_code == 200, "[12] mfa/unbind -> 200", r.status_code)
    ok(
        r.json().get("mfa_enabled") is False,
        "[12] mfa_enabled=false after unbind",
        r.json(),
    )

    # [13] wrong password on unbind -> 401
    r = c.post(
        "/api/auth/mfa/unbind", json={"current_password": "wrongpw"}, cookies=cookies
    )
    ok(r.status_code == 401, "[13] wrong password on unbind -> 401", r.status_code)

    # [14] after unbind, signin is back to password-only direct sign-in
    c.post("/api/auth/logout", cookies=cookies)
    r = c.post(
        "/api/auth/signin/start",
        json={
            "identifier": "dais_hk_life",
            "password": PRESET_PW,
        },
    )
    body = r.json()
    ok(
        body.get("status") == "ok",
        "[14] after unbind, shared account signs in directly again",
        body,
    )

    # [15] other preset accounts exist + sign in directly (no MFA by default)
    for username in ("dais_hk_gi", "dais_ml_life", "dais_ml_gi"):
        r = c.post(
            "/api/auth/signin/start",
            json={
                "identifier": username,
                "password": PRESET_PW,
            },
        )
        ok(r.status_code == 200, f"[15] {username} password ok", r.status_code)
        ok(
            r.json().get("status") == "ok",
            f"[15] {username} direct sign-in (no e-mail, no MFA)",
            r.json(),
        )
        ok(
            bool(r.cookies.get("onti_session")),
            f"[15] {username} session cookie set",
            dict(r.cookies),
        )

    print(f"\n=== {PASS} ok / {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
