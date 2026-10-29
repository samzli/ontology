"""T5 verification: admin backend — ontology CRUD, packs, import pipeline,
question bank, instance graph and dashboard stats."""

import io
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook

from modules import admin_service as adm
from modules import importer
from modules import ontology_service as svc
from modules.db import _SCHEMA, get_conn

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


ACTOR = "rootadmin"

with get_conn() as conn:
    # Idempotency: drop leftovers from earlier interrupted runs.
    stale = conn.execute(
        "SELECT id FROM ontology_packs WHERE slug='t5-import-lab'"
    ).fetchone()
    if stale:
        conn.execute("DELETE FROM ontology_packs WHERE id=?", (stale["id"],))
    conn.commit()
    pack_id = conn.execute(
        "SELECT id FROM ontology_packs WHERE slug='vnb-ape-sample'"
    ).fetchone()["id"]

    print("[TR-5.4] referential integrity — referenced node cannot be deleted")
    try:
        adm.delete_node(conn, ACTOR, pack_id, "TotalVNB")
        check("delete referenced node -> 409", False, "no exception")
    except adm.AdminConflict as e:
        keys = {c["key"] for c in e.conflicts if c["type"] == "edge"}
        check(
            "delete referenced node -> 409 with edge list",
            e.status_code == 409 and "E3" in keys,
            str(sorted(keys)),
        )
    check(
        "node still exists",
        conn.execute(
            "SELECT 1 FROM graph_nodes WHERE pack_id=? AND key='TotalVNB'", (pack_id,)
        ).fetchone()
        is not None,
    )

    print("[ontology CRUD happy paths + validation]")
    adm.upsert_entity(
        conn,
        ACTOR,
        pack_id,
        {"key": "_t_entity", "label_en": "Test Entity", "layer": 4},
    )
    adm.upsert_attribute(
        conn,
        ACTOR,
        pack_id,
        {"entity_key": "_t_entity", "key": "note", "label_en": "Note"},
    )
    adm.upsert_relation(
        conn,
        ACTOR,
        pack_id,
        {
            "key": "_t_rel",
            "source_key": "_t_entity",
            "target_key": "insurance_product",
            "label_en": "Test rel",
        },
    )
    adm.upsert_node(
        conn,
        ACTOR,
        pack_id,
        {"key": "_t_node", "label": "Test Node", "aliases": ["tnode"]},
    )
    adm.upsert_edge(
        conn,
        ACTOR,
        pack_id,
        {
            "key": "_t_edge",
            "source_key": "_t_node",
            "target_key": "TotalVNB",
            "label": "tests",
        },
    )
    try:
        adm.upsert_edge(
            conn,
            ACTOR,
            pack_id,
            {"key": "_t_bad", "source_key": "_t_node", "target_key": "Ghost"},
        )
        check("edge to ghost node rejected", False)
    except adm.AdminValueError as e:
        check("edge to ghost node rejected", e.status_code == 422)
    adm.upsert_formula(
        conn,
        ACTOR,
        pack_id,
        {"key": "_t_metric", "expression": "1 + 1", "fmt": "number"},
    )
    try:
        adm.upsert_formula(
            conn,
            ACTOR,
            pack_id,
            {"key": "_t_evil", "expression": "__import__('os').system('echo x')"},
        )
        check("malicious formula rejected at save time", False)
    except adm.AdminValueError:
        check("malicious formula rejected at save time", True)
    try:
        adm.upsert_rule(
            conn,
            ACTOR,
            pack_id,
            {"key": "_t_rule", "patterns": ["[bad("], "template": "x"},
        )
        check("invalid regex rule rejected", False)
    except adm.AdminValueError:
        check("invalid regex rule rejected", True)
    try:
        adm.delete_entity(conn, ACTOR, pack_id, "_t_entity")
        check("entity in use -> 409", False)
    except adm.AdminConflict:
        check("entity in use -> 409", True)
    # cleanup scaffolding
    adm.delete_formula(conn, ACTOR, pack_id, "_t_metric")
    adm.delete_edge(conn, ACTOR, pack_id, "_t_edge")
    adm.delete_node(conn, ACTOR, pack_id, "_t_node")
    adm.delete_relation(conn, ACTOR, pack_id, "_t_rel")
    adm.delete_attribute(conn, ACTOR, pack_id, "_t_entity", "note")
    adm.delete_entity(conn, ACTOR, pack_id, "_t_entity")
    check(
        "scaffolding cleaned",
        conn.execute(
            "SELECT COUNT(*) n FROM entities WHERE pack_id=? AND key='_t_entity'",
            (pack_id,),
        ).fetchone()["n"]
        == 0,
    )

    # ---- dedicated pack for the import pipeline ---------------------------
    tid_pack = adm.create_pack(
        conn, ACTOR, slug="t5-import-lab", name="T5 Import Lab", description="transient"
    )
    adm.upsert_entity(
        conn, ACTOR, tid_pack, {"key": "widget", "label_en": "Widget", "layer": 3}
    )
    tcur = conn.execute(
        "INSERT INTO pack_tables(pack_id,key,label,entity_key) VALUES(?,?,?,?)",
        (tid_pack, "widgets", "Widgets", "widget"),
    )
    table_id = int(tcur.lastrowid)
    for c in (
        ("id", "Widget ID", "STRING", 1, ""),
        ("name", "Widget Name", "STRING", 1, ""),
        ("score", "Score", "FLOAT", 0, ""),
        ("grade", "Grade", "STRING", 1, "A;B;C"),
    ):
        conn.execute(
            "INSERT INTO pack_columns(table_id,key,label,data_type,required,enum_values,fmt)"
            " VALUES(?,?,?,?,?,?, 'raw')",
            (table_id, c[0], c[1], c[2], c[3], c[4]),
        )

    conn.commit()  # make the new table visible to preview_upload's own connection
    print(
        "[TR-5.1] bad CSV -> preview errors, zero rows; good CSV -> rows reachable in Q&A"
    )
    bad_csv = (
        "id,name,score,grade\n"
        "W1,Alpha,92.5,A\n"
        "W2,,notanumber,Z\n"  # missing name + bad number + bad enum
        "W3,Gamma,,B\n"
    ).encode("utf-8")
    _, report = importer.preview_upload(table_id, "bad.csv", bad_csv)
    check(
        "bad CSV reports exactly 1 invalid row",
        report["total"] == 3
        and report["valid_count"] == 2
        and report["error_count"] == 1,
        json.dumps(report["errors"]),
    )
    check("error row is spreadsheet row 3", report["errors"][0]["row"] == 3)
    try:
        importer.commit_upload(conn, ACTOR, table_id, "bad set", "bad.csv", bad_csv)
        check("commit of bad CSV rejected", False)
    except importer.UploadError as e:
        check("commit of bad CSV rejected", e.status_code == 400)
    n_rows = conn.execute(
        "SELECT COUNT(*) n FROM pack_rows pr JOIN datasets d ON d.id=pr.dataset_id "
        "WHERE d.table_id=?",
        (table_id,),
    ).fetchone()["n"]
    check("zero rows persisted from bad CSV", n_rows == 0, str(n_rows))

    good_csv = (
        "ID,Widget Name,Score,Grade\n" "W1,Alpha,92.5,A\nW2,Beta,81,B\nW3,Gamma,70,C\n"
    ).encode("utf-8")
    _, preview = importer.preview_upload(table_id, "good.csv", good_csv)
    check(
        "mapping guessed from labels",
        preview["mapping"].get("Widget Name") == "name"
        and preview["mapping"].get("Grade") == "grade",
        json.dumps(preview["mapping"]),
    )
    committed = importer.commit_upload(
        conn, ACTOR, table_id, "Good set", "good.csv", good_csv
    )
    check("good CSV commits 3 rows", committed["rows"] == 3, str(committed))
    # metric + rule prove the imported data is live in the ontology engine
    adm.upsert_formula(
        conn,
        ACTOR,
        tid_pack,
        {
            "key": "widget_score_total",
            "expression": "round(sum(rows('widgets'), 'score'), 2)",
            "fmt": "number",
        },
    )
    adm.upsert_rule(
        conn,
        ACTOR,
        tid_pack,
        {
            "key": "widget_total",
            "category": "widget",
            "patterns": ["total.*score", "score.*total"],
            "template": "Total widget score is **{{ m.widget_score_total }}**.",
        },
    )
    pack = svc.load_pack(conn, tid_pack)
    computed = svc.compute_pack(pack)
    m = computed["metrics"]
    check(
        "engine computes metric over imported rows",
        m["widget_score_total"] == 243.5,
        str(m.get("widget_score_total")),
    )
    ans = svc.answer_question("what is the total score?", pack, computed)
    check(
        "rule answer reads imported value",
        ans is not None and "243.5" in ans["answer"],
        str(ans and ans["answer"]),
    )

    print("[TR-5.2] XLSX path works; size / extension / traversal guards")
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "name", "score", "grade"])
    ws.append(["W4", "Delta", 60.0, "B"])
    ws.append(["W5", "Epsilon", 55.25, "C"])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx = buf.getvalue()
    xres = importer.commit_upload(
        conn, ACTOR, table_id, "Excel set", "widgets.xlsx", xlsx
    )
    check("XLSX commits 2 rows", xres["rows"] == 2)
    try:
        importer.validate_upload("evil.exe", 10)
        check(".exe rejected", False)
    except importer.UploadError:
        check(".exe rejected", True)
    try:
        importer.validate_upload("../escape.csv", 10)
        check("path traversal filename rejected", False)
    except importer.UploadError:
        check("path traversal filename rejected", True)
    try:
        importer.validate_upload("big.csv", 11 * 1024 * 1024)
        check(">10MB rejected", False)
    except importer.UploadError as e:
        check(">10MB rejected", e.status_code == 413)

    print("[TR-5.3] question list import — TXT + CSV + dedup")
    txt = (
        "What is a widget?\nWhat is a widget?\n   \n# comment line\n"
        "How are scores graded?\n"
    ).encode("utf-8")
    items_txt = importer.parse_question_file("questions.txt", txt)
    r1 = adm.import_questions(conn, ACTOR, tid_pack, items_txt)
    check(
        "TXT: 2 inserted, 1 duplicate, blanks/comments skipped",
        r1["inserted"] == 2 and r1["duplicates"] == 1,
        str(r1),
    )
    csv_q = (
        "Question,Category\n" "What is a widget?,general\nNew widget question?,ops\n"
    ).encode("utf-8")
    r2 = adm.import_questions(
        conn, ACTOR, tid_pack, importer.parse_question_file("q.csv", csv_q)
    )
    check(
        "CSV: 1 inserted, 1 duplicate",
        r2["inserted"] == 1 and r2["duplicates"] == 1,
        str(r2),
    )
    bank = adm.list_questions(conn, tid_pack)
    check("question bank holds 3 unique", len(bank) == 3, str(len(bank)))

    print("[TR-5.6] instance graph counts and joins")
    g = adm.instance_graph(conn, pack_id)
    inst_rows = conn.execute(
        "SELECT COUNT(*) n FROM pack_rows pr JOIN datasets d ON d.id=pr.dataset_id "
        "WHERE d.pack_id=?",
        (pack_id,),
    ).fetchone()["n"]
    check(
        "instance node total == DB row count",
        g["instances"]["total_rows"] == inst_rows,
        f"{g['instances']['total_rows']} vs {inst_rows}",
    )
    check(
        "schema layer mirrors the pack ontology",
        len(g["schema"]["nodes"]) >= 5 and len(g["schema"]["edges"]) >= 5,
        f"{len(g['schema']['nodes'])}/{len(g['schema']['edges'])}",
    )
    print("  info  FK-derived instance edges:", len(g["instances"]["edges"]))
    check(
        "instance graph exposes schema edges",
        len(g["schema"]["edges"]) > 0,
        str(len(g["schema"]["edges"])),
    )
    gf = adm.instance_graph(conn, pack_id, entity_filter="Product")
    check(
        "entity filter narrows nodes",
        all(n["entity"] == "Product" for n in gf["instances"]["nodes"])
        and len(gf["instances"]["nodes"]) < g["instances"]["total_rows"],
    )

    # ---- dashboard ---------------------------------------------------------
    stats = adm.dashboard_stats(conn)
    check(
        "dashboard stats populated",
        stats["packs"] >= 2
        and stats["entities"] >= 1
        and stats["tables"] >= 1
        and stats["instance_rows"] >= 1
        and bool(stats["active_pack_id"]),
        json.dumps(stats)[:300],
    )

    print("[TR-5.5] pack export -> empty database import -> full roundtrip")
    bundle = adm.export_pack(conn, pack_id)
    check(
        "bundle carries seed data",
        len(bundle["entities"]) >= 5
        and len(bundle["question_rules"]) >= 25
        and len(bundle["tables"]) >= 6,
        json.dumps({k: len(v) for k, v in bundle.items() if isinstance(v, list)}),
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    fresh = sqlite3.connect(tmp.name)
    fresh.row_factory = sqlite3.Row
    fresh.executescript(_SCHEMA)
    imported_id = adm.import_pack(fresh, ACTOR, bundle, slug_override="restored-copy")
    fresh.commit()

    def counts(c, pid):
        return {
            t: c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE pack_id=?", (pid,)
            ).fetchone()[0]
            for t in (
                "entities",
                "attributes",
                "relations",
                "graph_nodes",
                "graph_edges",
                "metric_formulas",
                "question_rules",
                "pack_tables",
                "datasets",
                "question_bank",
            )
        }

    src_counts = counts(conn, pack_id)
    new_counts = counts(fresh, imported_id)
    check(
        "imported counts match source (10 tables)",
        src_counts == new_counts,
        json.dumps({"src": src_counts, "new": new_counts}),
    )
    col_rows = fresh.execute(
        """
        SELECT COUNT(*) n FROM pack_columns pc
          JOIN pack_tables pt ON pt.id=pc.table_id WHERE pt.pack_id=?""",
        (imported_id,),
    ).fetchone()["n"]
    col_src = conn.execute(
        """
        SELECT COUNT(*) n FROM pack_columns pc
          JOIN pack_tables pt ON pt.id=pc.table_id WHERE pt.pack_id=?""",
        (pack_id,),
    ).fetchone()["n"]
    check("columns match", col_rows == col_src, f"{col_rows}/{col_src}")
    pr_rows = fresh.execute(
        """
        SELECT COUNT(*) n FROM pack_rows pr JOIN datasets d ON d.id=pr.dataset_id
          WHERE d.pack_id=?""",
        (imported_id,),
    ).fetchone()["n"]
    pr_src = conn.execute(
        """
        SELECT COUNT(*) n FROM pack_rows pr JOIN datasets d ON d.id=pr.dataset_id
          WHERE d.pack_id=?""",
        (pack_id,),
    ).fetchone()["n"]
    check(
        "data rows match the source pack",
        pr_rows == pr_src and pr_rows > 0,
        f"{pr_rows} vs {pr_src}",
    )
    # imported pack must compute & answer in the fresh DB
    fp = svc.load_pack(fresh, imported_id)
    fc = svc.compute_pack(fp)
    fa = svc.answer_question(
        "What is the total VNB?", fp, fc, raise_on_template_error=False
    )
    check(
        "imported pack answers in fresh DB",
        fa is not None and bool(fa["answer"].strip()),
        (fa or {}).get("answer", "")[:80],
    )
    fresh.close()
    os.unlink(tmp.name)

    # cleanup transient lab
    adm.delete_pack(conn, ACTOR, tid_pack)
    check(
        "lab pack deleted",
        conn.execute("SELECT 1 FROM ontology_packs WHERE id=?", (tid_pack,)).fetchone()
        is None,
    )

    audited = conn.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action LIKE 'pack.%' OR action LIKE 'node.%' "
        "OR action LIKE 'dataset.%' OR action='questions.import'"
    ).fetchone()["n"]
    check("admin actions audited", audited >= 8, str(audited))

print()
print(f"T5 verification: {'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
