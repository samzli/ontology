"""End-to-end HTTP smoke test for the Onti platform.

Run against a running server (a freshly re-seeded database is recommended
because the analytics assertions check exact seeded values):

    python -m modules.db
    python -m uvicorn main:app --host 127.0.0.1 --port 8010
    python tests/smoke_http.py

The pack-specific sections activate the built-in VNB/APE sample pack
automatically and restore the previously active pack and the admin's
scenario selection at the end; the uploaded smoke dataset is removed
again after the commit check.

Covers TR-2.2 (cookie/auth matrix) plus the main v2 user and admin flows:
SSE rules answers with graph evidence, unmatched suggestions, report
generation + 3-format downloads + ownership, data upload validation,
question bank and admin authorization.
"""

from __future__ import annotations

import io
import random
import string
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.db import get_conn, get_setting, set_setting  # noqa: E402

BASE = "http://127.0.0.1:8010"
PASS = 0
FAIL = 0


def ok(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK  " + label)
    else:
        FAIL += 1
        print("  FAIL " + label)


def suffix() -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(6))


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                import json

                events.append(json.loads(line[6:]))
    return events


def main() -> int:
    admin_pw = "AdminPass123!"
    user_pw = "UserPass123!"
    admin_name = "smoke_" + suffix()
    user_name = "user_" + suffix()
    # Default administrator is pre-provisioned on first run so the platform is
    # usable immediately; the smoke test logs in as that admin.
    default_admin_user = "admin"
    default_admin_pw = "Onti@2026"

    # ----------------------------------------------------- unauthenticated ---
    print("[1] Unauthenticated matrix")
    anon = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    r = anon.get("/login")
    ok(r.status_code == 200 and b"Onti" in r.content, "GET /login renders")
    r = anon.get("/admin/login")
    ok(
        r.status_code == 200 and b"Admin console" in r.content,
        "GET /admin/login renders dedicated admin sign-in",
    )
    r = anon.get("/app")
    ok(
        r.status_code in (302, 307) and r.headers["location"] == "/login",
        "GET /app redirects to /login",
    )
    r = anon.get("/admin")
    ok(
        r.status_code in (302, 307) and r.headers["location"] == "/admin/login",
        "GET /admin redirects to /admin/login",
    )
    r = anon.get("/api/questions")
    ok(r.status_code == 401, "GET /api/questions -> 401")
    r = anon.get("/api/admin/stats")
    ok(r.status_code == 401, "GET /api/admin/stats -> 401")

    status = anon.get("/api/auth/status").json()
    ok(status["first_run"] is False, "default admin pre-provisioned -> first_run=False")

    # --------------------------------------------------------- default admin ---
    print("[2] Log in as the pre-provisioned default administrator")
    admin = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    r = admin.post(
        "/api/auth/login",
        json={"username": default_admin_user, "password": default_admin_pw},
    )
    ok(
        r.status_code == 200 and r.json()["user"]["role"] == "admin",
        "default admin login succeeds; session cookie set "
        + str(r.cookies.get("onti_session") is not None),
    )
    me = admin.get("/api/auth/me").json()["user"]
    ok(
        me["username"] == default_admin_user and me["role"] == "admin",
        "/api/auth/me identity",
    )
    ok(admin.get("/app").status_code == 200, "admin GET /app -> 200")
    ok(b"Ask the ontology" in admin.get("/app").content, "user portal rendered")
    ok(admin.get("/admin").status_code == 200, "admin GET /admin -> 200")

    # -------------------------------------------------------- second user ----
    print("[3] Self-registered account is regular user")
    user = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    r = user.post(
        "/api/auth/register",
        json={"username": user_name, "password": user_pw, "display_name": "Smoke User"},
    )
    ok(
        r.status_code == 200 and r.json()["user"]["role"] == "user",
        "registered account role=user",
    )
    ok(user.get("/api/admin/stats").status_code == 403, "user admin API -> 403")
    r = user.get("/admin")
    ok(
        r.status_code in (302, 307) and r.headers["location"] == "/app?forbidden=1",
        "user /admin -> redirect /app?forbidden=1",
    )

    print("[4] Registration toggle")
    r = admin.put("/api/admin/settings", json={"allow_registration": False})
    ok(r.status_code == 200, "admin disables self-registration")
    r = anon.post(
        "/api/auth/register", json={"username": "x_" + suffix(), "password": user_pw}
    )
    ok(r.status_code == 403, "registration while closed -> 403")
    admin.put("/api/admin/settings", json={"allow_registration": True})

    print("[5] Logout / login")
    user.post("/api/auth/logout")
    ok(user.get("/api/auth/me").status_code == 401, "logout invalidates session")
    r = user.post("/api/auth/login", json={"username": user_name, "password": user_pw})
    ok(r.status_code == 200 and "onti_session" in r.cookies, "login issues new cookie")
    r = user.post(
        "/api/auth/login", json={"username": user_name, "password": "bad-password-xx"}
    )
    ok(r.status_code == 401, "wrong password -> 401")

    # ------------------------------------------------------------ reading ----
    print("[6] Read APIs")
    # The assertions from here to the end are written against the built-in
    # VNB/APE sample pack. Activate it (and point the admin's scenario at
    # it), remember what was active before, and restore both at the end.
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM ontology_packs WHERE is_active=1").fetchone()
        prev_active = row["id"] if row else None
        prev_user_pack = get_setting(conn, "user_pack:1", None)
    packs_all = admin.get("/api/admin/packs").json()["packs"]
    sample = next(p for p in packs_all if p["slug"] == "vnb-ape-sample")
    PID = sample["id"]
    admin.post(f"/api/admin/packs/{PID}/activate")
    with get_conn() as conn:
        set_setting(conn, "user_pack:1", str(PID))

    cfg = admin.get("/api/config").json()
    ok(
        cfg["pack"]["id"] is not None and bool(cfg["pack"]["name"]),
        "active pack exposed",
    )
    qs = admin.get("/api/questions").json()["questions"]
    ok(len(qs) == 29, f"29 suggested questions (got {len(qs)})")
    g = admin.get("/api/graph").json()["graph"]
    ok(
        len(g["nodes"]) >= 20 and len(g["links"]) >= 20,
        f"visual graph populated: {len(g['nodes'])} nodes / {len(g['links'])} links",
    )
    lib = admin.get("/api/library").json()
    ok(
        len(lib["entities"]) >= 5 and len(lib["tables"]) >= 10,
        f"library populated: {len(lib['entities'])} entities / {len(lib['tables'])} tables",
    )
    ok(any(t["row_count"] > 0 for t in lib["tables"]), "library tables carry data rows")

    # --------------------------------------------------- analytics APIs ----
    print("[6b] Analytics APIs (trend / attribution / scenario / agents)")
    ts = admin.get("/api/analytics/timeseries").json()
    ok(len(ts["company"]) == 6, f"6 monthly company points (got {len(ts['company'])})")
    last = ts["company"][-1]
    ok(
        abs(last["total_vnb"] - 1.448) < 0.01 and last["month"] == "2025-06",
        "latest month is Jun 2025 total VNB 1.448",
    )
    attr = admin.get("/api/analytics/attribution").json()["attribution"]
    fac = attr["factors"]
    ok(
        attr["month"] == "2025-06"
        and abs(
            fac["agent_effect"]
            + fac["productivity_effect"]
            + fac["mix_effect"]
            - fac["delta_vnb"]
        )
        < 1e-6,
        "MoM attribution 3 factors sum exactly to delta VNB (no residual)",
    )
    scen = admin.get("/api/analytics/scenario?multiplier=2").json()["scenario"]
    ok(
        abs(scen["target_total_vnb"] - 2.896017) < 1e-3
        and abs(scen["required_agency_ape"] - 11.573657) < 1e-3,
        "2x scenario back-solves target 2.896 / required Agency APE 11.574",
    )
    scen420 = admin.get(
        "/api/analytics/scenario?multiplier=2&planned_active=420"
    ).json()["scenario"]
    ok(
        abs(scen420["lever_productivity"]["required_prod"] - 27.556325) < 1e-2,
        "planned 420 agents -> required productivity 27.56k (matches workbook)",
    )
    ag = admin.get("/api/analytics/agent-stats").json()["agents"]
    ok(
        ag["total_active"] == 390 and ag["total_registered"] == 500,
        "agent stats: 390/500 active in Jun 2025",
    )
    ok(ag["churn_risk"].get("High") == 49, "49 high-churn-risk active agents")

    # ------------------------------------------------------------ SSE --------
    print("[7] SSE rules answer with impact-chain evidence")
    r = admin.post(
        "/api/ask-stream", json={"question": "What is the total VNB?", "mode": "auto"}
    )
    evs = parse_sse(r.text)
    kinds = [e["type"] for e in evs]
    ok(kinds == ["meta", "token", "done"], "rules stream event order meta/token/done")
    done = next(e for e in evs if e["type"] == "done")
    token = next(e for e in evs if e["type"] == "token")["content"]
    ok(done["source"] == "rules" and done["matched"] is True, "matched by rules engine")
    ok("1.45" in token, "answer states VNB 1.45 (Jun 2025)")
    ok(
        len(done["graph"]["nodes"]) >= 3 and len(done["graph"]["edge_ids"]) >= 1,
        "done carries non-trivial graph evidence",
    )

    print("[8] Rules-only unmatched question gives suggestions")
    r = admin.post(
        "/api/ask-stream",
        json={"question": "zxqw frotz lorem ipsum banana", "mode": "rules"},
    )
    evs = parse_sse(r.text)
    meta = evs[0]
    ok(
        meta["matched"] is False and len(meta["suggestions"]) == 3,
        "unmatched returns exactly 3 clickable suggestions",
    )

    # --------------------------------------------------------- reports ------
    print("[9] Report generation / downloads / ownership")
    qa = [
        {
            "question": "What is the total VNB?",
            "answer": token[:800],
            "source": "rules",
            "matched": True,
            "latency_ms": 0,
            "graph": done["graph"],
        }
    ]
    r = admin.post(
        "/api/reports",
        json={
            "title": "Smoke analytics report",
            "scope": "session",
            "sections": ["summary", "qa", "evidence", "kpi", "charts"],
            "include_ai": False,
            "qa_items": qa,
        },
    )
    ok(r.status_code == 200, "report generated")
    uid = r.json()["report"]["uid"]
    md = admin.get(f"/api/reports/{uid}/download/md")
    html = admin.get(f"/api/reports/{uid}/download/html")
    pdf = admin.get(f"/api/reports/{uid}/download/pdf")
    ok(md.status_code == 200 and md.content.startswith(b"#"), "Markdown download")
    ok(html.status_code == 200 and b"<html" in html.content.lower(), "HTML download")
    ok(
        pdf.status_code == 200
        and pdf.content[:5] == b"%PDF-"
        and pdf.content.rstrip()[-5:] == b"%%EOF",
        "PDF download is a valid PDF",
    )
    ok(
        b"data:image/png;base64" in html.content,
        "HTML has embedded charts (no external assets)",
    )
    ok(
        b"Impact" in html.content or b"chain" in html.content.lower(),
        "HTML includes chain section",
    )
    r = user.get(f"/api/reports/{uid}/download/md")
    ok(r.status_code == 403, "cross-user report access -> 403")
    reports = user.get("/api/reports").json()["reports"]
    ok(all(x["uid"] != uid for x in reports), "owner-scoped report list")
    allrep = admin.get("/api/admin/reports").json()["reports"]
    ok(any(x["uid"] == uid for x in allrep), "admin report centre sees all reports")
    ok(user.delete(f"/api/reports/{uid}").status_code == 403, "non-owner cannot delete")
    ok(
        admin.delete(f"/api/reports/{uid}").status_code == 200,
        "owner/admin deletes report",
    )

    # ------------------------------------------------------------ upload ----
    print("[10] Data upload validation + commit")
    tables = admin.get(f"/api/admin/data/tables?pack_id={PID}").json()["tables"]
    ch = next(t for t in tables if t["key"] == "channels")
    csv_text = (
        "id,name,count,churn,prod\n"
        "DD,Digital Direct,12,0.08,40\n"
        "BOG,Bogus Row,notanumber,,\n"
    )
    files = {"file": ("smoke_channels.csv", csv_text.encode(), "text/csv")}
    r = admin.post(
        "/api/admin/data/upload",
        data={"pack_id": str(PID), "table_id": str(ch["id"]), "commit": "false"},
        files=files,
    )
    ok(
        r.status_code == 200
        and r.json()["valid_count"] == 1
        and r.json()["error_count"] == 1,
        "preview reports 1 valid / 1 invalid row",
    )
    csv_ok = "id,name,count,churn,prod\n" "DD,Digital Direct,12,0.08,40\n"
    files = {"file": ("smoke_channels.csv", csv_ok.encode(), "text/csv")}
    r = admin.post(
        "/api/admin/data/upload",
        data={
            "pack_id": str(PID),
            "table_id": str(ch["id"]),
            "dataset_name": "Smoke dataset",
            "commit": "true",
        },
        files=files,
    )
    ok(
        r.status_code == 200 and r.json()["rows"] == 1,
        "commit imports 1 row and activates dataset",
    )
    dsid = r.json().get("dataset_id")
    if dsid:
        ok(
            admin.delete(f"/api/admin/data/datasets/{dsid}").status_code == 200,
            "smoke dataset removed (state restored)",
        )
    bad = {"file": ("evil.txt", b"hello", "text/plain")}
    r = admin.post(
        "/api/admin/data/upload",
        data={"pack_id": str(PID), "table_id": str(ch["id"]), "commit": "false"},
        files=bad,
    )
    ok(r.status_code in (400, 422), f"disallowed extension rejected ({r.status_code})")
    r = user.post(
        "/api/admin/data/upload",
        data={"pack_id": str(PID), "table_id": str(ch["id"]), "commit": "false"},
        files={"file": ("x.csv", csv_ok.encode(), "text/csv")},
    )
    ok(r.status_code == 403, "upload by regular user -> 403")

    # ----------------------------------------------------------- questions --
    print("[11] Question bank admin")
    qtext = "Smoke question " + suffix()
    r = admin.post(
        "/api/admin/questions",
        json={"pack_id": PID, "question": qtext, "category": "test", "source": "user"},
    )
    ok(r.status_code == 200, "question added")
    qlist = admin.get(
        f"/api/admin/questions?pack_id={PID}&search=Smoke+question"
    ).json()["questions"]
    qid = next(q["id"] for q in qlist if q["question"] == qtext)
    ok(
        admin.delete(f"/api/admin/questions/{qid}").status_code == 200,
        "question deleted",
    )

    print("[12] Model centre + audit")
    models = admin.get("/api/admin/models").json()["models"]
    ok(len(models) >= 1, "model list available to admin")
    audit_events = admin.get("/api/admin/audit?limit=50").json()["events"]
    kinds_seen = " ".join(e["action"] for e in audit_events)
    ok(
        "auth.login_success" in kinds_seen,
        "audit trail records login events",
    )

    # ------------------------------------------------------- AI proposals ---
    print("[13] AI ontology proposals")
    s = admin.get("/api/admin/settings").json()
    ok("ai_auto_propose" in s, "settings expose ai_auto_propose switch")
    r = admin.put("/api/admin/settings", json={"ai_auto_propose": False})
    ok(
        r.status_code == 200
        and admin.get("/api/admin/settings").json()["ai_auto_propose"] is False,
        "ai_auto_propose can be switched off",
    )
    admin.put("/api/admin/settings", json={"ai_auto_propose": True})
    d = admin.get(f"/api/admin/proposals?pack_id={PID}&status=pending").json()
    ok(
        isinstance(d.get("proposals"), list) and isinstance(d.get("pending"), int),
        "proposals list endpoint returns pending queue",
    )
    ok(
        admin.post("/api/admin/proposals/999999/approve", json={}).status_code == 404,
        "approving a missing proposal -> 404",
    )
    ok(
        admin.post("/api/admin/proposals/999999/reject", json={}).status_code == 404,
        "rejecting a missing proposal -> 404",
    )
    r = admin.post(f"/api/admin/packs/{PID}/proposals/approve-all", json={})
    ok(
        r.status_code == 200
        and isinstance(r.json()["approved"], list)
        and isinstance(r.json()["rejected"], list),
        "approve-all is a safe batch no-op when queue is empty",
    )

    # ------------------------------------------------------ restore state ---
    with get_conn() as conn:
        if prev_user_pack is None:
            conn.execute("DELETE FROM app_settings WHERE key='user_pack:1'")
        else:
            set_setting(conn, "user_pack:1", prev_user_pack)
    if prev_active is not None and prev_active != PID:
        admin.post(f"/api/admin/packs/{prev_active}/activate")

    print(
        f"\nHTTP smoke: {'PASS' if FAIL == 0 else 'FAIL'}  ({PASS} ok, {FAIL} failed)"
    )
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
