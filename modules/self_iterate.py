"""Self-iterating agent — dual-loop ontology evolution engine.

Two cooperating loops improve answers *and* the ontology itself:

* **Inner loop** (per question, up to :data:`MAX_ROUNDS` rounds): the agent
  answers, then a deterministic critic scores the draft for ontology
  grounding (rule match, analysis depth, graph/metric evidence, substance).
  Below :data:`PASS_SCORE` the critique is injected into the prompt and the
  model re-answers; the best-scoring draft wins.
* **Outer loop** (across sessions, human-gated): every inner-loop session is
  persisted together with the ontology gaps it exposed (missing rules,
  coverage gaps, metric gaps). :func:`harvest` turns open findings into
  additive ontology proposals in the existing ``ontology_proposals`` review
  queue — an administrator approves them through the normal write path and
  the pack evolves.

The feature is opt-in per user (``self_iter:<user_id>`` setting, default
off). Nothing in this module runs unless a caller explicitly enables it and
invokes the engine.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from modules import deep_analysis as deep
from modules import llm_client as llm
from modules import ontology_service as svc
from modules import qa_service as qa
from modules.db import get_setting, jdumps, now, set_setting

TOGGLE_PREFIX = "self_iter:"
MAX_ROUNDS = 3
PASS_SCORE = 70.0
FINDING_KINDS = ("missing_rule", "coverage_gap", "metric_gap")
_MAX_FINDING_QUESTION_CHARS = 200


# ---------------------------------------------------------------------------
# Feature toggle (per user, default OFF)
# ---------------------------------------------------------------------------


def is_enabled(conn: sqlite3.Connection, user_id: int) -> bool:
    """Return whether the self-iteration feature is on for a user.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): User whose preference is read.

    Returns:
        bool: ``True`` only when the user explicitly enabled the feature;
        the default (key absent) is off.

    Example:
        >>> is_enabled(conn, 7)
        False
    """
    return get_setting(conn, f"{TOGGLE_PREFIX}{user_id}", "0") == "1"


def set_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    """Persist the per-user self-iteration toggle.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): User whose preference is written.
        enabled (bool): New toggle state.

    Example:
        >>> set_enabled(conn, 7, True)
    """
    set_setting(conn, f"{TOGGLE_PREFIX}{user_id}", "1" if enabled else "0")


# ---------------------------------------------------------------------------
# Inner loop — deterministic critic
# ---------------------------------------------------------------------------


def critique(
    answer: str,
    source: str,
    grounding: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Score one draft answer for ontology grounding and substance.

    The critic is fully deterministic — it needs no model, so the inner loop
    also works (and is unit-testable) without a configured LLM. Scores:

    * rule match +25, deep analysis +10, reasoning depth up to +10
    * graph evidence +10 (nodes) / +5 (edges), metrics +10, tables +5
    * substance (answer length) up to +15

    Args:
        answer (str): Draft answer text.
        source (str): Answering source (``rules`` / ``ai`` / ``none`` ...).
        grounding (Dict[str, Any]): Grounding summary from
            ``qa_service._grounding`` (counts of tables/metrics/graph).
        analysis (Dict[str, Any]): Result of ``deep.analyze``.

    Returns:
        Dict[str, Any]: ``score`` (0-100), ``passed`` (score >= PASS_SCORE),
        ``issues`` (machine-readable gap keys) and ``hints`` (human-readable
        refinement instructions for the next round).

    Example:
        >>> critique("", "none", {}, {})["score"]
        0.0
    """
    score = 0.0
    issues: List[str] = []
    hints: List[str] = []

    if source == "rules":
        score += 25
    else:
        issues.append("missing_rule")
        hints.append("Ground the answer in the ontology's canonical rules.")

    if analysis.get("available"):
        score += 10
        steps = int((analysis.get("depth") or {}).get("reasoning_steps") or 0)
        score += min(10.0, steps * 2.0)
        if steps < 3:
            issues.append("shallow_analysis")
            hints.append("Deepen the step-by-step reasoning over the data.")
    else:
        issues.append("shallow_analysis")
        hints.append("Add a data-driven analysis of the relevant tables.")

    if grounding.get("graph_nodes"):
        score += 10
        if grounding.get("graph_edges"):
            score += 5
    else:
        issues.append("coverage_gap")
        hints.append("Cite the ontology concepts and impact chain involved.")

    if grounding.get("metric_count"):
        score += 10
    else:
        issues.append("metric_gap")
        hints.append("Reference the pack's metric formulas and KPI values.")

    if grounding.get("table_count"):
        score += 5

    length = len(answer or "")
    if length >= 400:
        score += 15
    elif length >= 200:
        score += 10
        issues.append("thin_answer")
        hints.append("Expand the answer with concrete figures and drivers.")
    else:
        issues.append("thin_answer")
        hints.append("Write a fuller answer with concrete figures and drivers.")

    score = min(100.0, score)
    return {
        "score": round(score, 1),
        "passed": score >= PASS_SCORE,
        "issues": issues,
        "hints": hints,
    }


def _refine_prompt(base_prompt: str, draft: str, review: Dict[str, Any]) -> str:
    """Augment a system prompt with the critique of the previous draft.

    Args:
        base_prompt (str): The ontology-grounded system prompt.
        draft (str): The previous answer being improved.
        review (Dict[str, Any]): Critic output from :func:`critique`.

    Returns:
        str: System prompt that shows the draft, the critique and asks for a
        strictly better rewrite.

    Example:
        >>> "Critique" in _refine_prompt("base", "draft", {"issues": [], "hints": []})
        True
    """
    issues = ", ".join(review.get("issues") or []) or "none"
    hints = "\n".join(f"- {h}" for h in (review.get("hints") or [])) or "- none"
    return (
        f"{base_prompt}\n\n"
        "You are in a self-iteration loop. A previous draft answer is shown "
        "below with an automated critique. Rewrite it so every issue is "
        "fixed: ground every claim in the ontology context above, keep the "
        "correct figures, and follow the refinement hints.\n\n"
        f"Previous draft:\n{draft}\n\n"
        f"Critique (score {review.get('score')}/100, issues: {issues}):\n{hints}"
    )


# ---------------------------------------------------------------------------
# Inner loop — answer → critique → refine
# ---------------------------------------------------------------------------


def run_inner_loop(
    question: str,
    mode: str,
    pack: Dict[str, Any],
    computed: Dict[str, Any],
    model_cfg: Optional[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    user_id: Optional[int] = None,
    logger: Any = None,
    log_call: Optional[Callable[..., None]] = None,
    answer_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the self-iterating inner loop for one question.

    Round one mirrors the standard grounded pipeline (rules engine first,
    then the deep-analysis layer, then the model with the ontology context).
    The deterministic critic scores the draft; while it stays below
    :data:`PASS_SCORE` and a model is available, the critique is injected
    into the prompt and the model re-answers, up to :data:`MAX_ROUNDS`
    rounds. The best-scoring draft is returned.

    Args:
        question (str): Natural-language question.
        mode (str): ``"auto"``, ``"rules"`` or ``"ai"``.
        pack (Dict[str, Any]): Loaded ontology pack.
        computed (Dict[str, Any]): Pre-computed tables and metrics.
        model_cfg (Optional[Dict[str, Any]]): Resolved AI model config.
        history (Optional[List[Dict[str, str]]]): Prior chat turns.
        user_id (Optional[int]): Caller id recorded in the usage log.
        logger (Any): Optional logger.
        log_call (Optional[Callable[..., None]]): Usage sink compatible with
            ``llm_client.log_call``.
        answer_fn (Optional[Callable[..., Dict[str, Any]]]): Test seam taking
            ``(system_prompt, question, history)`` and returning an
            ``llm_client``-shaped result; defaults to a real
            ``ConfiguredLLM`` call.

    Returns:
        Dict[str, Any]: ``answer``, ``source``, ``graph``, ``grounding``,
        ``iterations`` (one entry per round with ``answer``, ``score`` and
        ``issues``), ``final_score``, ``initial_score`` and ``improved``.

    Example:
        >>> run_inner_loop("q", "auto", pack, computed, None)["iterations"]
        [...]
    """
    analysis = deep.analyze(pack, computed, question, history=history)

    if answer_fn is None and model_cfg is not None:
        client = llm.ConfiguredLLM(model_cfg, logger=logger)

        def answer_fn(system_prompt: str, q: str, hist: Any) -> Dict[str, Any]:
            res = client.answer(system_prompt, q, hist)
            if log_call:
                log_call(
                    user_id=user_id,
                    config_id=model_cfg.get("id"),
                    model=model_cfg.get("model", "?"),
                    success=res.get("status") == "success",
                    latency_ms=res.get("latency_ms", 0),
                    tokens=int(res.get("tokens_used") or 0),
                )
            return res

    hit = (
        qa.rule_answer(question, pack, computed, history=history)
        if (mode in ("auto", "rules"))
        else None
    )

    blocks: List[str] = []
    if hit:
        blocks.append(hit["answer"])
    if analysis.get("available") and analysis.get("body"):
        blocks.append(analysis["body"] if hit else analysis["full"])

    graph = None
    source = "none"
    draft = ""
    if blocks:
        draft = "\n\n".join(blocks)
        graph = svc.retrieve_subgraph(pack, question, draft)
        source = (
            "rules+analysis"
            if (hit and analysis.get("available"))
            else ("rules" if hit else "analysis")
        )
    elif answer_fn is not None:
        res = answer_fn(
            llm.build_system_prompt(svc.build_llm_context(pack)), question, history
        )
        if res.get("status") == "success":
            draft = res.get("response", "")
            graph = svc.retrieve_subgraph(pack, question, draft)
            source = "ai"
        else:
            draft = res.get("error", "Model error.")
            source = "error"
    else:
        draft = (
            "No ontology rule matched, and no AI model is configured to "
            "iterate on this question."
        )

    grounding = qa._grounding(computed, graph)
    review = critique(draft, "rules" if hit else source, grounding, analysis)
    iterations: List[Dict[str, Any]] = [
        {
            "round": 1,
            "answer": draft,
            "score": review["score"],
            "issues": review["issues"],
            "hints": review["hints"],
        }
    ]

    best = {
        "answer": draft,
        "score": review["score"],
        "graph": graph,
        "grounding": grounding,
    }
    base_prompt = (
        llm.build_system_prompt(svc.build_llm_context(pack))
        if answer_fn is not None
        else ""
    )
    round_no = 1
    while not review["passed"] and answer_fn is not None and round_no < MAX_ROUNDS:
        round_no += 1
        refined_prompt = _refine_prompt(base_prompt, best["answer"], review)
        res = answer_fn(refined_prompt, question, history)
        if res.get("status") != "success":
            break
        refined = res.get("response", "")
        rgraph = svc.retrieve_subgraph(pack, question, refined)
        rgrounding = qa._grounding(computed, rgraph)
        rsource = "rules+ai" if hit else "ai"
        review = critique(refined, "rules" if hit else "ai", rgrounding, analysis)
        iterations.append(
            {
                "round": round_no,
                "answer": refined,
                "score": review["score"],
                "issues": review["issues"],
                "hints": review["hints"],
            }
        )
        if review["score"] > best["score"]:
            best = {
                "answer": refined,
                "score": review["score"],
                "graph": rgraph,
                "grounding": rgrounding,
            }
            source = rsource

    initial_score = iterations[0]["score"]
    return {
        "answer": best["answer"],
        "source": source,
        "graph": best["graph"],
        "model": model_cfg.get("model") if model_cfg else None,
        "grounding": best["grounding"],
        "analysis": qa._analysis_payload(analysis),
        "iterations": iterations,
        "initial_score": initial_score,
        "final_score": best["score"],
        "improved": best["score"] > initial_score,
    }


# ---------------------------------------------------------------------------
# Outer loop — sessions, findings, harvest
# ---------------------------------------------------------------------------


def record_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    pack_id: Optional[int],
    question: str,
    result: Dict[str, Any],
) -> int:
    """Persist one inner-loop session and its ontology-gap findings.

    Gap-type issues (``missing_rule``, ``coverage_gap``, ``metric_gap``)
    become rows in ``self_iter_findings`` so the outer loop can aggregate
    them into evolution proposals; pure quality issues stay in the session
    payload only.

    Args:
        conn (sqlite3.Connection): Open database connection.
        user_id (int): Asking user.
        pack_id (Optional[int]): Pack the question ran against.
        question (str): The question text.
        result (Dict[str, Any]): Return value of :func:`run_inner_loop`.

    Returns:
        int: Id of the new ``self_iter_sessions`` row.

    Example:
        >>> record_session(conn, user_id=1, pack_id=2, question="q", result=res)
        1
    """
    payload = {
        "source": result.get("source"),
        "iterations": [
            {"round": it["round"], "score": it["score"], "issues": it["issues"]}
            for it in result.get("iterations", [])
        ],
    }
    cur = conn.execute(
        "INSERT INTO self_iter_sessions"
        "(ts, user_id, pack_id, question, iterations, initial_score,"
        " final_score, improved, payload) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            now(),
            user_id,
            pack_id,
            question[:500],
            len(result.get("iterations", [])),
            float(result.get("initial_score") or 0),
            float(result.get("final_score") or 0),
            1 if result.get("improved") else 0,
            jdumps(payload),
        ),
    )
    session_id = int(cur.lastrowid)
    if pack_id is not None:
        seen: set = set()
        for it in result.get("iterations", []):
            for issue in it.get("issues", []):
                if issue in FINDING_KINDS and issue not in seen:
                    seen.add(issue)
                    conn.execute(
                        "INSERT INTO self_iter_findings"
                        "(ts, pack_id, session_id, kind, detail, question,"
                        " status) VALUES(?,?,?,?,?,?, 'open')",
                        (
                            now(),
                            pack_id,
                            session_id,
                            issue,
                            f"Detected in round {it['round']} "
                            f"(score {it['score']}/100).",
                            question[:_MAX_FINDING_QUESTION_CHARS],
                        ),
                    )
    return session_id


def list_findings(
    conn: sqlite3.Connection, pack_id: int, status: str = "open"
) -> List[Dict[str, Any]]:
    """List self-iteration findings for a pack, newest first.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Pack whose findings are listed.
        status (str): Filter on status; ``""`` returns every status.

    Returns:
        List[Dict[str, Any]]: Finding rows as dictionaries.

    Example:
        >>> list_findings(conn, 4)[0]["kind"]
        'missing_rule'
    """
    sql = (
        "SELECT id, ts, kind, detail, question, status, proposal_id "
        "FROM self_iter_findings WHERE pack_id=?"
    )
    params: tuple = (pack_id,)
    if status:
        sql += " AND status=?"
        params = (pack_id, status)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", params)]


def _proposal_key(kind: str, question: str) -> str:
    """Derive a stable ontology key from a finding.

    Args:
        kind (str): Finding kind.
        question (str): Question that exposed the gap.

    Returns:
        str: ``si_<kind>_<slug>`` truncated to 60 characters.

    Example:
        >>> _proposal_key("metric_gap", "Why did VNB drop?")[:8]
        'si_metri'
    """
    words = re.findall(r"[a-z0-9]+", (question or "").lower())[:5]
    slug = "_".join(words) or "gap"
    return f"si_{kind}_{slug}"[:60]


def harvest(
    conn: sqlite3.Connection,
    pack_id: int,
    actor: str,
) -> Dict[str, Any]:
    """Convert open findings into additive ontology proposals.

    Each open finding becomes a pending proposal in the existing
    ``ontology_proposals`` review queue (kind ``node`` — always additive,
    applied through the normal write path on approval). Findings already
    covered by a pending proposal with the same key are skipped.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Pack whose open findings are harvested.
        actor (str): Username recorded as the proposer.

    Returns:
        Dict[str, Any]: ``proposed`` (new proposal ids), ``skipped`` (already
        covered findings) and ``total_open``.

    Example:
        >>> harvest(conn, 4, "admin")["total_open"]
        3
    """
    findings = list_findings(conn, pack_id, "open")
    proposed: List[int] = []
    skipped = 0
    grp_map = {
        "missing_rule": "knowledge",
        "coverage_gap": "analysis",
        "metric_gap": "metric",
    }
    label_map = {
        "missing_rule": "Missing rule",
        "coverage_gap": "Coverage gap",
        "metric_gap": "Metric gap",
    }
    for f in findings:
        key = _proposal_key(f["kind"], f["question"])
        dup = conn.execute(
            "SELECT id FROM ontology_proposals "
            "WHERE pack_id=? AND kind='node' AND key=? AND status='pending'",
            (pack_id, key),
        ).fetchone()
        exists = conn.execute(
            "SELECT id FROM graph_nodes WHERE pack_id=? AND key=?",
            (pack_id, key),
        ).fetchone()
        if dup is not None or exists is not None:
            skipped += 1
            conn.execute(
                "UPDATE self_iter_findings SET status='proposed', proposal_id=? "
                "WHERE id=?",
                (dup["id"] if dup is not None else None, f["id"]),
            )
            continue
        label = f"{label_map.get(f['kind'], 'Gap')}: {f['question'][:80]}"
        payload = {
            "key": key,
            "label": label,
            "grp": grp_map.get(f["kind"], "analysis"),
            "description": (
                "Self-iteration outer loop flagged an ontology gap. "
                f"Kind: {f['kind']}. Observed while answering: "
                f'"{f["question"]}". Approve to pin this concept into the '
                "ontology so future answers can ground in it."
            ),
            "aliases": [],
        }
        cur = conn.execute(
            "INSERT INTO ontology_proposals"
            "(ts, pack_id, table_id, dataset_id, kind, key, payload,"
            " rationale, evidence, status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
            (
                now(),
                pack_id,
                None,
                None,
                "node",
                key,
                jdumps(payload),
                f"Self-iteration finding #{f['id']} ({f['kind']}) — recurring "
                "gap across inner-loop sessions.",
                f["detail"],
            ),
        )
        conn.execute(
            "UPDATE self_iter_findings SET status='proposed', proposal_id=? WHERE id=?",
            (int(cur.lastrowid), f["id"]),
        )
        proposed.append(int(cur.lastrowid))
    return {
        "proposed": proposed,
        "skipped": skipped,
        "total_open": len(findings),
    }


def stats(conn: sqlite3.Connection, pack_id: Optional[int]) -> Dict[str, Any]:
    """Evolution metrics for the outer loop.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (Optional[int]): Scope to one pack; ``None`` aggregates all.

    Returns:
        Dict[str, Any]: Session count, average initial/final scores, share of
        sessions that improved, average rounds, and finding counts by status.

    Example:
        >>> stats(conn, 4)["sessions"]
        12
    """
    where = "WHERE pack_id=?" if pack_id is not None else ""
    params: tuple = (pack_id,) if pack_id is not None else ()
    row = conn.execute(
        "SELECT COUNT(*) n, AVG(initial_score) ai, AVG(final_score) af, "
        "AVG(improved) imp, AVG(iterations) rounds "
        f"FROM self_iter_sessions {where}",
        params,
    ).fetchone()
    fwhere = "WHERE pack_id=?" if pack_id is not None else ""
    fparams: tuple = (pack_id,) if pack_id is not None else ()
    findings = conn.execute(
        f"SELECT status, COUNT(*) n FROM self_iter_findings {fwhere} GROUP BY status",
        fparams,
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in findings}
    return {
        "sessions": row["n"] if row and row["n"] else 0,
        "avg_initial_score": round(row["ai"] or 0, 1) if row else 0.0,
        "avg_final_score": round(row["af"] or 0, 1) if row else 0.0,
        "improved_pct": round((row["imp"] or 0) * 100, 1) if row else 0.0,
        "avg_rounds": round(row["rounds"] or 0, 2) if row else 0.0,
        "findings_open": by_status.get("open", 0),
        "findings_proposed": by_status.get("proposed", 0),
        "findings_total": sum(by_status.values()),
    }
