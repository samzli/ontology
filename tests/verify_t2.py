"""T2 verification: hashing, sessions, registration gating, user management.

(HTTP 401/403 matrix via FastAPI is covered again in tests/smoke_http.py once
routers exist; this script verifies the service layer end to end.)

The fixtures use a per-run suffix and are deleted again at the end, so the
script is safe to run against a workspace that already has accounts.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.db import get_conn, set_setting
from modules.auth import (
    AuthError,
    authenticate,
    create_user,
    hash_password,
    logout,
    register_account,
    reset_password,
    set_user_enabled,
    set_user_role,
    user_by_token,
    verify_password,
)

failures = []
SUFFIX = f"{int(time.time())}"


def check(name, cond):
    print(f"  {'OK ' if cond else 'BAD'} {name}")
    if not cond:
        failures.append(name)


ROOT = f"rootadmin_{SUFFIX}"
ALICE = f"alice_{SUFFIX}"
BOB = f"bob_{SUFFIX}"

with get_conn() as conn:
    # Registration rule: the first account on an empty install becomes admin;
    # on a workspace that already has accounts a registration is a normal user.
    empty_install = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
    if empty_install:
        admin = register_account(
            conn, ROOT, "AdminPass123", "Root Admin", ip="127.0.0.1"
        )
        check("first account is admin", admin["role"] == "admin" and admin["enabled"])
    else:
        create_user(conn, ROOT, "AdminPass123", "Root Admin", role="admin")
        admin = dict(
            conn.execute("SELECT * FROM users WHERE username=?", (ROOT,)).fetchone()
        )
        check(
            "registration on a populated workspace is a plain user",
            register_account(conn, f"probe_{SUFFIX}", "ProbePass123")["role"] == "user",
        )

    # Wrong password -> 401
    try:
        authenticate(conn, ROOT, "wrong-password")
        check("wrong password rejected", False)
    except AuthError as e:
        check("wrong password rejected (401)", e.status_code == 401)

    # Right password -> session token works
    sess = authenticate(conn, ROOT, "AdminPass123", ip="127.0.0.1", user_agent="pytest")
    token = sess["token"]
    check("login returns token", bool(token) and sess["user"]["role"] == "admin")
    row = user_by_token(conn, token)
    check("session resolves to user", row is not None and row["username"] == ROOT)

    # Password hash storage
    stored = conn.execute(
        "SELECT password_hash FROM users WHERE username=?", (ROOT,)
    ).fetchone()["password_hash"]
    check("hash is pbkdf2 format", stored.startswith("pbkdf2_sha256$"))
    check("plaintext not in db", "AdminPass123" not in stored)
    check(
        "verify_password roundtrip",
        verify_password("AdminPass123", hash_password("AdminPass123")),
    )

    # Logout invalidates session
    logout(conn, token)
    check("session gone after logout", user_by_token(conn, token) is None)

    # Admin creates a normal user
    uid = create_user(conn, ALICE, "AlicePass123", "Alice", role="user")
    check("created user alice", uid > 0)

    # Self-protection
    try:
        set_user_role(conn, admin, admin["id"], "user")
        check("self demote blocked", False)
    except AuthError as e:
        check("self demote blocked (400)", e.status_code == 400)
    try:
        set_user_enabled(conn, admin, admin["id"], False)
        check("self disable blocked", False)
    except AuthError as e:
        check("self disable blocked (400)", e.status_code == 400)

    # User management lifecycle
    set_user_role(conn, admin, uid, "admin")
    role_now = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()[
        "role"
    ]
    check("promote alice -> admin", role_now == "admin")
    set_user_role(conn, admin, uid, "user")
    reset_password(conn, admin, uid, "NewAlice456!")
    check(
        "alice can login with reset password",
        authenticate(conn, ALICE, "NewAlice456!")["user"]["username"] == ALICE,
    )
    set_user_enabled(conn, admin, uid, False)
    try:
        authenticate(conn, ALICE, "NewAlice456!")
        check("disabled user blocked", False)
    except AuthError as e:
        check("disabled user blocked (403)", e.status_code == 403)
    set_user_enabled(conn, admin, uid, True)

    # Validation
    for bad_user, bad_pw in [("ab", "longenough1"), ("okuser", "short")]:
        try:
            create_user(conn, bad_user, bad_pw)
            check(f"validation rejected {bad_user}", False)
        except AuthError as e:
            check(
                f"validation rejected {bad_user} ({e.status_code})",
                e.status_code == 422,
            )

    # Duplicate username
    try:
        create_user(conn, ALICE, "AnotherPass123")
        check("duplicate username blocked", False)
    except AuthError as e:
        check("duplicate username blocked (409)", e.status_code == 409)

    # Registration toggle
    set_setting(conn, "allow_registration", "0")
    try:
        register_account(conn, BOB, "BobPassword123")
        check("registration disabled enforced", False)
    except AuthError as e:
        check("registration disabled enforced (403)", e.status_code == 403)
    set_setting(conn, "allow_registration", "1")
    bob = register_account(conn, BOB, "BobPassword123")
    check("registration enabled creates user", bob["role"] == "user")

    # Audit log: must contain both success and failure events
    actions = {
        r["action"]
        for r in conn.execute("SELECT DISTINCT action FROM audit_log").fetchall()
    }
    check("audit has login_success", "auth.login_success" in actions)
    check("audit has login_failed", "auth.login_failed" in actions)
    check("audit has logout", "auth.logout" in actions)
    n_fail = conn.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action='auth.login_failed'"
    ).fetchone()["n"]
    check("failed attempts recorded", n_fail >= 1)

    # Cleanup: leave the workspace exactly as we found it.
    created = [ROOT, ALICE, BOB, f"probe_{SUFFIX}"]
    conn.execute(
        "DELETE FROM users WHERE username IN (%s)" % ",".join("?" * len(created)),
        created,
    )

print("\nT2 verification:", "PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)
