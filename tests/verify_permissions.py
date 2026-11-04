"""End-to-end check: registration everywhere + ontology permissions.

Proves the guarantees from the deployment-readiness pass:

1. **Account creation works on every host** — ``/api/auth/status`` reports
   ``allow_registration: true`` regardless of storage mode, and a brand-new
   account registers and signs in (no "cannot create account" wall, even on
   ephemeral serverless preview deployments).
2. **Regular users and administrators can both build ontologies** in Graph
   studio — ``POST /api/graph-studio/create-ontology`` only requires a
   signed-in user, regardless of role.
3. **Administrators see every user-created ontology** — a pack created by a
   regular account appears in the admin console listing (and in the shared
   scenario picker) with the creator recorded in ``origin``.

Governance boundaries are re-checked as well: the new regular account gets
403 from admin APIs, and anonymous visitors are redirected to sign in.

Usage (from the project root, with the server already running)::

    python main.py                         # in one terminal
    python tests/verify_permissions.py     # in another
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8010"
ADMIN = {"username": "admin", "password": "Onti@2026"}
STAMP = int(time.time())
USERNAME = f"perm_{STAMP}"
PASSWORD = "PermPass@2026"
PACK_USER = f"Perm User Ontology {STAMP}"
PACK_ADMIN = f"Perm Admin Ontology {STAMP}"

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


def create_ontology(client: httpx.Client, table_ids: list, name: str) -> dict:
    """Create+activate one ontology via Graph studio and return the body.

    Args:
        client (httpx.Client): Signed-in HTTP client.
        table_ids (list): Source tables to infer from.
        name (str): Unique pack name.

    Returns:
        dict: Parsed response body (``{}`` when the request failed).
    """
    r = client.post(
        "/api/graph-studio/create-ontology",
        json={"table_ids": table_ids, "name": name, "ai_enrich": False},
    )
    ok(
        r.status_code == 200,
        f"create-ontology '{name}' answers 200",
        f"{r.status_code} {r.text[:200]}",
    )
    return r.json() if r.status_code == 200 else {}


def main() -> int:
    """Run the registration / permission / visibility checks.

    Returns:
        int: ``0`` when every check passed, ``1`` otherwise.
    """
    # create-ontology activates the new pack globally, which would leak into
    # later scripts in a full `run_all.py` pass — remember the current one
    # and restore it at the end.
    original_active = None
    with httpx.Client(base_url=BASE, timeout=30) as pre:
        if pre.post("/api/auth/login", json=ADMIN).status_code == 200:
            original_active = pre.get("/api/admin/packs").json().get("active_id")

    # -- 1. Registration is open and works --------------------------------
    with httpx.Client(base_url=BASE, timeout=30) as anon:
        r = anon.get("/api/auth/status")
        body = r.json() if r.status_code == 200 else {}
        ok(
            r.status_code == 200 and body.get("allow_registration") is True,
            "auth/status: allow_registration is true on this host",
            f"{r.status_code} {r.text[:160]}",
        )
        ok(
            anon.get("/app").status_code in (303, 307),
            "anonymous /app redirects to login",
        )
        ok(
            anon.get("/api/packs").status_code in (401, 403),
            "anonymous /api/packs is rejected",
        )
        r = anon.post(
            "/api/auth/register",
            json={"username": USERNAME, "password": "short"},
        )
        ok(r.status_code == 422, "short password rejected with 422", r.status_code)

    with httpx.Client(base_url=BASE, timeout=120) as user:
        r = user.post(
            "/api/auth/register",
            json={
                "username": USERNAME,
                "password": PASSWORD,
                "display_name": "Permission Check",
            },
        )
        body = r.json() if r.status_code == 200 else {}
        ok(
            r.status_code == 200 and (body.get("user") or {}).get("role") == "user",
            "new account registers with role 'user'",
            f"{r.status_code} {r.text[:200]}",
        )
        if r.status_code != 200:
            print(f"\n  {PASS} passed, {FAIL} failed")
            return 1
        me = user.get("/api/auth/me").json()
        ok(
            (me.get("user") or {}).get("username") == USERNAME,
            "session is live right after registration",
            me,
        )

        # -- 2. Governance boundaries for the fresh regular account --------
        ok(
            user.get("/api/admin/stats").status_code == 403,
            "regular user gets 403 from /api/admin/stats",
        )
        ok(
            user.get("/api/admin/packs").status_code == 403,
            "regular user gets 403 from /api/admin/packs",
        )
        ok(
            user.get("/admin").status_code in (303, 307),
            "regular user is redirected away from /admin",
        )

        # -- 3. Regular user builds an ontology in Graph studio ------------
        r = user.get("/api/graph-studio/sources")
        tables = r.json().get("tables", []) if r.status_code == 200 else []
        ok(
            r.status_code == 200 and len(tables) > 0,
            "graph-studio/sources lists pack tables for a regular user",
            f"{r.status_code} {r.text[:160]}",
        )
        ids = [t["id"] for t in tables[:2]]
        created = create_ontology(user, ids, PACK_USER) if ids else {}
        user_pid = created.get("pack_id")
        ok(isinstance(user_pid, int), "user ontology got a pack_id", created)

        packs = user.get("/api/packs").json().get("packs", [])
        mine = [p for p in packs if p.get("id") == user_pid]
        ok(bool(mine), "creator sees the new ontology in /api/packs")
        ok(
            bool(mine) and USERNAME in (mine[0].get("origin") or ""),
            "origin records the creating username",
            mine[:1],
        )

    # -- duplicate username rejected (fresh client, after user exists) -----
    with httpx.Client(base_url=BASE, timeout=30) as dup:
        r = dup.post(
            "/api/auth/register",
            json={"username": USERNAME, "password": PASSWORD},
        )
        ok(r.status_code == 409, "duplicate username rejected with 409", r.status_code)

    # -- 4. Administrator sees the user's ontology and can create too -----
    with httpx.Client(base_url=BASE, timeout=120) as admin:
        r = admin.post("/api/auth/login", json=ADMIN)
        body = r.json() if r.status_code == 200 else {}
        ok(
            r.status_code == 200 and (body.get("user") or {}).get("role") == "admin",
            "admin login",
            f"{r.status_code} {r.text[:160]}",
        )
        admin_packs = admin.get("/api/admin/packs").json().get("packs", [])
        seen = [p for p in admin_packs if p.get("id") == user_pid]
        ok(
            bool(seen),
            "admin console lists the user-created ontology",
            f"pack_id={user_pid} not in {len(admin_packs)} packs",
        )
        ok(
            bool(seen) and USERNAME in str(seen[0].get("origin") or seen[0]),
            "admin listing shows who created it",
            seen[:1],
        )
        picker = admin.get("/api/packs").json().get("packs", [])
        ok(
            any(p.get("id") == user_pid for p in picker),
            "admin also sees it in the shared scenario picker",
        )

        r = admin.get("/api/graph-studio/sources")
        tables = r.json().get("tables", []) if r.status_code == 200 else []
        ids = [t["id"] for t in tables[:1]]
        created = create_ontology(admin, ids, PACK_ADMIN) if ids else {}
        admin_pid = created.get("pack_id")
        ok(
            isinstance(admin_pid, int),
            "admin can build an ontology in Graph studio too",
            created,
        )

    # -- 5. Cross-visibility: the regular user sees the admin's pack ------
    with httpx.Client(base_url=BASE, timeout=30) as user:
        user.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        packs = user.get("/api/packs").json().get("packs", [])
        ok(
            any(p.get("id") == admin_pid for p in packs),
            "regular user sees the admin-created ontology in the picker",
        )

    # -- 6. Restore the previously active pack for later test scripts -----
    if original_active is not None:
        with httpx.Client(base_url=BASE, timeout=30) as post:
            post.post("/api/auth/login", json=ADMIN)
            r = post.post(f"/api/admin/packs/{original_active}/activate")
            ok(
                r.status_code == 200,
                "previously active pack restored for later test scripts",
                r.status_code,
            )

    print(f"\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
