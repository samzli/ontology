"""T7 verification: multi-sheet upload selection, manual column-mapping
overrides and AI-proposal evidence.

Run against a running server; the fixture pack is created on demand by
``tests/_seed_synthetic.py`` (and removed again if the helper created it):

    python main.py                 (or: uvicorn main:app --port 8010)
    python tests/verify_t7.py

Covers the administrator data-import path end to end:
  1. workbook sheet listing + default first-sheet pick
  2. explicit sheet selection changes the columns read
  3. unknown sheet -> clear 4xx error naming the available sheets
  4. manual column-mapping override (force-map and explicit ignore)
  5. commit with sheet + mapping (the test dataset is then deleted)
  6. proposal evidence + rationale stored and returned by the API
  7. ontology_proposals.evidence column present (migration)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import induction  # noqa: E402
from modules.db import get_conn  # noqa: E402
from _seed_synthetic import delete_pack, ensure_synthetic_pack  # noqa: E402

BASE = "http://127.0.0.1:8010"
XLSX = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "Lapse_mockdata_v3 - Scenario Demo.xlsx"
)
PACK_SLUG = "lapse_experience_synthetic_demo"
TEST_KEY = "EvidenceTestEntity"
# Sheets of the real Scenario-Demo workbook (the client's actual demo file).
EXPECTED_SHEETS = [
    "Monthly_Experience",
    "Lapse_Assumptions",
    "Product_Master",
    "Channel_Master",
]
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


def main():
    if not XLSX.exists():
        print(f"missing fixture: {XLSX}")
        raise SystemExit(1)

    pid, created = ensure_synthetic_pack()
    print(
        f"  fixture pack {pid} ({'created by helper' if created else 'already seeded'})"
    )

    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "Onti@2026"}
        )
        ok(r.status_code == 200, "admin login", r.status_code)

        table = js(c.get("/api/admin/data/tables", params={"pack_id": pid}))["tables"][
            0
        ]
        tid = table["id"]
        print(
            f"  pack={pid} table={tid} ({table['key']}, {len(table['columns'])} columns)"
        )

        def upload(sheet="", mapping=None, commit=False):
            data = {
                "pack_id": str(pid),
                "table_id": str(tid),
                "commit": "true" if commit else "false",
            }
            if sheet:
                data["sheet"] = sheet
            if mapping is not None:
                data["mapping"] = json.dumps(mapping)
            if commit:
                data["dataset_name"] = "Sheet/mapping test dataset"
            files = {
                "file": (
                    XLSX.name,
                    XLSX.read_bytes(),
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                )
            }
            return c.post("/api/admin/data/upload", data=data, files=files)

        # ------------------------------------------------ 1. sheets + default
        resp1 = upload()
        r1 = js(resp1)
        ok(resp1.status_code == 200, "preview default sheet -> 200")
        ok(
            set(r1.get("sheets", [])) == set(EXPECTED_SHEETS),
            "all sheets listed (order-independent)",
            r1.get("sheets"),
        )
        ok(
            r1.get("sheet") == "Monthly_Experience",
            "defaults to first sheet",
            r1.get("sheet"),
        )
        ok(
            isinstance(r1.get("headers"), list)
            and isinstance(r1.get("instance_columns"), list),
            "headers + instance_columns returned",
        )
        first_headers = set(r1.get("headers", []))

        # ------------------------------------------------ 2. explicit sheet
        r2 = js(upload(sheet="Lapse_Assumptions"))
        ok(
            r2.get("sheet") == "Lapse_Assumptions",
            "explicit sheet echoed back",
            r2.get("sheet"),
        )
        ok(
            set(r2.get("headers", [])) != first_headers,
            "different sheet yields different headers",
        )

        # ------------------------------------------------ 3. unknown sheet
        bad = upload(sheet="NoSuchSheet")
        ok(
            400 <= bad.status_code < 500,
            "unknown sheet rejected (4xx)",
            bad.status_code,
        )
        msg = js(bad).get("error", "")
        ok(
            "Lapse_Assumptions" in msg and "not found" in msg.lower(),
            "error names available sheets",
            msg,
        )

        # ------------------------------------------------ 4. mapping override
        valid_key = r2["instance_columns"][0]["key"]
        target_hdr = r2["headers"][0]
        r4 = js(upload(sheet="Lapse_Assumptions", mapping={target_hdr: valid_key}))
        ok(
            r4.get("mapping", {}).get(target_hdr) == valid_key,
            f"header '{target_hdr}' forced to '{valid_key}'",
            r4.get("mapping"),
        )
        r5 = js(upload(sheet="Lapse_Assumptions", mapping={target_hdr: ""}))
        ok(target_hdr not in r5.get("mapping", {}), "explicit ignore drops the header")
        ok(
            target_hdr in r5.get("unmapped_headers", {}),
            "ignored header counted as unmapped",
        )

        # ------------------------------------------------ 5. commit + cleanup
        r6 = js(
            upload(sheet="Monthly_Experience", mapping=r1.get("mapping"), commit=True)
        )
        ok((r6.get("rows") or 0) > 0, "commit with sheet+mapping imported rows", r6)
        ok(
            r6.get("sheet") == "Monthly_Experience",
            "commit echoes sheet",
            r6.get("sheet"),
        )
        if r6.get("dataset_id"):
            d = c.delete(f"/api/admin/data/datasets/{r6['dataset_id']}")
            ok(d.status_code == 200, "test dataset cleaned up", d.status_code)

        # ------------------------------------------------ 6. proposal evidence
        with get_conn() as conn:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(ontology_proposals)"
                ).fetchall()
            }
            ok("evidence" in cols, "db migration added evidence column")
            meta = induction._table_meta(conn, tid)
            ev = induction._build_evidence(
                meta, [{"month": "2024-01", "product": "Term"}], 0
            )
            saved = induction._save_proposals(
                conn,
                pid,
                {
                    "entity": [
                        {
                            "key": TEST_KEY,
                            "label_en": "Evidence Test",
                            "layer": 3,
                            "description": "d",
                            "rationale": "justified by the month column",
                        }
                    ]
                },
                table_id=tid,
                dataset_id=None,
                evidence=ev,
            )
            ok(saved == 1, "evidence-backed proposal saved", saved)

        props = js(
            c.get("/api/admin/proposals", params={"pack_id": pid, "status": "pending"})
        ).get("proposals", [])
        mine = next((p for p in props if p["key"] == TEST_KEY), None)
        ok(mine is not None, "proposal returned by API")
        if mine:
            ok(isinstance(mine["evidence"], dict), "evidence parsed as object")
            ok(
                bool(mine["evidence"].get("table_label")),
                "evidence carries table label",
            )
            ok(len(mine["evidence"].get("columns", [])) > 0, "evidence carries columns")
            ok(
                len(mine["evidence"].get("sample_rows", [])) > 0,
                "evidence carries sample rows",
            )
            ok(
                mine.get("rationale") == "justified by the month column",
                "rationale stored",
            )

    # ------------------------------------------------ cleanup test proposal
    with get_conn() as conn:
        conn.execute("DELETE FROM ontology_proposals WHERE key=?", (TEST_KEY,))
        left = conn.execute(
            "SELECT COUNT(*) n FROM ontology_proposals WHERE key=?", (TEST_KEY,)
        ).fetchone()["n"]
    ok(left == 0, "test proposal cleaned up", left)

    if created:
        delete_pack(pack_id=pid)
        print("  helper-created fixture pack removed")

    print(f"\n  {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
