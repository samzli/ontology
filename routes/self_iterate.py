"""HTTP routes for the self-iterating agent (dual-loop evolution).

Everything here is opt-in: the per-user toggle (default off) lives in
``app_settings`` and is read through :func:`modules.self_iterate.is_enabled`.
The three-way comparison endpoint powers the visualization panel that lets
Life and GI analysts compare, for one question:

* ``self_iterated`` — the inner-loop (answer → critique → refine) result;
* ``with_ontology`` — the original grounded pipeline;
* ``without_ontology`` — the no-ontology baseline.

The outer loop endpoints expose the findings the inner loop collected and
turn them into additive ontology proposals for admin review.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import ontology_service as svc
from modules import qa_service as qa
from modules import self_iterate as si
from modules.constants import MEMORY_ANSWER_CHARS
from modules.db import get_conn
from routes.common import (
    AskIn,
    _effective_model_cfg,
    _log_llm_call,
    logger,
    user_pack_id,
)

router = APIRouter()


class ToggleIn(BaseModel):
    """Request body for the self-iteration toggle.

    Attributes:
        enabled (bool): New per-user feature state.
    """

    enabled: bool


@router.get("/api/self-iterate/status")
async def self_iterate_status(user: Dict[str, Any] = Depends(auth.current_user)):
    """Report feature availability, the caller's toggle and evolution stats.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"available": bool, "enabled": bool, "stats": {...}}`` —
        ``available`` is true when a model is configured (the inner loop can
        then refine); the toggle defaults to off.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        enabled = si.is_enabled(conn, user["id"])
        model_cfg = _effective_model_cfg(conn, user["id"])
        stats = si.stats(conn, pid)
    return {
        "available": model_cfg is not None,
        "enabled": enabled,
        "stats": stats,
    }


@router.put("/api/self-iterate/toggle")
async def self_iterate_toggle(
    data: ToggleIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Flip the caller's self-iteration feature toggle.

    Args:
        data (ToggleIn): New state.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"enabled": bool}`` — the persisted state.
    """
    with get_conn() as conn:
        si.set_enabled(conn, user["id"], data.enabled)
        conn.execute(
            "INSERT INTO audit_log(ts, username, action, detail, ip) "
            "VALUES(?,?,?,?, '')",
            (
                adm.now(),
                user["username"],
                "self_iterate.toggle",
                f"enabled={bool(data.enabled)}",
            ),
        )
    return {"enabled": bool(data.enabled)}


@router.post("/api/ask/compare3")
async def ask_compare3(data: AskIn, user: Dict[str, Any] = Depends(auth.current_user)):
    """Answer one question three ways for the comparison panel.

    Runs the self-iterating inner loop, the original grounded pipeline and
    the no-ontology baseline for the same question, and records the session
    for the outer evolution loop. Requires the caller's self-iteration
    toggle to be on.

    Args:
        data (AskIn): Question, mode and optional conversation history.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``question``, ``self_iterated``,
        ``with_ontology``, ``without_ontology`` and ``value``; 403 JSON when
        the feature is off, 422 JSON for an empty question, 400 JSON when no
        pack is active.
    """
    question = data.question.strip()
    if not question:
        return JSONResponse(status_code=422, content={"error": "Question is required."})
    mode = data.mode if data.mode in ("auto", "rules", "ai") else "auto"

    with get_conn() as conn:
        if not si.is_enabled(conn, user["id"]):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Self-iteration is off. Enable it with the "
                    "⟳ Self-iterate switch in the header."
                },
            )
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid else None
        model_cfg = _effective_model_cfg(conn, user["id"])
        mem_rows = (
            conn.execute(
                "SELECT question, answer FROM ask_history WHERE user_id=? AND pack_id=? "
                "AND answer != '' ORDER BY id DESC LIMIT 4",
                (user["id"], pid),
            ).fetchall()
            if pid is not None
            else []
        )
    if not pack:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No active ontology pack — ask an administrator "
                "to create or activate one."
            },
        )

    memory = []
    for r in reversed(mem_rows):
        memory.append({"role": "user", "content": r["question"]})
        memory.append(
            {"role": "assistant", "content": (r["answer"] or "")[:MEMORY_ANSWER_CHARS]}
        )
    history = list(data.history or [])
    if memory:
        history = history + memory

    computed = svc.compute_pack(pack, strict=False)
    iterated, base = await asyncio.gather(
        asyncio.to_thread(
            si.run_inner_loop,
            question,
            mode,
            pack,
            computed,
            model_cfg,
            history=history or None,
            user_id=user["id"],
            logger=logger,
            log_call=_log_llm_call,
        ),
        asyncio.to_thread(
            qa.compare_answers,
            question,
            mode,
            pack,
            computed,
            model_cfg,
            user_id=user["id"],
            history=history or None,
            logger=logger,
            log_call=_log_llm_call,
        ),
    )

    with get_conn() as conn:
        session_id = si.record_session(
            conn,
            user_id=user["id"],
            pack_id=pid,
            question=question,
            result=iterated,
        )

    si_grounding = iterated.get("grounding") or {}
    base_grounding = base["with_ontology"].get("grounding") or {}
    return {
        "question": question,
        "self_iterated": {
            "label": "Self-iterated",
            "source": iterated["source"],
            "answer": iterated["answer"],
            "graph": iterated["graph"],
            "model": iterated.get("model"),
            "grounding": si_grounding,
            "analysis": iterated.get("analysis"),
            "iterations": iterated["iterations"],
            "initial_score": iterated["initial_score"],
            "final_score": iterated["final_score"],
            "improved": iterated["improved"],
        },
        "with_ontology": base["with_ontology"],
        "without_ontology": base["without_ontology"],
        "value": {
            "initial_score": iterated["initial_score"],
            "final_score": iterated["final_score"],
            "score_lift": round(iterated["final_score"] - iterated["initial_score"], 1),
            "rounds": len(iterated["iterations"]),
            "session_id": session_id,
            "si_chain_nodes": si_grounding.get("graph_nodes", 0),
            "si_chain_edges": si_grounding.get("graph_edges", 0),
            "si_metrics_used": si_grounding.get("metric_count", 0),
            "original_chain_nodes": base_grounding.get("graph_nodes", 0),
            "original_metrics_used": base_grounding.get("metric_count", 0),
            "baseline_chars": base["value"].get("baseline_chars", 0),
            "original_chars": len(base["with_ontology"].get("answer") or ""),
            "si_chars": len(iterated["answer"] or ""),
        },
    }


@router.get("/api/self-iterate/findings")
async def self_iterate_findings(
    status: str = "",
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """List the outer-loop findings of the caller's active pack.

    Args:
        status (str): Optional status filter (``open`` / ``proposed``).
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"findings": [...], "stats": {...}}``.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        findings = si.list_findings(conn, pid, status) if pid else []
        summary = si.stats(conn, pid)
    return {"findings": findings, "stats": summary}


@router.post("/api/self-iterate/harvest")
async def self_iterate_harvest(user: Dict[str, Any] = Depends(auth.current_user)):
    """Turn the active pack's open findings into ontology proposals.

    The proposals enter the existing review queue (status ``pending``) and
    are applied only when an administrator approves them — the outer loop
    never mutates the ontology on its own.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"proposed": [ids], "skipped": int,
        "total_open": int}``; 400 JSON when no pack is active, 403 JSON when
        the feature is off.
    """
    with get_conn() as conn:
        if not si.is_enabled(conn, user["id"]):
            return JSONResponse(
                status_code=403,
                content={"error": "Self-iteration is off for your account."},
            )
        pid = user_pack_id(conn, user)
        if pid is None:
            return JSONResponse(status_code=400, content={"error": "No active pack."})
        result = si.harvest(conn, pid, user["username"])
        conn.execute(
            "INSERT INTO audit_log(ts, username, action, detail, ip) "
            "VALUES(?,?,?,?, '')",
            (
                adm.now(),
                user["username"],
                "self_iterate.harvest",
                f"pack={pid} proposed={len(result['proposed'])} "
                f"skipped={result['skipped']}",
            ),
        )
    return result
