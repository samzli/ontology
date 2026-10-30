"""HTTP routes for the config area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import config as cfg
from modules import llm_client as llm
from modules import ontology_service as svc
from modules.db import get_conn

from routes.common import user_pack_id

router = APIRouter()


@router.get("/api/config")
async def public_config(user: Optional[Dict[str, Any]] = Depends(auth.optional_user)):
    """Public branding and runtime configuration for the portal shell.

    Args:
        user (Optional[Dict[str, Any]]): Signed-in user (or ``None`` when
            anonymous), injected by the ``optional_user`` dependency.

    Returns:
        dict: ``{"system_name", "version", "modes": ["auto", "rules", "ai"],
        "pack": {"id", "name", "description"}, "model": {"configured", "name",
        "model"}}``.
    """
    with get_conn() as conn:
        # Resolve the effective pack: user's personal pick wins when logged in,
        # otherwise fall back to the globally active admin pack.
        if user is not None:
            pid = user_pack_id(conn, user)
        else:
            pid = adm.active_pack_id(conn)
        pack = adm.get_pack(conn, pid) if pid else None
        models = llm.list_configs(conn)
        default = next((m for m in models if m.get("is_default")), None)
    return {
        "system_name": cfg.SYSTEM_NAME,
        "version": cfg.VERSION,
        "modes": ["auto", "rules", "ai"],
        "pack": {
            "id": pid,
            "name": pack["name"] if pack else None,
            "description": pack.get("description", "") if pack else "",
        },
        "model": {
            "configured": bool(default),
            "name": default.get("name") if default else None,
            "model": default.get("model") if default else None,
        },
    }


@router.get("/api/questions")
async def questions(user: Dict[str, Any] = Depends(auth.current_user)):
    """Starter questions for the active pack's ask box.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"questions": [str, ...]}``.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        items = svc.suggested_questions(pid)
    return {"questions": items}


@router.get("/api/library")
async def library(user: Dict[str, Any] = Depends(auth.current_user)):
    """Read-only glossary + data inventory for the user portal.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"pack", "entities", "relations", "tables"}``; empty lists when
        the user has no active pack.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"pack": None, "entities": [], "relations": [], "tables": []}
        pack = svc.load_pack(conn, pid)
    return svc.library_payload(pack, pid)


@router.get("/api/data/rows")
async def user_data_rows(
    table_id: int,
    limit: int = 50,
    offset: int = 0,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Paginated rows of a table in the user's active pack.

    Powers the data centre's full-dataset browser (the library endpoint only
    carries a small preview of each table).

    Args:
        table_id (int): Table to browse; must belong to the active pack.
        limit (int): Page size, clamped to 1-500 (default 50).
        offset (int): Number of rows to skip, clamped to >= 0 (default 0).
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"columns", "rows", "total", "dataset_id"}``;
        404 JSON when there is no active pack or the table is not in it.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        t = conn.execute(
            "SELECT id FROM pack_tables WHERE id=? AND pack_id=?", (table_id, pid)
        ).fetchone()
        if t is None:
            return JSONResponse(
                status_code=404,
                content={"error": "Table not found in your active pack."},
            )
        columns = adm.table_columns(conn, table_id)
        result = adm.table_rows(conn, table_id, None, limit, offset)
    return {
        "columns": columns,
        "rows": result["rows"],
        "total": result["total"],
        "dataset_id": result["dataset_id"],
    }


@router.get("/api/recommended-questions")
async def recommended_questions(user: Dict[str, Any] = Depends(auth.current_user)):
    """Smart question suggestions derived from the active ontology.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"questions": [str, ...]}`` (empty when no pack is active).
    """
    with get_conn() as conn:
        pack = svc.load_pack(conn)
    if pack is None:
        return {"questions": []}
    return {"questions": svc.recommended_questions(pack)}


@router.get("/api/my-questions")
async def my_questions(user: Dict[str, Any] = Depends(auth.current_user)):
    """The caller's saved questions for the active pack (newest first).

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"questions": [{"id", "question", "created_at"}, ...]}``.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        rows = conn.execute(
            "SELECT id, question, created_at FROM user_questions "
            "WHERE user_id=? AND (pack_id IS ? OR pack_id=?) ORDER BY id DESC",
            (user["id"], pid, pid),
        ).fetchall()
    return {"questions": [dict(r) for r in rows]}


class MyQuestionIn(BaseModel):
    """Request body for saving a question.

    Attributes:
        question (str): The question text to store (trimmed server-side).
    """

    question: str


@router.post("/api/my-questions")
async def add_my_question(
    data: MyQuestionIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Save a question to the caller's personal list (deduplicated per user).

    Args:
        data (MyQuestionIn): The question to save.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"id": int}`` (plus ``"deduplicated": true`` when
        it already existed); 422 JSON for an empty question.
    """
    q = data.question.strip()
    if not q:
        return JSONResponse(status_code=422, content={"error": "Empty question."})
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        dup = conn.execute(
            "SELECT id FROM user_questions WHERE user_id=? AND question=?",
            (user["id"], q),
        ).fetchone()
        if dup:
            return {"id": dup["id"], "deduplicated": True}
        cur = conn.execute(
            "INSERT INTO user_questions(user_id, pack_id, question, created_at) "
            "VALUES(?,?,?,?)",
            (user["id"], pid, q, adm.now()),
        )
        return {"id": int(cur.lastrowid)}


@router.delete("/api/my-questions/{qid}")
async def delete_my_question(
    qid: int, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Delete one of the caller's saved questions.

    Args:
        qid (int): Question id; only rows owned by the caller are affected.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_questions WHERE id=? AND user_id=?",
            (qid, user["id"]),
        )
    return {"ok": True}
