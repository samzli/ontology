"""T9 verification: the Actuarial Lapse Experience (Clean) pack, end to end.

Run against a running server:

    python main.py
    python tests/verify_t9.py

Uses the two files the user supplied, from this project's ``data/`` dir:

  * ``actuarial_experience_study_ontology - Clean.json``  (an onti-pack/v1 export)
  * ``Lapse_mockdata_v3 - Clean.xlsx``                    (4 sheets, one per table)

The test imports the ontology as a temporary pack, uploads the workbook through
the batch importer (one sheet -> one table), and then checks that:

  1. the import creates a pack carrying that ontology's own metric formulas
     (M1-M7, incl. CSM / ISR / EV closing values);
  2. the batch importer routes each sheet to its matching table (no new tables);
  3. ``/api/dashboard`` renders the pack's *own* formulas as KPI cards, so the
     CSM / EV numbers actually show up instead of an empty carousel;
  4. ``/api/analytics/timeseries`` reports the missing monthly schema
     (``requirements``) instead of a bare "not available";
  5. ``/api/admin/proposals`` answers for the pack (the AI-proposal page).

The temporary pack is activated only for the round-trip and is deleted at the
end, leaving the previously-active pack in place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8010"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
ONTOLOGY_JSON = DATA_DIR / "actuarial_experience_study_ontology - Clean.json"
LAPSE_XLSX = DATA_DIR / "Lapse_mockdata_v3 - Clean.xlsx"

TEMP_SLUG = "lapse_clean_verify"
EXPECTED_TABLES = ["monthly_experience", "lapse_assumptions", "financial_variance"]
EXPECTED_FORMULAS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
# Analytics only light up for the built-in VNB/APE monthly schema; the Clean
# lapse pack carries a different one, so every required table is "missing".
REQUIRED_MONTHLY = ["vnb_monthly", "channel_monthly", "agent_monthly", "agent_master"]

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


def main() -> int:
    for path in (ONTOLOGY_JSON, LAPSE_XLSX):
        if not path.exists():
            print(f"missing fixture: {path}")
            return 1

    payload = json.loads(ONTOLOGY_JSON.read_text(encoding="utf-8"))
    xlsx_bytes = LAPSE_XLSX.read_bytes()
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    pid = None
    with httpx.Client(base_url=BASE, timeout=120) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "Onti@2026"}
        )
        ok(r.status_code == 200, "admin login", r.status_code)

        # The pack that was active before the round-trip — restored at the end.
        packs0 = js(c.get("/api/admin/packs")).get("packs", [])
        prev_active = next((p for p in packs0 if p["is_active"]), None)
        ok(prev_active is not None, "found the previously-active pack")

        # ------------------------------------------------------ 1. import pack
        r = c.post(
            "/api/admin/packs/import",
            json={"payload": payload, "slug": TEMP_SLUG, "activate": True},
        )
        pid = js(r).get("id")
        ok(
            r.status_code == 200 and isinstance(pid, int),
            "import Clean ontology -> pack id",
            r.status_code,
        )
        if not pid:
            print("  " + str(js(r)))
            return 1

        packs = js(c.get("/api/admin/packs")).get("packs", [])
        imp = next((p for p in packs if p["id"] == pid), None)
        ok(
            imp is not None and imp["slug"] == TEMP_SLUG and imp["is_active"],
            "imported pack is created and active",
            imp,
        )
        ok(
            (imp or {}).get("name", "").startswith("Actuarial Lapse Experience"),
            "pack carries the Clean file's name",
            (imp or {}).get("name"),
        )

        # ------------------------------------------ 2. batch-import the workbook
        files = [("files", (LAPSE_XLSX.name, xlsx_bytes, xlsx_mime))]

        prev = js(
            c.post(
                "/api/admin/data/batch-upload",
                data={"pack_id": str(pid), "commit": "false"},
                files=files,
            )
        )
        ok(prev.get("ok") is True, "batch preview ready", prev.get("ok"))
        routed = {i["item"]: i["target"]["key"] for i in prev.get("items", [])}
        ok(
            bool(routed) and all(i["target"]["is_new"] is False for i in prev["items"]),
            "every sheet routes to its matching existing table",
            routed,
        )
        ok(
            all(i["valid_count"] > 0 for i in prev["items"]),
            "every sheet validates with rows",
            {i["item"]: i["valid_count"] for i in prev["items"]},
        )

        rows = js(
            c.post(
                "/api/admin/data/batch-upload",
                data={"pack_id": str(pid), "commit": "true"},
                files=files,
            )
        )
        ok(
            rows.get("rows_total", 0) > 0,
            "batch upload committed rows",
            rows.get("rows_total"),
        )
        ok(
            rows.get("created_tables") == [],
            "no stray tables created",
            rows.get("created_tables"),
        )
        committed = {i["table_key"] for i in rows.get("items", [])}
        ok(
            set(EXPECTED_TABLES) <= committed,
            "all four tables received data",
            committed,
        )

        tables = js(c.get(f"/api/admin/data/tables?pack_id={pid}")).get("tables", [])
        tkeys = sorted(t["key"] for t in tables)
        ok(set(EXPECTED_TABLES) <= set(tkeys), "expected tables present", tkeys)

        # ------------------------------------------------- 3. dashboard renders M1-M7
        dash = js(c.get("/api/dashboard"))
        ok(
            dash.get("pack", {}).get("id") == pid,
            "dashboard scoped to the pack",
            dash.get("pack"),
        )
        ok(
            dash.get("formula_count") == len(EXPECTED_FORMULAS),
            "pack declares 7 metric formulas",
            dash.get("formula_count"),
        )
        kpi_keys = [k["key"] for k in dash.get("kpis", [])]
        ok(
            set(EXPECTED_FORMULAS) <= set(kpi_keys),
            "all M1-M7 formulas surface as KPI cards",
            kpi_keys,
        )
        kpi_by_key = {k["key"]: k for k in dash.get("kpis", [])}
        for k in ("M5", "M6", "M7"):  # CSM / ISR / EV closing values
            card = kpi_by_key.get(k)
            ok(
                card is not None and "n/a" not in str(card.get("value")),
                f"{k} KPI shows a computed value",
                card,
            )
        print(
            "      KPI cards: "
            + ", ".join(f"{k['key']}={k['value']}" for k in dash.get("kpis", []))
        )
        m7 = kpi_by_key.get("M7") or {}
        ok(
            bool(m7.get("definition"))
            and "financial_variance" in (m7.get("source_tables") or []),
            "KPI card carries its formula + source table for the in-card explainer",
            {k: m7.get(k) for k in ("definition", "source_tables")},
        )

        # ------------------------------------------------- 4. analytics guidance
        ts = js(c.get("/api/analytics/timeseries"))
        req = ts.get("requirements") or {}
        ok(
            req.get("available") is False,
            "analytics flag this pack as not covered",
            req.get("available"),
        )
        ok(
            sorted(req.get("missing_tables", [])) == sorted(REQUIRED_MONTHLY),
            "analytics report the missing monthly schema",
            req.get("missing_tables"),
        )
        ok(
            "monthly_experience" in (req.get("present_tables") or []),
            "analytics report the tables the pack does carry",
            req.get("present_tables"),
        )

        # ------------------------------------------------- 5. AI-proposal endpoint
        r = c.get(f"/api/admin/proposals?pack_id={pid}&status=pending")
        ok(r.status_code == 200, "AI-proposal page answers 200", r.status_code)

    # ------------------------------------------------------------- 6. cleanup
    with httpx.Client(base_url=BASE, timeout=60) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "Onti@2026"})
        if prev_active:
            c.post(f"/api/admin/packs/{prev_active['id']}/activate")
        if pid:
            r = c.delete(f"/api/admin/packs/{pid}")
            ok(r.status_code == 200, "temporary pack deleted", r.status_code)
        packs = js(c.get("/api/admin/packs")).get("packs", [])
        ok(all(p["id"] != pid for p in packs), "temporary pack gone")
        now_active = next((p for p in packs if p["is_active"]), None)
        ok(
            now_active is not None
            and now_active["id"] == (prev_active or {}).get("id"),
            "previously-active pack restored",
            now_active,
        )

    print(f"\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
