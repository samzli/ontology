"""T6 verification: report factory — MD / self-contained HTML / ReportLab PDF,
dynamic sections, ownership and cleanup."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import admin_service as adm
from modules import auth
from modules import report_builder as rb
from modules.db import get_conn

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  BAD {name} :: {detail}")


with get_conn() as conn:
    # Idempotency: clean leftovers from interrupted earlier runs.
    import shutil

    for r in conn.execute(
        "SELECT uid FROM reports WHERE title IN "
        "('VNB Ontology Analytics — Test Report','Empty Scope Report','Alice Private Report')"
    ).fetchall():
        shutil.rmtree(os.path.join(rb.REPORTS_ROOT, r["uid"]), ignore_errors=True)
        conn.execute("DELETE FROM reports WHERE uid=?", (r["uid"],))
    for uname in ("t6_alice", "t6_bob"):
        if not conn.execute(
            "SELECT 1 FROM users WHERE username=?", (uname,)
        ).fetchone():
            auth.create_user(
                conn, uname, "Password123!", display_name=uname, role="user"
            )
    if not conn.execute("SELECT 1 FROM users WHERE username='rootadmin'").fetchone():
        auth.create_user(
            conn,
            "rootadmin",
            "AdminPass123!",
            display_name="Administrator",
            role="admin",
        )
    else:
        # Fixture must be an admin regardless of what an earlier run left behind.
        conn.execute("UPDATE users SET role='admin' WHERE username='rootadmin'")
    conn.commit()

    pack_id = conn.execute(
        "SELECT id FROM ontology_packs WHERE slug='vnb-ape-sample'"
    ).fetchone()["id"]

    print("[TR-6.1] rich session (>=3 Q&A with chains + answers) -> three files")
    qa = [
        {
            "question": "What is the total VNB?",
            "answer": (
                "The stated **Total VNB is $11.08M** on APE of **$68.00M**.\n\n"
                "| Metric | Value |\n| --- | --- |\n| APE | $68.00M |\n| VNB | $11.08M |"
            ),
            "mode": "rules",
            "latency_ms": 12.4,
            "graph": {
                "nodes": ["TotalVNB", "TotalAPE", "BlendedVNBMargin"],
                "edge_ids": ["E3"],
            },
        },
        {
            "question": "How many channels are there and what do they contribute?",
            "answer": (
                "Three distribution channels:\n\n- **Agency** — APE $20.00M, VNB $3.62M\n"
                "- **Bancassurance** — APE $30.00M, VNB $6.55M\n"
                "- **Brokerage** — APE $18.00M, VNB $3.10M"
            ),
            "mode": "rules",
            "latency_ms": 18.9,
            "graph": {
                "nodes": ["ChannelAPE", "TotalAPE", "AgentCount", "AgentProductivity"],
                "edge_ids": ["E1", "E4", "E5"],
            },
        },
        {
            "question": "Why does the blended margin differ across channels?",
            "answer": "Blended margin is driven by **product mix weights** and per-product VNB "
            "margins; Banca leans on Participating products (21.85%).",
            "mode": "rules",
            "latency_ms": 9.1,
            "graph": {
                "nodes": [
                    "ProductMixWeight",
                    "ProductVNBMargin",
                    "ChannelBlendedMargin",
                    "BlendedVNBMargin",
                ],
                "edge_ids": ["E7", "E8", "E2"],
            },
        },
    ]
    rich = rb.build_report(
        conn,
        owner_id=None,
        title="VNB Ontology Analytics — Test Report",
        scope="session",
        sections=["summary", "qa", "evidence", "kpi", "charts"],
        include_ai=False,
        qa_items=qa,
        actor="rootadmin",
    )
    rdir = rich["dir"]
    for fn in ("report.md", "report.html", "report.pdf"):
        p = os.path.join(rdir, fn)
        check(
            f"{fn} exists and non-empty",
            os.path.exists(p) and os.path.getsize(p) > 2000,
            f"{p} size={os.path.getsize(p) if os.path.exists(p) else 0}",
        )

    md = open(os.path.join(rdir, "report.md"), encoding="utf-8").read()
    for sec in (
        "Executive Summary",
        "Q&A Record",
        "Ontology Evidence",
        "KPI and Numbers",
        "Chart Analysis",
        "Appendix",
    ):
        check(f"MD contains section: {sec}", sec in md)
    check(
        "MD cites session values",
        "$11.08M" in md and "$30.00M" in md and "Agency" in md,
    )
    check("MD chain image referenced", "charts/impact_chain.png" in md)
    chart_pngs = [
        f for f in os.listdir(os.path.join(rdir, "charts")) if f.endswith(".png")
    ]
    check("chain + data chart PNGs rendered", len(chart_pngs) >= 2, str(chart_pngs))
    check("impact chain PNG present", "impact_chain.png" in chart_pngs)

    page = open(os.path.join(rdir, "report.html"), encoding="utf-8").read()
    check(
        "HTML is self-contained (no local img refs)",
        'src="charts/' not in page and "src='charts/" not in page,
    )
    check("HTML embeds images as data URIs", "data:image/png;base64" in page)
    check(
        "HTML has no external resources",
        not re.search(r'(src|href)\s*=\s*["\']https?://', page),
    )
    check(
        "HTML renders answer table",
        "<table>" in page and "$11.08M" in page and "Bancassurance" in page,
    )

    pdf = open(os.path.join(rdir, "report.pdf"), "rb").read()
    check("PDF header", pdf.startswith(b"%PDF"))
    n_pages = len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf))
    check("PDF has >1 page", n_pages > 1, f"pages={n_pages}")
    check(
        "PDF embeds raster image XObjects",
        b"/Image" in pdf
        and (b"/XObject" in pdf or b"Subtype /Image" in pdf or b"Subtype/Image" in pdf),
    )
    check("PDF EOF marker", b"%%EOF" in pdf[-32:])
    check(
        "report registered in DB",
        conn.execute("SELECT 1 FROM reports WHERE uid=?", (rich["uid"],)).fetchone()
        is not None,
    )

    print("[TR-6.2] empty scope -> all three files, empty sections omitted, no crash")
    stale = conn.execute(
        "SELECT id FROM ontology_packs WHERE slug='t6-empty-lab'"
    ).fetchone()
    if stale:
        conn.execute("DELETE FROM ontology_packs WHERE id=?", (stale["id"],))
    conn.commit()
    empty_pid = adm.create_pack(
        conn,
        "rootadmin",
        slug="t6-empty-lab",
        name="T6 Empty Lab",
        description="no data",
    )
    conn.commit()
    empty = rb.build_report(
        conn,
        owner_id=None,
        title="Empty Scope Report",
        scope="pack",
        sections=["summary", "qa", "evidence", "kpi", "charts"],
        include_ai=False,
        qa_items=[],
        pack_id=empty_pid,
        actor="rootadmin",
    )
    for fn in ("report.md", "report.html", "report.pdf"):
        path = os.path.join(empty["dir"], fn)
        check(
            f"empty {fn} exists",
            os.path.exists(path) and os.path.getsize(path) > 0,
            str(os.path.getsize(path)) if os.path.exists(path) else "missing",
        )
    empty_md = open(os.path.join(empty["dir"], "report.md"), encoding="utf-8").read()
    check(
        "empty report omits Q&A/charts/chain",
        "Q&A Record" not in empty_md
        and "Chart Analysis" not in empty_md
        and "Impact Chain" not in empty_md,
    )
    check("empty report keeps appendix", "Appendix" in empty_md)

    print(
        "[TR-6.4] ownership: other user 403, owner/admin allowed, delete cleans files"
    )
    for uname in ("t6_alice", "t6_bob"):
        if not conn.execute(
            "SELECT 1 FROM users WHERE username=?", (uname,)
        ).fetchone():
            auth.create_user(
                conn, uname, "Password123!", display_name=uname, role="user"
            )
    conn.commit()
    alice = dict(
        conn.execute("SELECT * FROM users WHERE username='t6_alice'").fetchone()
    )
    bob = dict(conn.execute("SELECT * FROM users WHERE username='t6_bob'").fetchone())
    alice_report = rb.build_report(
        conn,
        owner_id=alice["id"],
        title="Alice Private Report",
        scope="session",
        qa_items=qa[:1],
        include_ai=False,
        actor="t6_alice",
    )
    try:
        rb.get_record(conn, alice_report["uid"], bob)
        check("bob cannot open alice report (403)", False)
    except rb.ReportError as e:
        check("bob cannot open alice report (403)", e.status_code == 403)
    rec_alice = rb.get_record(conn, alice_report["uid"], alice)
    check("owner can open", rec_alice["owner"] == "t6_alice")
    admin = dict(
        conn.execute("SELECT * FROM users WHERE username='rootadmin'").fetchone()
    )
    rec_admin = rb.get_record(conn, alice_report["uid"], admin)
    check("admin can open", rec_admin["uid"] == alice_report["uid"])
    listing_bob = rb.list_reports(conn, bob)
    check(
        "bob sees only his own reports",
        all(r["owner_id"] == bob["id"] for r in listing_bob),
    )
    listing_admin = rb.list_reports(conn, admin)
    check("admin sees all reports", len(listing_admin) > len(listing_bob))
    rb.delete_report(conn, alice_report["uid"], admin, "rootadmin")
    check(
        "delete removes DB row",
        conn.execute(
            "SELECT 1 FROM reports WHERE uid=?", (alice_report["uid"],)
        ).fetchone()
        is None,
    )
    check("delete removes files", not os.path.exists(alice_report["dir"]))

    # cleanup: rich/empty reports + empty pack (admin perms needed)
    rb.delete_report(conn, rich["uid"], admin, "rootadmin")
    rb.delete_report(conn, empty["uid"], admin, "rootadmin")
    adm.delete_pack(conn, "rootadmin", empty_pid)
    conn.commit()
    audited = conn.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action LIKE 'report.%'"
    ).fetchone()["n"]
    check("report generate/delete audited", audited >= 4, str(audited))

print()
print(f"T6 verification: {'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
