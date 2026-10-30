"""HTTP routes for the admin data area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import UploadFile
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import importer as imp
from modules import induction
from modules.db import get_conn
from routes.common import IdIn, logger

router = APIRouter()


class AnalyzeIn(BaseModel):
    """Request body for triggering ontology analysis on a table.

    Attributes:
        pack_id (int): Pack whose ontology may be extended.
    """

    pack_id: int


@router.get("/api/admin/data/tables")
async def admin_tables(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Knowledge-base tables of a pack.

    Args:
        pack_id (int): Pack whose tables are listed.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"tables": [{"id", "key", "label", ...}, ...]}``.
    """
    with get_conn() as conn:
        return {"tables": adm.list_tables(conn, pack_id)}


@router.get("/api/admin/data/datasets")
async def admin_datasets(
    table_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Datasets (uploaded versions) of a table.

    Args:
        table_id (int): Table whose datasets are listed.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"datasets": [{"id", "name", "filename", "row_count", ...}]}``.
    """
    with get_conn() as conn:
        return {"datasets": adm.list_datasets(conn, table_id)}


@router.post("/api/admin/data/datasets/{did}/activate")
async def admin_dataset_activate(
    did: int, body: IdIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Mark a dataset as the active version of its table.

    Args:
        did (int): Dataset id to activate.
        body (IdIn): Body whose ``value`` is the owning table id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.set_active_dataset(conn, admin["username"], int(body.value), did)
    return {"ok": True}


@router.delete("/api/admin/data/datasets/{did}")
async def admin_dataset_delete(
    did: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Delete a dataset and its stored rows.

    Args:
        did (int): Dataset id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.delete_dataset(conn, admin["username"], did)
    return {"ok": True}


@router.get("/api/admin/data/rows")
async def admin_rows(
    table_id: int,
    dataset_id: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Paginated rows of a table for the admin data browser.

    Args:
        table_id (int): Table to read.
        dataset_id (Optional[int]): Specific dataset; defaults to the active one.
        limit (int): Page size, clamped to 1-1000 (default 200).
        offset (int): Rows to skip, clamped to >= 0.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"columns", "rows", "total", "dataset_id"}``.
    """
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    with get_conn() as conn:
        columns = adm.table_columns(conn, table_id)
        result = adm.table_rows(conn, table_id, dataset_id, limit, offset)
    return {
        "columns": columns,
        "rows": result["rows"],
        "total": result["total"],
        "dataset_id": result["dataset_id"],
    }


@router.post("/api/admin/data/upload")
async def admin_upload(
    background_tasks: BackgroundTasks,
    pack_id: int = Form(...),
    table_id: int = Form(...),
    dataset_name: str = Form(""),
    commit: bool = Form(False),
    sheet: str = Form(""),
    mapping: str = Form(""),
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Upload a data file into one table (preview or commit).

    With ``commit=false`` the upload is parsed and a mapping preview is
    returned without writing. With ``commit=true`` the rows are stored as a new
    dataset and AI ontology induction is queued in the background.

    Args:
        background_tasks (BackgroundTasks): FastAPI hook for post-response work.
        pack_id (int): Pack that owns the table.
        table_id (int): Table that receives the rows.
        dataset_name (str): Optional label for the new dataset.
        commit (bool): Write the rows (``False`` previews only).
        sheet (str): Worksheet name for workbook uploads.
        mapping (str): Optional JSON mapping of file columns to table columns.
        file (UploadFile): The uploaded CSV/XLSX file.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Preview report, or the commit result ``{"dataset_id", ...}``.

    Raises:
        imp.UploadError: When the upload is invalid or badly formatted.
    """
    content = await file.read()
    imp.validate_upload(file.filename or "", len(content))
    mapping_override = imp.parse_mapping(mapping)
    if not commit:
        _, report = imp.preview_upload(
            table_id, file.filename, content, sheet, mapping_override
        )
        return report
    with get_conn() as conn:
        result = imp.commit_upload(
            conn,
            admin["username"],
            table_id,
            dataset_name,
            file.filename,
            content,
            sheet,
            mapping_override,
        )
    # AI ontology induction runs after the response so a slow model never
    # blocks the upload; failures are isolated inside the task.
    background_tasks.add_task(
        _run_induction_bg,
        pack_id,
        table_id,
        result.get("dataset_id"),
        admin["username"],
    )
    return result


@router.post("/api/admin/data/batch-upload")
async def admin_batch_upload(
    background_tasks: BackgroundTasks,
    pack_id: int = Form(...),
    commit: bool = Form(False),
    plan: str = Form(""),
    files: List[UploadFile] = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Upload many files / every sheet / a whole folder in one request.

    Preview (``commit=false``) returns a per-item routing + mapping report and
    writes nothing. Commit writes every valid item as its own dataset and
    creates any missing tables, each with a matching ontology entity and
    attributes. ``plan`` is an optional JSON list of per-item overrides
    ``[{file,item,target,mapping,skip}]``.

    Args:
        background_tasks (BackgroundTasks): FastAPI hook for post-response work.
        pack_id (int): Pack that receives the data.
        commit (bool): Write the data (``False`` analyses only).
        plan (str): Optional JSON list of per-item overrides.
        files (List[UploadFile]): The uploaded files.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Analysis report or commit result with per-item outcomes.

    Raises:
        imp.UploadError: When no file was uploaded.
    """
    payload = []
    for f in files:
        content = await f.read()
        payload.append({"filename": f.filename or "", "content": content})
    if not payload:
        raise imp.UploadError("No files were uploaded.")
    actor = admin["username"]
    with get_conn() as conn:
        if not commit:
            return imp.analyse_batch(conn, pack_id, payload, plan)
        result = imp.commit_batch(conn, actor, pack_id, payload, plan)
    # Keep parity with the single-file flow: enrich each new dataset in the
    # background so a slow model never blocks the response.
    for item in result["items"]:
        background_tasks.add_task(
            _run_induction_bg, pack_id, item["table_id"], item.get("dataset_id"), actor
        )
    return result


def _run_induction_bg(pack_id: int, table_id: int, dataset_id, actor: str) -> None:
    """Run ontology induction for a freshly uploaded dataset.

    Executed as a background task, so any failure is logged and swallowed
    instead of surfacing to the caller.

    Args:
        pack_id (int): Pack that receives the proposed ontology additions.
        table_id (int): Table that was uploaded.
        dataset_id (Any): The new dataset id (may be ``None``).
        actor (str): Username recorded against the proposals.

    Returns:
        None: Results are stored in the DB; exceptions are logged only.
    """
    try:
        with get_conn() as conn:
            induction.induce_from_table(
                conn, pack_id, table_id, dataset_id=dataset_id, actor=actor
            )
    except Exception:  # noqa: BLE001 — background task must never raise
        logger.exception("Background ontology induction failed")


@router.post("/api/admin/data/tables/{table_id}/analyze")
async def admin_table_analyze(
    table_id: int,
    data: AnalyzeIn,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Manually ask the AI to propose ontology additions for one table.

    Args:
        table_id (int): Table to analyse.
        data (AnalyzeIn): Body carrying the target pack id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Induction result (proposals created / skipped).
    """
    with get_conn() as conn:
        return induction.induce_from_table(
            conn, data.pack_id, table_id, actor=admin["username"]
        )
