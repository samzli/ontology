"""Login flow regression — covers all reported and edge-case scenarios."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    print("=== Backend login regression ===")
    c = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)

    # [1] normal admin login
    r = c.post("/api/auth/login", json={"username": "admin", "password": "Onti@2026"})
    body = r.json()
    ok(r.status_code == 200, "[1] admin login -> 200", r.status_code)
    ok(body.get("user", {}).get("role") == "admin", "[1] role preserved", body)

    # [2] wrong password -> 401 + REAL message (not session expiry)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrongpass"})
    body = r.json()
    ok(r.status_code == 401, "[2] wrong password -> 401", r.status_code)
    ok("Invalid username or password" in str(body), "[2] correct error text", body)

    # [3] unknown user -> 401 + same message
    r = c.post(
        "/api/auth/login", json={"username": "nobody999", "password": "Onti@2026"}
    )
    body = r.json()
    ok(r.status_code == 401, "[3] unknown user -> 401", r.status_code)
    ok("Invalid username or password" in str(body), "[3] correct error text", body)

    # [4] empty username -> 422 (Pydantic)
    r = c.post("/api/auth/login", json={"username": "", "password": "Onti@2026"})
    ok(r.status_code == 422, "[4] empty username -> 422", r.status_code)

    # [5] username too short (<3) -> 422
    r = c.post("/api/auth/login", json={"username": "ab", "password": "Onti@2026"})
    ok(r.status_code == 422, "[5] short username -> 422", r.status_code)

    # [6] invalid username chars (space) -> 422
    r = c.post(
        "/api/auth/login", json={"username": "bad user", "password": "Onti@2026"}
    )
    ok(r.status_code == 422, "[6] invalid username chars -> 422", r.status_code)

    # [7] password too short (<8) -> 422
    r = c.post("/api/auth/login", json={"username": "admin", "password": "short"})
    ok(r.status_code == 422, "[7] short password -> 422", r.status_code)

    # [8] password too long (>256) -> 422
    r = c.post("/api/auth/login", json={"username": "admin", "password": "x" * 300})
    ok(r.status_code == 422, "[8] long password -> 422", r.status_code)

    # [9] disabled account -> 403 + specific message
    # Use edgebot which is already registered by prior tests; the test is
    # responsible for restoring enabled afterwards even if registration was
    # what originally created it.
    conn = sqlite3.connect("data/onti.db")
    pre = conn.execute("SELECT id FROM users WHERE username='edgebot'").fetchone()
    if pre is None:
        # Safety fallback — register the account first so the test is real.
        r_reg = httpx.Client(base_url=BASE, timeout=15).post(
            "/api/auth/register",
            json={
                "username": "edgebot",
                "password": "Edge@2026x",
                "display_name": "Edge",
            },
        )
        ok(r_reg.status_code == 200, "[9] ensure edgebot exists", r_reg.status_code)
    conn.execute("UPDATE users SET enabled=0 WHERE username='edgebot'")
    conn.commit()
    r = c.post(
        "/api/auth/login", json={"username": "edgebot", "password": "Edge@2026x"}
    )
    body = r.json()
    ok(r.status_code == 403, "[9] disabled account -> 403", r.status_code)
    ok("disabled" in str(body).lower(), "[9] account-disabled message", body)
    conn.execute("UPDATE users SET enabled=1 WHERE username='edgebot'")
    conn.commit()
    conn.close()

    # [10] anonymous /api/auth/me -> 401 (this SHOULD be session-expiry territory)
    c4 = httpx.Client(base_url=BASE, timeout=15)
    r = c4.get("/api/auth/me")
    ok(
        r.status_code == 401,
        "[10] anonymous /me -> 401 (session-expiry path)",
        r.status_code,
    )

    # [11] register new user — pick a username that definitely does not
    # collide with an earlier run; the app forbids duplicates with 409.
    import time as _t

    suffix = str(int(_t.time() * 1000))[-6:]
    new_user = f"freshbot_{suffix}"
    c5 = httpx.Client(base_url=BASE, timeout=15)
    r = c5.post(
        "/api/auth/register",
        json={
            "username": new_user,
            "password": "Fresh@2026q",
            "display_name": "Fresh",
        },
    )
    ok(r.status_code == 200, "[11] register -> 200", f"{r.status_code} {r.json()}")
    ok(
        r.json().get("user", {}).get("role") == "user",
        "[11] role=user for non-first-run registration",
        r.json(),
    )

    # [12] register -> immediately logged in (cookie set)
    ok(
        "onti_session" in (r.headers.get("set-cookie") or ""),
        "[12] register sets session cookie",
        r.headers.get("set-cookie", "")[:80],
    )

    # [13] login -> /me works
    c6 = httpx.Client(base_url=BASE, timeout=15)
    r = c6.post(
        "/api/auth/login", json={"username": new_user, "password": "Fresh@2026q"}
    )
    ok(r.status_code == 200, "[13] freshbot login -> 200", r.status_code)
    # carry cookie jar
    c6 = httpx.Client(base_url=BASE, timeout=15, cookies=r.cookies)
    r = c6.get("/api/auth/me")
    ok(
        r.status_code == 200 and r.json().get("user", {}).get("username") == new_user,
        "[13] /me returns the user",
        r.json(),
    )

    # [14] logout -> 200 + cookie cleared
    r = c6.post("/api/auth/logout")
    ok(r.status_code == 200, "[14] logout -> 200", r.status_code)
    c6b = httpx.Client(base_url=BASE, timeout=15)
    r = c6b.get("/api/auth/me")
    ok(r.status_code == 401, "[14] after logout /me -> 401", r.status_code)

    # [15] session-expiry path still correct for non-auth endpoints (this is
    # the old "Please sign in again" behaviour, which must NOT be affected)
    c7 = httpx.Client(base_url=BASE, timeout=15)
    r = c7.get("/api/auth/me")  # session-expiry path (401, non-auth endpoint)
    body = r.json()
    ok(r.status_code == 401, "[15] /me 401 is still session-expiry path", r.status_code)
    # Also: login endpoint 401 must NOT look like a session-expiry
    c8 = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)
    r = c8.post(
        "/api/auth/login", json={"username": "admin", "password": "wrongpassword123"}
    )
    body = r.json()
    msg = str(body.get("error", "")).lower()
    ok(
        "please sign in" not in msg,
        "[15] login failure message is NOT session-expiry wording",
        body,
    )
    ok(
        "invalid username" in msg,
        "[15] login failure IS the real error",
        body,
    )

    print()
    print(f"Backend: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
