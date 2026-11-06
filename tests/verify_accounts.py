"""Account lifecycle regression — creation, sign-in, switching and storage.

The reported bug: a newly registered account was bounced straight back to the
sign-in page (and then rejected with "Invalid username or password") because
serverless instances each kept their own copy of the database. These checks
cover the service layer against a throwaway account store plus the HTTP flow
against a running server on port 8010.

Run after starting the app (``python main.py``) like the other verify scripts.
"""

from __future__ import annotations

import http.server
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8010"
DB_PATH = ROOT / "data" / "onti.db"
PASS = 0
FAIL = 0
CREATED = []


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL {label}  :: {detail}")


def unique(prefix: str) -> str:
    """Return a collision-free username that matches the synthetic prefixes."""
    return f"{prefix}_{str(int(time.time() * 1000))[-7:]}"


class _AccountStore(http.server.BaseHTTPRequestHandler):
    """Stand-in for the operator's account endpoint (PUT/GET/DELETE)."""

    records: dict = {}
    fail_put = False

    def _key(self) -> str:
        return (parse_qs(urlparse(self.path).query).get("account") or [""])[0]

    def _reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PUT(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        if _AccountStore.fail_put:
            self._reply(500)
            return
        _AccountStore.records[self._key()] = payload
        self._reply(200)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        data = _AccountStore.records.get(self._key())
        self._reply(200, data) if data is not None else self._reply(404)

    def do_DELETE(self):  # noqa: N802 - BaseHTTPRequestHandler API
        _AccountStore.records.pop(self._key(), None)
        self._reply(200)

    def log_message(self, *args):  # keep the test output clean
        pass


def _service_checks(store_url: str) -> None:
    """Exercise registration, adoption and mutation sync in-process."""
    from modules import auth
    from modules.db import get_conn

    saved_ephemeral = auth.EPHEMERAL_STORAGE
    auth.EPHEMERAL_STORAGE = True
    try:
        # [1] no durable store -> registration is refused, no phantom row
        user = unique("acct_guard")
        os.environ.pop("ONTI_SNAPSHOT_URL", None)
        try:
            with get_conn() as conn:
                auth.register_account(conn, user, "Guard@2026x")
            raised = None
        except auth.AuthError as exc:
            raised = exc
        with get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username=?", (user,)
            ).fetchone()
        ok(
            raised is not None and raised.status_code == 503 and exists is None,
            "[1] registration refused without durable storage",
            f"{raised} exists={exists}",
        )

        os.environ["ONTI_SNAPSHOT_URL"] = store_url
        os.environ["ONTI_SNAPSHOT_TOKEN"] = "test-token"

        # [2] registration writes the account record before committing
        user = unique("acct_new")
        CREATED.append(user)
        with get_conn() as conn:
            created = auth.register_account(conn, user, "New@2026pass", "New User")
        key = __import__("modules.snapshot", fromlist=["x"])._account_key(user)
        record = _AccountStore.records.get(key)
        stored = json.loads(record) if record else {}
        ok(
            stored.get("username") == user and stored.get("role") == "user",
            "[2] registration persists the account record",
            stored,
        )
        ok(created.get("username") == user, "[2] public projection returned", created)

        # [3] a fresh instance that never saw the row adopts it at sign-in
        with sqlite3.connect(str(DB_PATH)) as raw:
            raw.execute("DELETE FROM users WHERE username=?", (user,))
        with get_conn() as conn:
            adopted = auth.authenticate(conn, user, "New@2026pass")
        ok(
            adopted["user"]["username"] == user,
            "[3] missing row is adopted from the account store",
            adopted["user"],
        )

        # [4] a stale local hash still accepts the stored (newer) password
        new_hash = auth.hash_password("Rotated@2026")
        _AccountStore.records[key] = json.dumps(
            {
                "username": user,
                "display_name": "New User",
                "password_hash": new_hash,
                "role": "user",
                "enabled": 1,
                "created_at": "",
            }
        ).encode()
        with get_conn() as conn:
            refreshed = auth.authenticate(conn, user, "Rotated@2026")
        ok(
            refreshed["user"]["username"] == user,
            "[4] password reset on another instance is honoured",
            refreshed["user"],
        )

        # [5] admin mutations mirror to the store
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE username=?", (user,)
            ).fetchone()
            admin = {"id": -1, "username": "admin"}
            auth.set_user_role(conn, admin, row["id"], "admin")
            auth.set_user_enabled(conn, admin, row["id"], False)
            auth.reset_password(conn, admin, row["id"], "Again@2026x")
        stored = json.loads(_AccountStore.records[key])
        ok(
            stored["role"] == "admin" and not int(stored["enabled"]),
            "[5] role/enabled changes are persisted",
            stored,
        )
        ok(
            auth.verify_password("Again@2026x", stored["password_hash"]),
            "[5] password reset is persisted",
        )
        with get_conn() as conn:
            auth.set_user_enabled(
                conn, {"id": -1, "username": "admin"}, row["id"], True
            )

        # [6] a failed upload rolls the registration back (no ghost account)
        ghost = unique("acct_ghost")
        _AccountStore.fail_put = True
        try:
            with get_conn() as conn:
                auth.register_account(conn, ghost, "Ghost@2026x")
            failed = None
        except auth.AuthError as exc:
            failed = exc
        finally:
            _AccountStore.fail_put = False
        with get_conn() as conn:
            lingering = conn.execute(
                "SELECT 1 FROM users WHERE username=?", (ghost,)
            ).fetchone()
        ok(
            failed is not None and failed.status_code == 503 and lingering is None,
            "[6] failed persistence leaves no phantom account",
            f"{failed} lingering={lingering}",
        )

        # [7] validation boundaries
        boundary = []
        for username, password, code in (
            (user, "Whatever@2026", 409),
            ("ab", "Whatever@2026", 422),
            ("bad user", "Whatever@2026", 422),
            ("x" * 65, "Whatever@2026", 422),
            (unique("acct_short"), "short", 422),
            (unique("acct_long"), "y" * 257, 422),
        ):
            try:
                with get_conn() as conn:
                    auth.register_account(conn, username, password)
                got = 200
            except auth.AuthError as exc:
                got = exc.status_code
            boundary.append((username[:16], got, code))
        ok(
            all(got == code for _, got, code in boundary),
            "[7] duplicate / short / invalid / oversized inputs rejected",
            boundary,
        )

        # [8] deleting an account record removes only that account
        from modules import snapshot

        ok(
            snapshot.delete_account(user) and key not in _AccountStore.records,
            "[8] delete_account removes the record",
        )
        ok(
            snapshot._account_key(" Alice ") == snapshot._account_key("alice"),
            "[8] account keys ignore case and whitespace",
        )
    finally:
        auth.EPHEMERAL_STORAGE = saved_ephemeral


def _http_checks() -> None:
    """Full create / sign-in / switch flow against the running server."""
    client = httpx.Client(base_url=BASE, timeout=20, follow_redirects=False)
    user_a = unique("acct_a")
    user_b = unique("acct_b")
    CREATED.extend([user_a, user_b])
    try:
        status = client.get("/api/auth/status")
        ok(status.status_code == 200, "[9] status endpoint answers", status.status_code)
        info = status.json()
        ok(
            info.get("accounts_persist") is True,
            "[9] local host reports durable accounts",
            info,
        )

        # [10] register -> /app renders -> /me resolves
        reg = client.post(
            "/api/auth/register",
            json={"username": user_a, "password": "Alpha@2026x", "display_name": "A"},
        )
        ok(reg.status_code == 200, "[10] register -> 200", reg.text[:200])
        ok(
            "onti_session" in (reg.headers.get("set-cookie") or ""),
            "[10] register signs the user in",
        )
        app_page = client.get("/app")
        ok(
            app_page.status_code == 200 and b"Sign in" not in app_page.content[:400],
            "[10] /app loads after registration",
            app_page.status_code,
        )
        me = client.get("/api/auth/me")
        ok(
            me.status_code == 200 and me.json()["user"]["username"] == user_a,
            "[10] /me resolves the new account",
            me.text[:200],
        )

        # [11] logout -> sign in again
        ok(client.post("/api/auth/logout").status_code == 200, "[11] logout -> 200")
        ok(
            client.get("/api/auth/me").status_code == 401,
            "[11] /me after logout -> 401",
        )
        again = client.post(
            "/api/auth/login",
            json={"username": user_a, "password": "Alpha@2026x"},
        )
        ok(again.status_code == 200, "[11] sign in again -> 200", again.text[:200])

        # [12] two accounts stay separate in two browsers
        other = httpx.Client(base_url=BASE, timeout=20, follow_redirects=False)
        reg_b = other.post(
            "/api/auth/register",
            json={"username": user_b, "password": "Bravo@2026x", "display_name": "B"},
        )
        ok(reg_b.status_code == 200, "[12] second account registers", reg_b.text[:200])
        me_b = other.get("/api/auth/me")
        me_a = client.get("/api/auth/me")
        ok(
            me_b.json()["user"]["username"] == user_b
            and me_a.json()["user"]["username"] == user_a,
            "[12] multi-account sessions stay isolated",
            f"{me_a.text[:80]} | {me_b.text[:80]}",
        )
        ok(
            other.post("/api/auth/logout").status_code == 200
            and client.get("/api/auth/me").status_code == 200,
            "[12] signing out one account leaves the other signed in",
        )

        # [13] wrong password and duplicate registration are clear, not vague
        bad = client.post(
            "/api/auth/login",
            json={"username": user_a, "password": "wrong-password"},
        )
        message = str(bad.json())
        ok(
            bad.status_code == 401 and "Invalid username or password" in message,
            "[13] wrong password -> 401 with the real message",
            message,
        )
        ok(
            "between instances" not in message,
            "[13] the demo-only hint is gone",
            message,
        )
        dup = client.post(
            "/api/auth/register",
            json={"username": user_a, "password": "Alpha@2026x"},
        )
        ok(
            dup.status_code == 409,
            "[13] duplicate registration -> 409",
            dup.status_code,
        )
    finally:
        client.close()


def _cleanup(names) -> None:
    names = [n for n in names if n]
    if not names:
        return
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            f"DELETE FROM users WHERE username IN ({','.join('?' * len(names))})",
            list(names),
        )
        conn.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")
    from modules import snapshot

    for name in names:
        snapshot.delete_account(name)


def main() -> int:
    print("=== Account lifecycle regression ===")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AccountStore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store_url = f"http://127.0.0.1:{server.server_address[1]}/accounts"
    saved_env = {
        name: os.environ.get(name)
        for name in ("ONTI_SNAPSHOT_URL", "ONTI_SNAPSHOT_TOKEN")
    }
    try:
        _service_checks(store_url)
        _http_checks()
    finally:
        # Remove the durable records while the stub is still reachable, then
        # shut it down and put the environment back the way we found it.
        _cleanup(CREATED)
        server.shutdown()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    print()
    print(f"Accounts: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
