"""Self-iterating agent verification — unit, API integration and end-to-end.

Runs against a throwaway SQLite database: ``ONTI_DATA_DIR`` is pointed at a
temporary directory *before* any project module is imported, so the suite
never touches real workspace data (run_all executes each file in its own
process).

Covered surface:

* unit — toggle default/round-trip, deterministic critic, proposal keys,
  session recording, finding kinds, harvest + dedup, approval application;
* inner loop — stubbed-model refinement improves the score, rules-only path
  stays single-round, the round cap is respected;
* API  — status/toggle/compare3/findings/harvest endpoints, including the
  403 gate while the feature is off (the default);
* e2e  — alien question → gap findings → harvest → admin approval → the
  ontology pack physically gains the proposed node (outer-loop evolution).
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="onti_si_test_")
os.environ["ONTI_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from modules import induction  # noqa: E402
from modules import ontology_service as svc  # noqa: E402
from modules import self_iterate as si  # noqa: E402
from modules.db import get_conn, init_db  # noqa: E402
from modules.seed import ensure_seed  # noqa: E402

PASS = "UserPass123!"


def _seed() -> int:
    """Initialise the throwaway DB and return the built-in pack id."""
    init_db()
    with get_conn() as conn:
        return ensure_seed(conn)


def _register(client: TestClient, username: str) -> None:
    r = client.post("/api/auth/register", json={"username": username, "password": PASS})
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# Unit — toggle, critic, harvest chain
# ---------------------------------------------------------------------------


def test_toggle(conn) -> None:
    """Per-user toggle defaults to OFF and round-trips."""
    assert si.is_enabled(conn, 999) is False
    si.set_enabled(conn, 999, True)
    assert si.is_enabled(conn, 999) is True
    si.set_enabled(conn, 999, False)
    assert si.is_enabled(conn, 999) is False
    print("  toggle default-off + round-trip OK")


def test_critic() -> None:
    """Deterministic critic: empty draft scores 0, grounded draft passes."""
    empty = si.critique("", "none", {}, {})
    assert empty["score"] == 0.0 and not empty["passed"]
    assert "missing_rule" in empty["issues"] and "thin_answer" in empty["issues"]

    full = si.critique(
        "x" * 500,
        "rules",
        {"graph_nodes": 3, "graph_edges": 2, "metric_count": 1, "table_count": 2},
        {"available": True, "depth": {"reasoning_steps": 5}},
    )
    assert full["score"] == 90.0 and full["passed"] and full["issues"] == []

    gaps = si.critique("x" * 500, "ai", {"table_count": 1}, {})
    assert "missing_rule" in gaps["issues"]
    assert "coverage_gap" in gaps["issues"]
    assert "metric_gap" in gaps["issues"]
    print("  critic scoring + gap issues OK")


def test_proposal_key() -> None:
    """Proposal keys are deterministic, slugged and length-bounded."""
    k1 = si._proposal_key("metric_gap", "Why did VNB drop in Q3?")
    k2 = si._proposal_key("metric_gap", "Why did VNB drop in Q3?")
    assert k1 == k2
    assert k1.startswith("si_metric_gap_") and len(k1) <= 60
    assert " " not in k1
    print("  proposal key OK:", k1)


def test_session_and_findings(conn, pack_id: int) -> None:
    """Sessions persist; only gap-kind issues become findings."""
    result = {
        "source": "ai",
        "iterations": [
            {"round": 1, "score": 45.0, "issues": ["missing_rule", "thin_answer"]},
            {"round": 2, "score": 60.0, "issues": ["coverage_gap"]},
        ],
        "initial_score": 45.0,
        "final_score": 60.0,
        "improved": True,
    }
    sid = si.record_session(
        conn, user_id=1, pack_id=pack_id, question="Q1 alien?", result=result
    )
    row = conn.execute(
        "SELECT iterations, initial_score, final_score, improved "
        "FROM self_iter_sessions WHERE id=?",
        (sid,),
    ).fetchone()
    assert row["iterations"] == 2 and row["improved"] == 1
    kinds = {
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM self_iter_findings WHERE session_id=?", (sid,)
        ).fetchall()
    }
    # thin_answer is a quality issue, not an ontology gap -> not persisted.
    assert kinds == {"missing_rule", "coverage_gap"}
    print("  session recording + gap-only findings OK")


def test_harvest_and_approve(conn, pack_id: int) -> None:
    """Harvest proposes, dedups, and approval evolves the pack."""
    first = si.harvest(conn, pack_id, "tester")
    assert first["proposed"], "expected proposals from open findings"

    # A repeat session over the same question exposes the same gap again;
    # the second harvest must dedup against the pending proposal instead of
    # creating a duplicate.
    repeat = {
        "source": "ai",
        "iterations": [
            {"round": 1, "score": 45.0, "issues": ["missing_rule"]},
        ],
        "initial_score": 45.0,
        "final_score": 45.0,
        "improved": False,
    }
    si.record_session(
        conn, user_id=1, pack_id=pack_id, question="Q1 alien?", result=repeat
    )
    second = si.harvest(conn, pack_id, "tester")
    assert second["proposed"] == [] and second["skipped"] >= 1
    findings = si.list_findings(conn, pack_id, "open")
    assert findings == [], "all findings must be proposed after harvest"
    dup = [
        f for f in si.list_findings(conn, pack_id, "") if f["kind"] == "missing_rule"
    ]
    assert all(f["status"] == "proposed" for f in dup)

    pid = first["proposed"][0]
    res = induction.approve_proposal(conn, "admin", pid)
    assert res["status"] == "approved"
    key = conn.execute(
        "SELECT key FROM ontology_proposals WHERE id=?", (pid,)
    ).fetchone()["key"]
    node = conn.execute(
        "SELECT id FROM graph_nodes WHERE pack_id=? AND key=?", (pack_id, key)
    ).fetchone()
    assert node is not None, "approved proposal must materialise as a node"
    print("  harvest -> dedup -> approve -> node materialised OK")


# ---------------------------------------------------------------------------
# Inner loop with a stubbed model
# ---------------------------------------------------------------------------


def _load_pack(conn, pack_id: int):
    pack = svc.load_pack(conn, pack_id)
    return pack, svc.compute_pack(pack, strict=False)


def test_inner_loop_refines(conn, pack_id: int) -> None:
    """A weak first draft is refined until the score improves."""
    pack, computed = _load_pack(conn, pack_id)
    responses = iter(
        [
            "short.",
            "A much fuller answer. " + ("VNB APE margin impact chain " * 30),
        ]
        * 3
    )

    def stub(system_prompt, question, history):
        text = next(responses, "final " + "grounded VNB " * 50)
        return {
            "status": "success",
            "response": text,
            "model": "stub",
            "tokens_used": 10,
            "latency_ms": 1,
        }

    res = si.run_inner_loop(
        "Why is VNB important?",
        "ai",
        pack,
        computed,
        {"id": 0, "model": "stub"},
        answer_fn=stub,
    )
    assert len(res["iterations"]) >= 2, "expected at least one refinement"
    assert res["final_score"] > res["initial_score"], res["iterations"]
    assert res["improved"] is True
    assert res["answer"], "best draft returned"
    print(
        "  inner loop refine OK: rounds=%d score %s -> %s"
        % (len(res["iterations"]), res["initial_score"], res["final_score"])
    )


def test_inner_loop_rules_single_round(conn, pack_id: int) -> None:
    """A passing rules answer never calls the model and stays one round."""
    pack, computed = _load_pack(conn, pack_id)
    res = si.run_inner_loop(
        "What is the total VNB this year?", "auto", pack, computed, None
    )
    assert len(res["iterations"]) == 1
    assert res["source"].startswith("rules")
    assert res["iterations"][0]["score"] > 0
    print("  rules single-round OK:", res["source"], res["iterations"][0]["score"])


def test_inner_loop_round_cap(conn, pack_id: int) -> None:
    """The loop stops at MAX_ROUNDS even when the score never passes."""

    def flat_stub(system_prompt, question, history):
        return {
            "status": "success",
            "response": "short.",
            "model": "stub",
            "tokens_used": 1,
            "latency_ms": 1,
        }

    pack, computed = _load_pack(conn, pack_id)
    res = si.run_inner_loop(
        "zz alien topic with no grounding at all",
        "ai",
        pack,
        computed,
        {"id": 0, "model": "stub"},
        answer_fn=flat_stub,
    )
    assert len(res["iterations"]) == si.MAX_ROUNDS
    assert res["improved"] is False
    print("  round cap OK: stopped at", len(res["iterations"]))


# ---------------------------------------------------------------------------
# API integration + end-to-end evolution
# ---------------------------------------------------------------------------


def test_api_flow(pack_id: int) -> None:
    """Full HTTP flow: gate, toggle, compare3, findings, harvest, approve."""
    client = TestClient(main.app)
    _register(client, "si_api_user")

    r = client.get("/api/self-iterate/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and "stats" in body

    q = {"question": "What is the claims frequency for Martian rovers?"}
    r = client.post("/api/ask/compare3", json=q)
    assert r.status_code == 403, r.text  # default OFF gate

    r = client.post("/api/ask/compare3", json={"question": "   "})
    assert r.status_code == 422  # empty question rejected before the gate

    r = client.put("/api/self-iterate/toggle", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True

    r = client.get("/api/self-iterate/status")
    assert r.json()["enabled"] is True

    r = client.post("/api/ask/compare3", json=q)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("self_iterated", "with_ontology", "without_ontology", "value"):
        assert key in body, key
    si_side = body["self_iterated"]
    assert si_side["iterations"], "iteration trace present"
    assert si_side["initial_score"] <= si_side["final_score"]
    assert body["value"]["rounds"] == len(si_side["iterations"])
    # No model configured in the throwaway DB -> honest baseline placeholder.
    assert body["without_ontology"]["source"] in ("unavailable", "none", "ai")
    print(
        "  compare3 OK: si=%s rounds=%d, with=%s, without=%s"
        % (
            si_side["source"],
            len(si_side["iterations"]),
            body["with_ontology"]["source"],
            body["without_ontology"]["source"],
        )
    )

    r = client.get("/api/self-iterate/findings")
    assert r.status_code == 200
    findings = r.json()["findings"]
    assert findings, "alien question must leave ontology-gap findings"
    assert any(f["kind"] == "missing_rule" for f in findings)

    r = client.post("/api/self-iterate/harvest", json={})
    assert r.status_code == 200
    harvested = r.json()
    assert harvested["proposed"], harvested

    # End-to-end evolution: an admin approval evolves the physical pack.
    with get_conn() as conn:
        pid = harvested["proposed"][0]
        res = induction.approve_proposal(conn, "admin", pid)
        assert res["status"] == "approved"
        pack = svc.load_pack(conn, pack_id)
        keys = {n.get("key") for n in pack.get("nodes", [])}
        assert any(str(k).startswith("si_") for k in keys), keys

    stats = client.get("/api/self-iterate/status").json()["stats"]
    assert stats["sessions"] >= 1 and stats["findings_total"] >= 1
    print("  API + e2e evolution OK: pack gained a self-iteration node")


def main_entry() -> None:
    """Run every section in order (mirrors the other verify_* scripts)."""
    pack_id = _seed()
    print("self-iterate verification on temp DB:", _TMP)
    with get_conn() as conn:
        test_toggle(conn)
        test_session_and_findings(conn, pack_id)
        test_harvest_and_approve(conn, pack_id)
    test_critic()
    test_proposal_key()
    with get_conn() as conn:
        test_inner_loop_rules_single_round(conn, pack_id)
        test_inner_loop_refines(conn, pack_id)
        test_inner_loop_round_cap(conn, pack_id)
    test_api_flow(pack_id)
    print("verify_self_iterate: ALL PASS")


if __name__ == "__main__":
    main_entry()
