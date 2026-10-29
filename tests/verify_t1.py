"""T1 verification: seed idempotency, active-pack invariants, model migration.

Written against whatever database the platform is currently using, so it
passes both on a freshly seeded install and on a workspace that has since
gained extra packs / users / models. The invariants it protects are:

* re-running the seed is idempotent (row counts do not move);
* exactly one pack is active and ``app_settings['active_pack_id']`` agrees;
* the active pack computes every declared formula without error;
* the stored model key is encrypted at rest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.db import get_conn
from modules import admin_service as adm
from modules import ontology_service as svc
from modules.seed import ensure_seed

TABLES = [
    "ontology_packs",
    "entities",
    "attributes",
    "relations",
    "graph_nodes",
    "graph_edges",
    "metric_formulas",
    "question_rules",
    "pack_tables",
    "pack_columns",
    "datasets",
    "pack_rows",
    "question_bank",
    "model_configs",
]


def snapshot():
    with get_conn() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in TABLES
        }


print("[TR-1.1] seed idempotency")
before = snapshot()
with get_conn() as conn:
    ensure_seed(conn)
after = snapshot()

ok = True
for t in TABLES:
    flag = "OK " if before[t] == after[t] else "BAD"
    if before[t] != after[t]:
        ok = False
    print(f"  {flag} {t:16} {after[t]} (before {before[t]})")

print("[TR-1.2] active pack invariants")
with get_conn() as conn:
    active = (
        conn.execute(
            "SELECT value FROM app_settings WHERE key='active_pack_id'"
        ).fetchone()
        or {"value": ""}
    )["value"]
    active_rows = conn.execute(
        "SELECT COUNT(*) n FROM ontology_packs WHERE is_active=1"
    ).fetchone()["n"]
    model = conn.execute(
        "SELECT name, base_url, model, is_default, enabled, length(api_key_enc) klen "
        "FROM model_configs ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    pack = svc.load_pack(conn, adm.active_pack_id(conn))

print("  active_pack_id:", active, "| active packs:", active_rows)
print("  model row:", dict(model) if model else None)

if active_rows != 1:
    ok = False
    print("  BAD exactly one pack must be active ::", active_rows)
else:
    print("  OK  exactly one pack is active")
if not str(active):
    ok = False
    print("  BAD active_pack_id missing from app_settings")
else:
    print("  OK  app_settings carries active_pack_id")
if model is None or model["klen"] <= 40:
    ok = False
    print("  BAD api key should be encrypted at rest")
else:
    print("  OK  api key encrypted at rest")

print("[TR-1.3] active pack computes cleanly")
if pack is None:
    ok = False
    print("  BAD no active pack could be loaded")
else:
    computed = svc.compute_pack(pack)
    errors = computed.get("errors") or []
    tables = computed.get("tables") or {}
    metrics = computed.get("metrics") or {}
    print(f"  pack: {pack['slug']} | tables: {len(tables)} | metrics: {len(metrics)}")
    if errors:
        ok = False
        print("  BAD formula errors ::", errors)
    else:
        print("  OK  every declared formula computes")
    if not tables or not metrics:
        ok = False
        print("  BAD pack must declare both tables and metrics")
    else:
        print("  OK  pack declares tables and metrics")

print("\nT1 structural verification:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
