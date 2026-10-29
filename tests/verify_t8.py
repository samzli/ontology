"""T8 verification: batch data import — many files / every sheet / a folder.

Run against a running server; the fixture pack is created on demand by
``tests/_seed_synthetic.py`` (and removed again if the helper created it):

    python main.py                 (or: uvicorn main:app --port 8010)
    python tests/verify_t8.py

Covers the batch-import path end to end:
  1. one workbook expands into one item per sheet, sheets kept apart
  2. a name-matched sheet routes to its existing table; an unmatched sheet is
     flagged as a table to be created
  3. committing a new-table item creates the table plus a matching ontology
     entity / attributes, syncs the graph and activates the dataset
  4. several CSVs in one request all land as their own tables
  5. a data-shaped JSON (array of records) imports
  6. an ontology-shaped JSON is rejected with a pointer to 'Import pack'
  7. an unusable item aborts the whole batch — nothing is written
  8. every table / entity / dataset the test made is cleaned up
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import admin_service as adm  # noqa: E402
from modules.db import get_conn  # noqa: E402
from _seed_synthetic import delete_pack, ensure_synthetic_pack  # noqa: E402

BASE = "http://127.0.0.1:8010"
XLSX = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "Lapse_mockdata_v3 - Scenario Demo.xlsx"
)
PACK_SLUG = "lapse_experience_synthetic_demo"
NEW_SHEET = "New_Market_Data"
JSON_NAME = "term_rates"
TEST_KEYS = [
    "new_market_data",
    "regions_data",
    "distribution_share",
    JSON_NAME,
    "safe_new",
]
XLSX_BASENAME = XLSX.name
PASS = 0
FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK  " + label)
    else:
        FAIL += 1
        print("  FAIL " + label + (("  :: " + str(detail)) if detail else ""))


def js(resp):
    try:
        return resp.json()
    except Exception:
        return {}


def workbook_with_new_sheet():
    """The seeded demo workbook plus one extra sheet that matches no table."""
    import openpyxl

    wb = openpyxl.load_workbook(XLSX)
    ws = wb.create_sheet(NEW_SHEET)
    ws.append(["market", "region", "volume"])
    ws.append(["HK", "APAC", 100])
    ws.append(["SG", "APAC", 250])
    ws.append(["MY", "APAC", 75])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), list(wb.sheetnames)


def header_only_workbook():
    """A workbook whose single sheet has a header but no data rows."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Only_Header"
    ws.append(["market", "volume"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def cleanup(conn, pack_id, keys):
    for key in keys:
        t = conn.execute(
            "SELECT id FROM pack_tables WHERE pack_id=? AND key=?", (pack_id, key)
        ).fetchone()
        if t:
            for d in conn.execute(
                "SELECT id FROM datasets WHERE table_id=?", (t["id"],)
            ).fetchall():
                conn.execute("DELETE FROM pack_rows WHERE dataset_id=?", (d["id"],))
                conn.execute("DELETE FROM datasets WHERE id=?", (d["id"],))
            conn.execute("DELETE FROM pack_columns WHERE table_id=?", (t["id"],))
            conn.execute("DELETE FROM pack_tables WHERE id=?", (t["id"],))
            conn.execute(
                "DELETE FROM ontology_proposals WHERE pack_id=? AND table_id=?",
                (pack_id, t["id"]),
            )
        conn.execute("DELETE FROM entities WHERE pack_id=? AND key=?", (pack_id, key))
        conn.execute(
            "DELETE FROM attributes WHERE pack_id=? AND entity_key=?", (pack_id, key)
        )
        conn.execute(
            "DELETE FROM graph_nodes WHERE pack_id=? AND key=?", (pack_id, key)
        )
        conn.execute(
            "DELETE FROM graph_edges WHERE pack_id=? AND (source_key=? OR target_key=?)",
            (pack_id, key, key),
        )


def main():
    if not XLSX.exists():
        print(f"missing fixture: {XLSX}")
        raise SystemExit(1)

    wb_bytes, sheets = workbook_with_new_sheet()
    empty_bytes = header_only_workbook()
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    pid, created = ensure_synthetic_pack()
    print(
        f"  fixture pack {pid} ({'created by helper' if created else 'already seeded'})"
    )

    with httpx.Client(base_url=BASE, timeout=90) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "Onti@2026"}
        )
        ok(r.status_code == 200, "admin login", r.status_code)

        def batch(parts, plan=None, commit=False):
            data = {"pack_id": str(pid), "commit": "true" if commit else "false"}
            if plan is not None:
                data["plan"] = json.dumps(plan)
            files = [("files", p) for p in parts]
            return c.post("/api/admin/data/batch-upload", data=data, files=files)

        # Use a neutral basename for the in-memory workbook so the route
        # fallback on filename stem never accidentally matches an existing
        # table token (e.g. real xlsx is named "Lapse_...", whose stem would
        # overlap with the ``lapse_assumptions`` table tokens).
        demo = ("batch_upload_test.xlsx", wb_bytes, xlsx_mime)

        # ------------------------------------------- 1. one item per sheet
        resp = batch([demo])
        r1 = js(resp)
        items = r1.get("items", [])
        ok(resp.status_code == 200, "batch preview -> 200", resp.status_code)
        got = [i["item"] for i in items]
        ok(got == sheets, "workbook expands into one item per sheet", got)
        ok(len(got) == len(sheets) and len(got) >= 5, "sheet count matches", len(got))

        # ------------------------------------------- 2. routing
        months = next(i for i in items if i["item"] == "Monthly_Experience")
        ok(
            months["target"]["key"] == "experience" and not months["target"]["is_new"],
            "name-matched sheet routes to its table",
            months["target"],
        )
        news = next(i for i in items if i["item"] == NEW_SHEET)
        ok(
            news["target"]["is_new"] and news["target"]["key"] == "new_market_data",
            "unmatched sheet flagged as a new table",
            news["target"],
        )
        ok(months["valid_count"] > 0, "matched sheet validated", months["valid_count"])
        ok(news["valid_count"] == 3, "new sheet rows validated", news["valid_count"])

        # ------------------------------------------- 3. commit the new table
        plan = [
            {"file": "batch_upload_test.xlsx", "item": s, "skip": True}
            for s in sheets
            if s != NEW_SHEET
        ]
        r3 = js(batch([demo], plan=plan, commit=True))
        created = [t["key"] for t in r3.get("created_tables", [])]
        ok("new_market_data" in created, "new table created on commit", created)
        ok(r3.get("rows_total") == 3, "new-table rows imported", r3.get("rows_total"))
        with get_conn() as conn:
            t = conn.execute(
                "SELECT * FROM pack_tables WHERE pack_id=? AND key=?",
                (pid, "new_market_data"),
            ).fetchone()
            ok(t is not None, "table row persisted")
            if t:
                ok(t["active_dataset_id"] is not None, "dataset activated on new table")
                cols = [
                    row["key"]
                    for row in conn.execute(
                        "SELECT key FROM pack_columns WHERE table_id=?", (t["id"],)
                    )
                ]
                ok(
                    set(cols) == {"market", "region", "volume"},
                    "inferred columns created",
                    cols,
                )
            ent = conn.execute(
                "SELECT id FROM entities WHERE pack_id=? AND key=?",
                (pid, "new_market_data"),
            ).fetchone()
            ok(ent is not None, "matching ontology entity created")
            n_at = conn.execute(
                "SELECT COUNT(*) n FROM attributes WHERE pack_id=? AND entity_key=?",
                (pid, "new_market_data"),
            ).fetchone()["n"]
            ok(n_at == 3, "attributes created for the entity", n_at)
            node = conn.execute(
                "SELECT 1 FROM graph_nodes WHERE pack_id=? AND key=?",
                (pid, "new_market_data"),
            ).fetchone()
            ok(node is not None, "impact graph synced")

        # ------------------------------------------- 4. several CSVs at once
        # Use filenames whose tokens do NOT overlap with the throwaway
        # pack's tables (experience / lapse_assumptions / product_master /
        # channel_master). Otherwise find_table_by_name's token-overlap
        # fallback routes the CSV into an existing table instead of creating
        # a new one.
        csv_a = ("regions_data.csv", b"region,volume\nAPAC,120\nEMEA,80\n", "text/csv")
        csv_b = (
            "distribution_share.csv",
            b"channel,share\nAgency,0.6\nBroker,0.4\n",
            "text/csv",
        )
        r4 = js(batch([csv_a, csv_b], commit=True))
        keys4 = [t["key"] for t in r4.get("created_tables", [])]
        ok(
            r4.get("rows_total") == 4
            and "regions_data" in keys4
            and "distribution_share" in keys4,
            "multiple CSVs imported as their own tables",
            r4.get("created_tables"),
        )

        # ------------------------------------------- 5. data-shaped JSON
        recs = json.dumps(
            [
                {"product": "Term", "rate": 0.02},
                {"product": "UL", "rate": 0.015},
            ]
        ).encode()
        # Filename stem must not collide with any throwaway-pack table key.
        r5 = js(batch([(JSON_NAME + ".json", recs, "application/json")], commit=True))
        ok(
            r5.get("rows_total") == 2
            and JSON_NAME in [t["key"] for t in r5.get("created_tables", [])],
            "data JSON (array of records) imported",
            r5.get("created_tables"),
        )

        # ------------------------------------------- 6. ontology JSON rejected
        onto = json.dumps(
            {"slug": "x", "entities": [{"key": "a", "label_en": "A"}]}
        ).encode()
        r6 = js(batch([("pack_like.json", onto, "application/json")]))
        oi = next(
            (i for i in r6.get("items", []) if i["file"] == "pack_like.json"), None
        )
        ok(
            oi is not None and "Import pack" in (oi.get("parse_error") or ""),
            "ontology JSON rejected with Import-pack pointer",
            oi and oi.get("parse_error"),
        )
        ok(r6.get("ok") is False, "batch flagged not ready", r6.get("ok"))

        # ------------------------------------------- 7. unusable item aborts all
        mixed = [
            ("empty.xlsx", empty_bytes, xlsx_mime),
            ("safe_new.csv", b"alpha,beta\n1,2\n", "text/csv"),
        ]
        prev = js(batch(mixed))
        ok(prev.get("ok") is False, "empty item flagged before commit", prev.get("ok"))
        res7 = batch(mixed, commit=True)
        ok(
            400 <= res7.status_code < 500,
            "commit refused while an item is unusable",
            res7.status_code,
        )
        with get_conn() as conn:
            leaked = conn.execute(
                "SELECT 1 FROM pack_tables WHERE pack_id=? AND key IN ('safe_new','only_header')",
                (pid,),
            ).fetchone()
        ok(leaked is None, "nothing written when the batch was rejected")

    # ------------------------------------------- 8. cleanup
    with get_conn() as conn:
        cleanup(conn, pid, TEST_KEYS + ["only_header"])
        adm.sync_visual_graph(conn, "verify_t8", pid)
        keys_csv = ",".join(f"'{k}'" for k in TEST_KEYS)
        left = conn.execute(
            f"SELECT COUNT(*) n FROM pack_tables WHERE pack_id=? AND key IN ({keys_csv})",
            (pid,),
        ).fetchone()["n"]
    ok(left == 0, "all test tables cleaned up", left)

    if created:
        delete_pack(pack_id=pid)
        print("  helper-created fixture pack removed")

    print(f"\n  {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
