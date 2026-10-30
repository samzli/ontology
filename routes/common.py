"""Shared helpers used across the HTTP routers.

This module holds the small set of functions every router needs: pack
resolution for the signed-in user, data-source summaries, the automatic
analytics report, model-selection preferences, SSE helpers and the
generic JSON error normaliser. Routers must not import each other; anything
shared belongs here.
"""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

from fastapi.responses import JSONResponse

from common.logger import AppLogger
from pydantic import BaseModel

from modules import admin_service as adm
from modules import llm_client as llm
from modules import ontology_service as svc
from modules import report_builder as rb
from modules.db import get_setting
from modules.db import set_setting
from modules.constants import AUTO_REPORT_QUESTION_MIN
from modules.constants import AUTO_REPORT_QUESTION_TARGET
from modules.constants import SOURCE_SUMMARY_LIMIT

# Application-wide logger shared by every router.
logger = AppLogger("onti")


def _err(exc: Exception, status: int) -> JSONResponse:
    """Normalise an exception into a JSON ``{"error": ...}`` response.

    Args:
        exc (Exception): The exception to translate; its ``message`` attribute
            (or first argument) becomes the error text.
        status (int): HTTP status code to return.

    Returns:
        JSONResponse: Response whose body is ``{"error": "<detail>"}``.

    Example:
        >>> _err(ValueError("bad request"), 400)
        <JSONResponse status_code=400>
    """
    detail = getattr(exc, "message", None) or getattr(exc, "args", [str(exc)])[0]
    return JSONResponse(status_code=status, content={"error": detail})


class IdIn(BaseModel):
    """Generic request body carrying a single JSON value.

    Used by toggle-style endpoints (role, enabled flag, password reset, active
    dataset) so the payload stays ``{"value": <anything>}``.

    Attributes:
        value (Any): The raw value supplied by the client.
    """

    value: Any


def user_pack_id(conn, user: Dict[str, Any]) -> Optional[int]:
    """Resolve the pack this user is working in.

    The user's personal selection (persisted via the portal scenario dropdown)
    wins; otherwise the globally active pack is used.

    Args:
        conn: Open database connection.
        user (Dict[str, Any]): Current user record (must contain ``id``).

    Returns:
        Optional[int]: Id of the effective pack, or ``None`` when no pack exists.

    Example:
        >>> user_pack_id(conn, {"id": 7})
        3
    """
    raw = get_setting(conn, f"user_pack:{user['id']}", "")
    if raw:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            pid = None
        if (
            pid is not None
            and conn.execute(
                "SELECT 1 FROM ontology_packs WHERE id=?", (pid,)
            ).fetchone()
        ):
            return pid
    return adm.active_pack_id(conn)


def resolve_pack(conn, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Load the user's effective pack.

    Args:
        conn: Open database connection.
        user (Dict[str, Any]): Current user record.

    Returns:
        Optional[Dict[str, Any]]: The pack dict, or ``None`` when nothing exists.

    Example:
        >>> resolve_pack(conn, user)["name"]
        'VNB / APE demo'
    """
    pid = user_pack_id(conn, user)
    return svc.load_pack(conn, pid) if pid is not None else None


def pack_data_sources(conn, pid: Optional[int]) -> List[Dict[str, Any]]:
    """Dataset provenance records for the pack (what data it was built on).

    Args:
        conn: Open database connection.
        pid (Optional[int]): Pack id; ``None`` yields an empty list.

    Returns:
        List[Dict[str, Any]]: Rows with ``name``, ``filename``, ``kind``,
        ``row_count`` and ``created_at``, oldest first.

    Example:
        >>> pack_data_sources(conn, 3)[0]["filename"]
        'vnb_ape.xlsx'
    """
    if pid is None:
        return []
    return [
        dict(r)
        for r in conn.execute(
            "SELECT name, filename, kind, row_count, created_at "
            "FROM datasets WHERE pack_id=? ORDER BY id",
            (pid,),
        ).fetchall()
    ]


def _auto_pack_report(
    conn,
    user: Dict[str, Any],
    pack: Dict[str, Any],
    title: str,
) -> Dict[str, Any]:
    """Build the default analytics report for a pack.

    Recommended questions are answered by the rules engine, then rendered into
    MD/HTML/PDF. The title states the scenario (pack) and the data it is based
    on.

    Args:
        conn: Open database connection.
        user (Dict[str, Any]): Owner of the report (needs ``id``, ``username``).
        pack (Dict[str, Any]): Pack to analyse and report on.
        title (str): Base title; scenario and data provenance are appended.

    Returns:
        Dict[str, Any]: The stored report record (includes ``uid`` and title).

    Example:
        >>> _auto_pack_report(conn, user, pack, "Full Analytics Report")["uid"]
        'rep_ab12'
    """
    computed = svc.compute_pack(pack, strict=False)
    questions = svc.recommended_questions(pack, AUTO_REPORT_QUESTION_TARGET)
    if len(questions) < AUTO_REPORT_QUESTION_MIN:
        for r in conn.execute(
            "SELECT question FROM question_bank WHERE pack_id=? ORDER BY id",
            (pack["id"],),
        ).fetchall():
            if r["question"] not in questions:
                questions.append(r["question"])
            if len(questions) >= AUTO_REPORT_QUESTION_TARGET:
                break
    qa_items = []
    for q in questions:
        try:
            res = svc.answer_question(q, pack, computed, raise_on_template_error=False)
        except Exception:
            res = None
        if res:
            qa_items.append(
                {
                    "question": q,
                    "answer": res["answer"],
                    "source": "rules",
                    "matched": True,
                    "latency_ms": 0,
                    "graph": res.get("nodes", []),
                }
            )
        else:
            qa_items.append(
                {
                    "question": q,
                    "answer": "_No rule matched — ask this in AI mode for a "
                    "model answer grounded in the same knowledge base._",
                    "source": "rules",
                    "matched": False,
                    "latency_ms": 0,
                }
            )
    sources = pack_data_sources(conn, pack["id"])
    data_note = (
        ", ".join(s["filename"] or s["name"] for s in sources[:SOURCE_SUMMARY_LIMIT])
        or "seed data"
    )
    if len(sources) > SOURCE_SUMMARY_LIMIT:
        data_note += f" (+{len(sources) - SOURCE_SUMMARY_LIMIT} more)"
    return rb.build_report(
        conn,
        owner_id=user["id"],
        title=f"{title} | Scenario: {pack['name']} | Data: {data_note}",
        scope="pack",
        sections=[
            "summary",
            "qa",
            "evidence",
            "kpi",
            "charts",
            "ontology",
            "analytics",
            "appendix",
        ],
        include_ai=False,
        qa_items=qa_items,
        pack_id=pack["id"],
        actor=user["username"],
    )


def _ensure_auto_report(
    conn,
    user: Dict[str, Any],
    pack: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Guarantee a full analytics report exists for this user + pack.

    The first dashboard visit generates one automatically (rules engine, no AI
    cost); later visits reuse it.

    Args:
        conn: Open database connection.
        user (Dict[str, Any]): Current user record.
        pack (Dict[str, Any]): The user's effective pack.

    Returns:
        Optional[Dict[str, Any]]: ``{"uid", "title", "created_at"}`` or ``None``
        when generation failed or the stored report vanished.

    Example:
        >>> _ensure_auto_report(conn, user, pack)["title"]
        'Full Analytics Report | Scenario: ...'
    """
    flag = f"auto_report:{user['id']}:{pack['id']}"
    uid = get_setting(conn, flag)
    if not uid:
        try:
            rep = _auto_pack_report(conn, user, pack, "Full Analytics Report")
            uid = rep.get("uid")
            if uid:
                set_setting(conn, flag, uid)
        except Exception:
            return None
    if not uid:
        return None
    row = conn.execute(
        "SELECT title, created_at FROM reports WHERE uid=?", (uid,)
    ).fetchone()
    if row is None:
        return None
    return {
        "uid": uid,
        "title": row["title"],
        "created_at": row["created_at"],
    }


def _user_model_pref(conn, user_id: int) -> Optional[str]:
    """Read the user's stored model preference.

    Args:
        conn: Open database connection.
        user_id (int): The user whose preference is read.

    Returns:
        Optional[str]: Raw preference such as ``"sys:12"`` / ``"my:3"``, or
        ``None`` when unset.

    Example:
        >>> _user_model_pref(conn, 7)
        'my:3'
    """
    raw = get_setting(conn, f"user_model:{user_id}", "")
    return raw or None


def _parse_model_pref(pref: Optional[str]) -> Optional[Tuple[str, int]]:
    """Parse a stored model preference into ``(kind, id)``.

    ``'sys:12'`` → ``('sys', 12)``; ``'my:3'`` → ``('my', 3)``; a plain
    ``'12'`` → ``('sys', 12)``.

    Args:
        pref (Optional[str]): Raw preference string.

    Returns:
        Optional[Tuple[str, int]]: Parsed pair, or ``None`` when invalid/empty.

    Example:
        >>> _parse_model_pref("my:3")
        ('my', 3)
    """
    if not pref:
        return None
    s = str(pref).strip()
    try:
        if ":" in s:
            kind, mid = s.split(":", 1)
            if kind in ("sys", "my"):
                return kind, int(mid)
        return "sys", int(s)
    except (TypeError, ValueError):
        return None


def _effective_model_cfg(conn, user_id: int) -> Optional[Dict[str, Any]]:
    """Resolve the model that answers this user.

    Their own pick (system or personal) is used if still available, otherwise
    the system default.

    Args:
        conn: Open database connection.
        user_id (int): The asking user.

    Returns:
        Optional[Dict[str, Any]]: Client config dict, or ``None`` when no model
        is configured.

    Example:
        >>> _effective_model_cfg(conn, 7)["model"]
        'gpt-4o-mini'
    """
    parsed = _parse_model_pref(_user_model_pref(conn, user_id))
    if parsed is not None:
        kind, mid = parsed
        cfg = (
            llm.get_user_client_config_by_id(conn, user_id, mid)
            if kind == "my"
            else llm.get_client_config_by_id(conn, mid)
        )
        if cfg is not None:
            return cfg
    return llm.get_default_client_config(conn)
