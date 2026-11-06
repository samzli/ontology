"""End-to-end check for the shared team (demo) account.

Proves the access model documented in ``docs/ADMIN_GUIDE.md`` §4.7: a
**regular** account can sign in, use the whole user portal and build
ontologies in Graph studio, while every governance route stays closed.

Usage (from the project root, with the server already running)::

    python main.py                     # in one terminal
    python tests/verify_giteam.py      # user: giteam / GiDemo@2026

The account ships in the committed ``data/onti.db`` so that Docker and
serverless deployments start with it (see README → Deploying to Vercel).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _auth_client as auth_client  # noqa: E402

BASE = "http://127.0.0.1:8010"
USERNAME = "giteam"
PASSWORD = "GiDemo@2026"

PASS = 0
FAIL = 0


def ok(cond: bool, label: str, detail: object = "") -> None:
    """Record one assertion and print it.

    Args:
        cond (bool): Result of the check.
        label (str): Human-readable description.
        detail (object, optional): Extra context shown only when it fails.
    """
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} :: {detail}")


def main() -> int:
    """Run the shared-account checks.

    Returns:
        int: ``0`` when every check passed, ``1`` otherwise.
    """
    with httpx.Client(base_url=BASE, timeout=60) as c:
        auth_client.login(c, USERNAME, PASSWORD)
        if c.get("/api/auth/me").status_code != 200:
            print("  FAIL giteam login :: session not established")
            print("\n  0 passed, 1 failed")
            return 1
        print("  OK  giteam login")
        me = c.get("/api/auth/me").json()
        ok((me.get("user") or {}).get("role") == "user", "account role is 'user'", me)
        ok(c.get("/app").status_code == 200, "user portal /app answers 200")

        # Governance stays out of reach.
        ok(c.get("/admin").status_code in (303, 307), "GET /admin redirects away")
        ok(c.get("/api/admin/stats").status_code == 403, "admin API answers 403")

        # Graph studio — the ontology-building feature — is a user feature.
        r = c.get("/api/graph-studio/sources")
        ok(r.status_code == 200, "graph-studio/sources answers 200", r.status_code)
        ok(
            isinstance(r.json().get("tables"), list),
            "sources lists the pack tables",
            r.text[:120],
        )
        ok(c.get("/api/graph-studio/history").status_code == 200, "history answers 200")
        ok(
            c.post("/api/graph-studio/build", json={"view": "schema"}).status_code
            == 200,
            "build answers 200",
        )

        # Core user-portal APIs.
        for path in ("/api/config", "/api/reports", "/api/questions", "/api/packs"):
            ok(c.get(path).status_code == 200, f"{path} answers 200")

    # What an external visitor sees through a shared link.
    with httpx.Client(base_url=BASE, timeout=30) as anon:
        ok(anon.get("/health").status_code == 200, "anonymous /health answers 200")
        ok(anon.get("/login").status_code == 200, "anonymous /login answers 200")
        ok(
            anon.get("/app").status_code in (303, 307),
            "anonymous /app redirects to login",
        )
        ok(
            anon.get("/static/css/app.css").status_code == 200,
            "anonymous CSS answers 200",
        )

    print(f"\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
