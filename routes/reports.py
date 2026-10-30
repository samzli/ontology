"""HTTP routes for the reports area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import auth
from modules import report_builder as rb
from modules.db import get_conn

from routes.common import _auto_pack_report
from routes.common import resolve_pack

router = APIRouter()


class ReportIn(BaseModel):
    """Request body for building a report.

    Attributes:
        title (str): Report title; defaults to "Analytics Report" when blank.
        scope (str): Report scope, e.g. ``session`` (default) or ``pack``.
        sections (Optional[List[str]]): Section keys to render; ``None`` uses
            the full default set.
        include_ai (bool): Whether AI-generated narrative is added.
        qa_items (List[Dict[str, Any]]): Pre-computed Q&A pairs to embed.
        use_ontology (bool): ``False`` renders a text-only report without graph,
            evidence, chart or analytics sections.
    """

    title: str = ""
    scope: str = "session"
    sections: Optional[List[str]] = None
    include_ai: bool = False
    qa_items: List[Dict[str, Any]] = []
    use_ontology: bool = True


@router.post("/api/reports")
async def generate_report(
    data: ReportIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Build and store a report from caller-supplied sections and Q&A items.

    Args:
        data (ReportIn): Title, scope, sections and Q&A content.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"report": {"uid", "title", "created_at", ...}}``.
    """
    title = data.title.strip() or "Analytics Report"
    sections = data.sections
    if not data.use_ontology:
        # No-ontology mode: the report still renders, but as text only —
        # every section that needs the ontology (graph, evidence chains,
        # charts, analytics) is dropped rather than rendered empty.
        drop = {"evidence", "charts", "analytics", "ontology"}
        sections = [s for s in (sections or []) if s not in drop]
        # Never hand build_report an empty list: it would fall back to the
        # full default section set and re-introduce ontology visuals.
        sections = sections or ["summary"]
        title += " — No ontology (text-only)"
    with get_conn() as conn:
        rec = rb.build_report(
            conn,
            owner_id=user["id"],
            title=title,
            scope=data.scope,
            sections=sections,
            include_ai=data.include_ai,
            qa_items=data.qa_items,
            pack_id=None,
            actor=user["username"],
            text_only=not data.use_ontology,
        )
    return {"report": rec}


class SampleReportIn(BaseModel):
    """Request body for the one-click demo report.

    Attributes:
        title (str): Report title; a default is used when blank.
        include_ai (bool): Whether AI narrative is requested (the rules engine
            still answers the questions).
    """

    title: str = ""
    include_ai: bool = False


@router.post("/api/reports/sample")
async def sample_report(
    data: SampleReportIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """One-click demo report.

    Recommended questions are answered by the deterministic rules engine (no
    waiting on AI), then rendered into a full MD/HTML/PDF report with charts,
    ontology graph and analytics.

    Args:
        data (SampleReportIn): Title and AI flag.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"report": {...}}``; 400 JSON when no pack is
        active.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if not pack:
            return JSONResponse(status_code=400, content={"error": "No active pack."})
        rec = _auto_pack_report(
            conn, user, pack, data.title.strip() or "Sample Analytics Report"
        )
    return {"report": rec}


@router.get("/api/reports")
async def my_reports(user: Dict[str, Any] = Depends(auth.current_user)):
    """List the caller's reports.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"reports": [{"uid", "title", "created_at", ...}, ...]}``.
    """
    with get_conn() as conn:
        return {"reports": rb.list_reports(conn, user)}


@router.get("/api/reports/{uid}/download/{kind}")
async def download_report(
    uid: str, kind: str, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Download a generated report file.

    Args:
        uid (str): Report uid.
        kind (str): Format — ``md``, ``html`` or ``pdf``.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        FileResponse | JSONResponse: The file with the matching media type;
        404 JSON for an unknown format.
    """
    if kind not in ("md", "html", "pdf"):
        return JSONResponse(status_code=404, content={"error": "Unknown format."})
    with get_conn() as conn:
        path = rb.report_file(conn, uid, kind, user)
    media = {"md": "text/markdown", "html": "text/html", "pdf": "application/pdf"}[kind]
    return FileResponse(path, media_type=media, filename=f"{uid}.{kind}")


@router.get("/api/reports/{uid}/view")
async def view_report(uid: str, user: Dict[str, Any] = Depends(auth.current_user)):
    """Open the generated HTML report inline in the browser.

    Args:
        uid (str): Report uid.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        FileResponse: The HTML file rendered with ``text/html``.
    """
    with get_conn() as conn:
        path = rb.report_file(conn, uid, "html", user)
    return FileResponse(path, media_type="text/html")


@router.delete("/api/reports/{uid}")
async def remove_report(uid: str, user: Dict[str, Any] = Depends(auth.current_user)):
    """Delete a report owned by the caller (audited).

    Args:
        uid (str): Report uid.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        rb.delete_report(conn, uid, user, user["username"])
    return {"ok": True}
