"""HTTP routes for the ask area."""

from __future__ import annotations

import asyncio
import json

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import llm_client as llm
from modules import ontology_service as svc
from modules import qa_service as qa
from modules.constants import AUDIT_ANSWER_CHARS
from modules.constants import AUDIT_QUESTION_CHARS
from modules.constants import MEMORY_ANSWER_CHARS
from modules.db import get_conn
from routes.common import logger

from routes.common import _effective_model_cfg
from routes.common import user_pack_id

router = APIRouter()


class AskIn(BaseModel):
    """Payload shared by the streaming and compare ask endpoints.

    Attributes:
        question (str): The natural-language question.
        mode (str): ``auto`` (default), ``rules`` or ``ai``.
        history (Optional[List[Dict[str, str]]]): Prior turns shaped as
            ``[{"role": "user"|"assistant", "content": "..."}]``.
    """

    question: str
    mode: str = "auto"  # auto | rules | ai
    history: Optional[List[Dict[str, str]]] = None


def _sse(obj: Dict[str, Any]) -> str:
    """Serialise a payload as a Server-Sent Events ``data:`` frame.

    Args:
        obj (Dict[str, Any]): Event payload to send.

    Returns:
        str: ``"data: <json>\\n\\n"`` ready to be streamed.

    Example:
        >>> _sse({"type": "token", "content": "Hi"})
        'data: {"type": "token", "content": "Hi"}\\n\\n'
    """
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _log_llm_call(**kwargs: Any) -> None:
    """Persist an LLM call record (callback used by the QA pipeline).

    Args:
        **kwargs (Any): Call fields forwarded verbatim to ``llm.log_call``.

    Returns:
        None: The record is written to the database; failures propagate.
    """
    with get_conn() as conn:
        llm.log_call(conn, **kwargs)


@router.post("/api/ask-stream")
async def ask_stream(data: AskIn, user: Dict[str, Any] = Depends(auth.current_user)):
    """Answer a question as a Server-Sent Events stream.

    Emits ``token`` frames while the answer is produced, then a ``done`` frame
    with the answering source, match flag and token count. The completed turn
    is persisted so follow-up questions and the history panel keep context.

    Args:
        data (AskIn): Question, mode and optional conversation history.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        StreamingResponse: ``text/event-stream`` frames; a single ``error``
        frame when no pack is active.
    """
    question = data.question.strip()
    mode = data.mode if data.mode in ("auto", "rules", "ai") else "auto"

    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid else None
        model_cfg = _effective_model_cfg(conn, user["id"])
        # Long/short-term memory: recent Q&A of this user against this pack
        # (server-side, survives page reloads) + their saved questions.
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

        async def _empty():
            """Yield a single SSE error frame when no ontology pack exists."""
            yield _sse(
                {
                    "type": "error",
                    "error": "No active ontology pack. "
                    "Ask an administrator to create or activate one.",
                }
            )

        return StreamingResponse(_empty(), media_type="text/event-stream")

    computed = svc.compute_pack(pack, strict=False)

    # Server memory → conversation history (oldest first, after the
    # client-supplied session turns).
    memory: List[Dict[str, str]] = []
    for r in reversed(mem_rows):
        memory.append({"role": "user", "content": r["question"]})
        memory.append(
            {"role": "assistant", "content": (r["answer"] or "")[:MEMORY_ANSWER_CHARS]}
        )
    history = list(data.history or [])
    if memory:
        history = history + memory

    async def events():
        """Forward QA events as SSE frames, then persist the turn.

        Yields:
            str: ``data:`` frames for every event produced by
            ``qa.stream_answer``.
        """
        info = {"source": None, "matched": None, "tokens": 0}
        answer_excerpt = ""
        async for ev in qa.stream_answer(
            question,
            mode,
            pack,
            computed,
            model_cfg,
            history=history or None,
            user_id=user["id"],
            logger=logger,
            log_call=_log_llm_call,
        ):
            if ev.get("type") == "done":
                info["source"] = ev.get("source")
                info["matched"] = ev.get("matched")
                info["tokens"] = int(ev.get("tokens") or 0)
            elif ev.get("type") == "token":
                answer_excerpt += ev.get("content", "")
            yield _sse(ev)
        # Persist the turn so it can be recalled later (memory + history UI).
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO ask_history(user_id, pack_id, question, answer, "
                    "source, matched, tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        user["id"],
                        pid,
                        question[:AUDIT_QUESTION_CHARS],
                        answer_excerpt[:AUDIT_ANSWER_CHARS],
                        info["source"] or mode,
                        0 if info["matched"] is False else 1,
                        info["tokens"],
                        adm.now(),
                    ),
                )
        except Exception:
            pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/ask/compare")
async def ask_compare(data: AskIn, user: Dict[str, Any] = Depends(auth.current_user)):
    """Answer one question twice — with the ontology and without it.

    Lets a demo show, side by side, what the ontology grounding adds to both
    the answer and the impact chain.

    Args:
        data (AskIn): Question, mode and optional conversation history.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"with_ontology": {...}, "without_ontology":
        {...}}``; 422 JSON for an empty question, 400 JSON when no pack is
        active.
    """
    question = data.question.strip()
    if not question:
        return JSONResponse(status_code=422, content={"error": "Question is required."})
    mode = data.mode if data.mode in ("auto", "rules", "ai") else "auto"

    with get_conn() as conn:
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

    # Same conversation memory as /api/ask-stream so follow-up questions
    # ("…compared to the previous month?") resolve in Compare mode too.
    memory: List[Dict[str, str]] = []
    for r in reversed(mem_rows):
        memory.append({"role": "user", "content": r["question"]})
        memory.append(
            {"role": "assistant", "content": (r["answer"] or "")[:MEMORY_ANSWER_CHARS]}
        )
    history = list(data.history or [])
    if memory:
        history = history + memory

    computed = svc.compute_pack(pack, strict=False)
    result = await asyncio.to_thread(
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
    )
    # Keep the grounded side in the user's history so later questions retain
    # context; the non-ontology answer is deliberately not stored.
    grounded = result["with_ontology"]
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ask_history(user_id, pack_id, question, answer, "
                "source, matched, tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    pid,
                    question[:AUDIT_QUESTION_CHARS],
                    (grounded.get("answer") or "")[:AUDIT_ANSWER_CHARS],
                    grounded.get("source") or mode,
                    1 if grounded.get("source") in ("rules", "ai") else 0,
                    0,
                    adm.now(),
                ),
            )
    except Exception:
        pass
    return result


@router.get("/api/ask/history")
async def ask_history(user: Dict[str, Any] = Depends(auth.current_user)):
    """The caller's recent asks against the active pack (cross-session).

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"history": [{"id", "question", "source", "matched",
        "created_at"}, ...], "saved": [str, ...]}`` (up to 30 and 20 items).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"history": [], "saved": []}
        rows = conn.execute(
            "SELECT id, question, source, matched, created_at FROM ask_history "
            "WHERE user_id=? AND pack_id=? ORDER BY id DESC LIMIT 30",
            (user["id"], pid),
        ).fetchall()
        saved = conn.execute(
            "SELECT question FROM user_questions WHERE user_id=? "
            "AND (pack_id IS ? OR pack_id=?) ORDER BY id DESC LIMIT 20",
            (user["id"], pid, pid),
        ).fetchall()
    return {
        "history": [dict(r) for r in rows],
        "saved": [r["question"] for r in saved],
    }
