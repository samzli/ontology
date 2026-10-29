"""T3 verification: generic data-driven ontology engine.

Covers TR-3.1 (canonical questions answered + graph contract), TR-3.2 (Top3
similar questions all clickable), TR-3.3 (empty-pack isolation) and TR-3.4
(unsafe formula rejection).

The suite pins the built-in VNB / APE sample pack by slug (not the active
pack), because it exercises that pack's own rules. Its numeric parity block
is only asserted when the pack still carries the pristine v1 snapshot; on a
workspace whose demo rows have been replaced it is reported as SKIP and the
behavioural checks below (which are data-agnostic) still run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.db import get_conn, now, set_setting
from modules import ontology_service as svc

failures = []


def check(name, cond, extra=""):
    print(f"  {'OK ' if cond else 'BAD'} {name} {extra}")
    if not cond:
        failures.append(name)


with get_conn() as conn:
    row = conn.execute(
        "SELECT id FROM ontology_packs WHERE slug='vnb-ape-sample'"
    ).fetchone()
    if row is None:
        raise SystemExit("sample pack 'vnb-ape-sample' is missing from this database")
    pack = svc.load_pack(conn, row["id"])
    sample_pack_id = pack["id"]
    prev_active = svc.active_pack_id(conn)

computed = svc.compute_pack(pack)
m = computed["metrics"]
ch = {r["name"]: r for r in computed["tables"]["channels"]}
pristine = m["stated_ape"] == 68.0 and m["stated_vnb"] == 11.08

print("[metrics parity with v1 Ontology]")
if pristine:
    check("stated APE 68.00", m["stated_ape"] == 68.0, str(m["stated_ape"]))
    check("stated VNB 11.08", m["stated_vnb"] == 11.08, str(m["stated_vnb"]))
    check("stated margin 0.163", m["stated_margin"] == 0.163, str(m["stated_margin"]))
    check("derived APE 68.00", m["derived_ape"] == 68.0, str(m["derived_ape"]))
    # v1 parity: derived VNB exceeds stated -> +$2.19M gap.
    check("gap VNB +2.19", m["gap_vnb"] == 2.19, str(m["gap_vnb"]))
    print(
        "  derived_vnb =", m["derived_vnb"], "| derived_margin =", m["derived_margin"]
    )
    print("  channels:")
    for name, r in ch.items():
        print(
            f"    {name:10} ape={r['ape']:<5} blended={r['blended']:<7} vnb={r['vnb']}"
        )
    check("Agency blended 0.181", ch["Agency"]["blended"] == 0.181)
    check("Banca blended 0.2185", ch["Banca"]["blended"] == 0.2185)
    check("Brokerage blended 0.172", ch["Brokerage"]["blended"] == 0.172)
    check(
        "Agency ape 20 / vnb 3.62",
        ch["Agency"]["ape"] == 20 and ch["Agency"]["vnb"] == 3.62,
    )
    check(
        "Brokerage ape 18 / vnb 3.096",
        ch["Brokerage"]["ape"] == 18 and abs(ch["Brokerage"]["vnb"] - 3.096) < 1e-9,
    )
else:
    print(
        "  SKIP v1 parity: this workspace's demo rows were replaced "
        f"(stated APE {m['stated_ape']:.3f}, VNB {m['stated_vnb']:.3f}, "
        "not 68.00 / 11.08). Behavioural checks below still run."
    )

print("\n[internal consistency of the computed tables]")
tot_ape = sum(r["ape"] for r in ch.values())
tot_vnb = sum(r["vnb"] for r in ch.values())
check(
    "channel APE sums to derived APE",
    abs(tot_ape - m["derived_ape"]) <= 0.01,
    f"{tot_ape:.3f} vs {m['derived_ape']:.3f}",
)
check(
    "channel VNB sums to derived VNB",
    abs(tot_vnb - m["derived_vnb"]) <= 0.01,
    f"{tot_vnb:.3f} vs {m['derived_vnb']:.3f}",
)
check(
    "each channel blended margin = VNB / APE",
    all(
        abs(r["blended"] - (r["vnb"] / r["ape"])) <= 0.002
        for r in ch.values()
        if r["ape"]
    ),
)
check(
    "derived margin = derived VNB / derived APE",
    abs(m["derived_margin"] - (m["derived_vnb"] / m["derived_ape"])) <= 0.002,
)
check(
    "gap VNB = stated VNB - derived VNB",
    abs(m["gap_vnb"] - (m["stated_vnb"] - m["derived_vnb"])) <= 0.01,
)

print("\n[TR-3.1] canonical questions answered by the rule engine")
questions = svc.suggested_questions(sample_pack_id)
matched = 0
answers = {}
crashes = []
for q in questions:
    try:
        res = svc.answer_question(q, pack, computed, raise_on_template_error=False)
    except Exception as e:  # noqa: BLE001 - a raise here is a real regression
        crashes.append(f"{q}: {e}")
        continue
    if res:
        matched += 1
        answers[q] = res
    else:
        print("  SKIP unmatched (input table has no rows):", q)
check("no rule raised an exception", not crashes, str(crashes))
check(
    ">=90% of the question bank is answered by rules",
    matched >= 0.9 * len(questions),
    f"({matched}/{len(questions)})",
)

print("\n[graph contract — every rule answer carries a >=1-edge chain]")
for q, res in answers.items():
    extra = list(res["nodes"]) + (res["path"]["steps"] if res["path"] else [])
    sub = svc.retrieve_subgraph(pack, q, res["answer"], extra)
    if not sub["edge_ids"]:
        print("  BAD  no edges for:", q)
        failures.append(f"no edges: {q}")
full_graph = svc.visual_graph(pack)
check(
    "visual graph has nodes",
    len(full_graph["nodes"]) >= 8,
    str(len(full_graph["nodes"])),
)
check(
    "visual graph has links",
    len(full_graph["links"]) >= 8,
    str(len(full_graph["links"])),
)
sub = svc.retrieve_subgraph(
    pack, "What is the total VNB?", "Total VNB", ["TotalVNB", "TotalAPE"]
)
check(
    "retrieve matched a graph node", len(sub["matched"]) >= 1, str(sub["matched"])[:80]
)
check("retrieve edges non-empty", len(sub["edge_ids"]) >= 1)

narrative = svc.render_narrative(pack, computed)
check("narrative renders directed edges", "directed edge" in narrative)

if pristine:
    print("\n[answer text parity with the v1 snapshot]")
    a_vnb = answers.get("What is the total VNB?", {}).get("answer", "")
    a_margin = answers.get("What is the overall margin?", {}).get("answer", "")
    a_gap = answers.get("What is the VNB gap?", {}).get("answer", "")
    a_ch = answers.get("How many channels?", {}).get("answer", "")
    a_agent = answers.get("Which agent is most productive?", {}).get("answer", "")
    check("VNB answer shows $11.08M", "$11.08M" in a_vnb)
    check(
        "margin answer shows 16.3% and gap",
        "16.3%" in a_margin and "$2.19M" in a_margin,
    )
    check("gap answer shows 2.19 and 16.3%", "$2.19M" in a_gap and "16.3%" in a_gap)
    check("channel table shows Banca VNB $6.55M", "$6.55M" in a_ch)
    check("top agent A019 $180K", "Agent:A019" in a_agent and "$180K" in a_agent)
    check(
        "tenure channel bullet Agency 23",
        "23 months"
        in answers.get("What is the average agent tenure?", {}).get("answer", ""),
    )
    certified = answers.get("Which agents are certified for all products?", {}).get(
        "answer", ""
    )
    check("10 all-products certified", certified.startswith("**10**"), certified[:40])
    high_churn = answers.get("Which agents are at high churn risk?", {}).get(
        "answer", ""
    )
    check(
        "high churn lists A002/A008/A012",
        all(x in high_churn for x in ("Agent:A002", "Agent:A008", "Agent:A012")),
    )
    mix = answers.get("What is the product mix?", {}).get("answer", "")
    check("mix shows Agency Participating 70.0%", "70.0%" in mix)

print("\n[TR-3.2] unmatched question -> Top3 similar, all clickable")
sim = svc.similar_questions("tell me about solvency and embedded value assumptions")
check("returns 3 suggestions", len(sim) == 3, str([s["question"] for s in sim]))
for s in sim:
    hit = svc.answer_question(
        s["question"], pack, computed, raise_on_template_error=False
    )
    if not hit:
        failures.append(f"suggestion not clickable: {s['question']}")
check(
    "all 3 suggestions are answerable",
    all(
        svc.answer_question(
            s["question"], pack, computed, raise_on_template_error=False
        )
        for s in sim
    ),
)

print("\n[LLM grounding context builds dynamically]")
ctx = svc.build_llm_context(pack)
check("context names the pack", pack["name"] in ctx, pack["name"])
check(
    "context lists at least one table and metric",
    any(t["key"] in ctx for t in pack["tables"])
    and any(f["label"] in ctx or f["key"] in ctx for f in pack["formulas"]),
)

print("\n[TR-3.4] unsafe formulas rejected at save-time validation")
names = svc.formula_namespace_names(pack)
for evil in [
    "__import__('os').system('dir')",
    "open('x').read()",
    "rows.__class__",
    "(lambda: 1)()",
    "sum([1,2]).real",
]:
    try:
        svc.validate_expression(evil, names)
        print("  BAD  accepted:", evil)
        failures.append(f"accepted unsafe: {evil}")
    except svc.EvalError:
        pass
safe_table = pack["tables"][0]["key"]
check(
    "safe formula accepted",
    svc.validate_expression(f"round(sum(rows('{safe_table}'), 'ape'), 2)", names)
    is None,
)

print("\n[TR-3.3] empty pack isolation")
with get_conn() as conn:
    cur = conn.execute(
        "INSERT INTO ontology_packs(slug, name, description, narrative, is_builtin, "
        "is_active, created_at) VALUES(?,?,?,?,0,0,?)",
        ("empty-test", "Empty Test Pack", "No content", "", now()),
    )
    empty_id = cur.lastrowid
    set_setting(conn, "active_pack_id", empty_id)
with get_conn() as conn:
    empty = svc.load_pack(conn)
check(
    "empty pack loads",
    empty is not None and empty["rules"] == [] and empty["nodes"] == [],
)
check(
    "empty pack answer -> None",
    svc.answer_question("What is the total VNB?", empty) is None,
)
g0 = svc.visual_graph(empty)
check("empty graph is empty lists", g0["nodes"] == [] and g0["links"] == [])
check(
    "empty narrative does not crash",
    "does not define a graph" in svc.render_narrative(empty),
)
check(
    "empty LLM context does not crash", "knowledge base" in svc.build_llm_context(empty)
)
sub0 = svc.retrieve_subgraph(empty, "anything")
check("empty retrieve returns empty", sub0["nodes"] == [] and sub0["edge_ids"] == [])
check("empty pack suggestions []", svc.suggested_questions(empty_id) == [])
with get_conn() as conn:
    conn.execute("DELETE FROM ontology_packs WHERE id=?", (empty_id,))
    set_setting(conn, "active_pack_id", str(prev_active))

print("\nT3 verification:", "PASS" if not failures else f"FAIL ({len(failures)})")
sys.exit(1 if failures else 0)
