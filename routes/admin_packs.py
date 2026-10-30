"""HTTP routes for the admin packs area."""

from __future__ import annotations

import sqlite3

from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import importer as imp
from modules import induction
from modules import ontology_service as svc
from modules.db import get_conn

from routes.common import _err

router = APIRouter()


class PackIn(BaseModel):
    """Request body for creating a pack.

    Attributes:
        slug (str): URL-safe unique identifier.
        name (str): Display name.
        description (str): Optional free-text description.
    """

    slug: str
    name: str
    description: str = ""


class DuplicateIn(BaseModel):
    """Request body for cloning a pack.

    Attributes:
        slug (str): Slug for the new copy (must be unique).
        name (str): Display name for the new copy.
    """

    slug: str
    name: str


class ElementIn(BaseModel):
    """Request body for creating or updating one ontology element.

    Attributes:
        pack_id (int): Pack the element belongs to.
        payload (Dict[str, Any]): Element fields, e.g. entity/relation/formula
            keys and labels.
        expected_version (Optional[int]): Optimistic-locking guard; the write is
            rejected when the pack version has moved on.
    """

    pack_id: int
    payload: Dict[str, Any]
    expected_version: Optional[int] = None


class ImportPackIn(BaseModel):
    """Request body for importing a pack definition.

    Attributes:
        payload (Dict[str, Any]): Parsed pack JSON payload.
        slug (Optional[str]): Override slug used when importing.
        activate (bool): Whether to activate the imported pack immediately.
    """

    payload: Dict[str, Any]
    slug: Optional[str] = None
    activate: bool = False


class QuestionIn(BaseModel):
    """Request body for adding a question to a pack's bank.

    Attributes:
        pack_id (int): Pack that owns the question.
        question (str): Question text.
        category (str): Optional grouping label.
        source (str): Provenance label, default ``user``.
    """

    pack_id: int
    question: str
    category: str = ""
    source: str = "user"


@router.get("/api/admin/stats")
async def admin_stats(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Dashboard counters for the administrator console.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Counts and summaries produced by ``adm.dashboard_stats``.
    """
    with get_conn() as conn:
        return adm.dashboard_stats(conn)


@router.get("/api/admin/packs")
async def admin_packs(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Every pack plus the globally active one.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"packs": [...], "active_id": int | None}``.
    """
    with get_conn() as conn:
        return {"packs": adm.list_packs(conn), "active_id": adm.active_pack_id(conn)}


@router.post("/api/admin/packs")
async def admin_pack_create(
    data: PackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Create an empty pack and return its id.

    Args:
        data (PackIn): Slug, name and description of the new pack.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"id": int}``.
    """
    with get_conn() as conn:
        pid = adm.create_pack(
            conn,
            admin["username"],
            slug=data.slug,
            name=data.name,
            description=data.description,
        )
        return {"id": pid}


@router.post("/api/admin/packs/{pid}/activate")
async def admin_pack_activate(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Make a pack the globally active one for every user without an override.

    Args:
        pid (int): Pack id to activate.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.activate_pack(conn, admin["username"], pid)
    return {"ok": True}


@router.post("/api/admin/packs/{pid}/duplicate")
async def admin_pack_duplicate(
    pid: int, data: DuplicateIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Clone a pack (ontology and data) under a new slug and name.

    Args:
        pid (int): Source pack id.
        data (DuplicateIn): Slug and name for the copy.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"id": int}`` of the new pack.
    """
    with get_conn() as conn:
        new_id = adm.duplicate_pack(
            conn, admin["username"], pid, slug=data.slug, name=data.name
        )
    return {"id": new_id}


@router.post("/api/admin/packs/{pid}/reset")
async def admin_pack_reset(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Restore a built-in pack to its shipped contents.

    Args:
        pid (int): Pack id to reset.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        pack = adm.get_pack(conn, pid)
        adm.reset_builtin_pack(conn, admin["username"], pack["slug"])
    return {"ok": True}


@router.delete("/api/admin/packs/{pid}")
async def admin_pack_delete(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Delete a pack together with its ontology and data.

    Args:
        pid (int): Pack id to delete.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.delete_pack(conn, admin["username"], pid)
    return {"ok": True}


@router.get("/api/admin/packs/{pid}/export")
async def admin_pack_export(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Export a pack as a portable JSON payload.

    Args:
        pid (int): Pack id to export.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: The pack document (entities, attributes, relations, nodes, edges,
        formulas, rules and questions).
    """
    with get_conn() as conn:
        return adm.export_pack(conn, pid)


@router.post("/api/admin/packs/import")
async def admin_pack_import(
    data: ImportPackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Import a pack definition, recording the importing administrator.

    Args:
        data (ImportPackIn): Parsed pack payload plus optional slug override and
            activate flag.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"id": int}``; 4xx JSON when the payload is
        invalid, structurally broken or conflicts with existing records.
    """
    try:
        with get_conn() as conn:
            pid = adm.import_pack(
                conn,
                admin["username"],
                data.payload,
                slug_override=data.slug,
                activate=data.activate,
            )
            conn.execute(
                "UPDATE ontology_packs SET origin=? WHERE id=?",
                (f"Imported from JSON file by {admin['username']}", pid),
            )
    except (adm.AdminValueError, adm.AdminConflict) as e:
        return _err(e, e.status_code)
    except sqlite3.IntegrityError as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Pack data conflicts with existing records: {e}. "
                "Entity/attribute/relation keys must be unique within the pack."
            },
        )
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Invalid pack file structure: {e}. Please check the JSON format."
            },
        )
    return {"id": pid}


@router.post("/api/admin/packs/import/preview")
async def admin_pack_import_preview(
    data: ImportPackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Dry-run import — validate and summarise a pack file without writing.

    Args:
        data (ImportPackIn): Parsed pack payload and optional slug override.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: Summary of what would be imported; 422 JSON when
        the structure is invalid or conflicts with existing records.
    """
    try:
        with get_conn() as conn:
            return adm.preview_import(conn, data.payload, slug_override=data.slug)
    except (adm.AdminValueError, adm.AdminConflict) as e:
        return _err(e, e.status_code)
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Invalid pack file structure: {e}. Please check the JSON format."
            },
        )


@router.get("/api/admin/packs/{pid}/versions")
async def admin_pack_versions(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """List the saved version snapshots of a pack.

    Args:
        pid (int): Pack id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"versions": [...]}`` newest first.
    """
    with get_conn() as conn:
        return {"versions": adm.list_versions(conn, pid)}


@router.post("/api/admin/packs/{pid}/versions")
async def admin_pack_snapshot(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Create a manual version snapshot of a pack.

    Args:
        pid (int): Pack id to snapshot.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"id": int}`` of the new version record.
    """
    with get_conn() as conn:
        vid = adm.snapshot_pack(conn, admin["username"], pid, label="manual")
    return {"id": vid}


@router.post("/api/admin/packs/{pid}/versions/{version_id}/restore")
async def admin_pack_restore(
    pid: int, version_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Restore a pack to a previously snapshotted version.

    Args:
        pid (int): Pack id.
        version_id (int): Version record to restore.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.restore_version(conn, admin["username"], pid, version_id)
    return {"ok": True}


_ONTO_KINDS = {
    "entity": (adm.upsert_entity, adm.delete_entity),
    "relation": (adm.upsert_relation, adm.delete_relation),
    "attribute": (adm.upsert_attribute, adm.delete_attribute),
    "node": (adm.upsert_node, adm.delete_node),
    "edge": (adm.upsert_edge, adm.delete_edge),
    "formula": (adm.upsert_formula, adm.delete_formula),
    "rule": (adm.upsert_rule, adm.delete_rule),
}


@router.get("/api/admin/ontology")
async def admin_ontology(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Full ontology document (all element kinds) for one pack.

    Args:
        pack_id (int): Pack to load.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"pack": {...}, "entities", "attributes",
        "relations", "nodes", "edges", "formulas", "rules"}``; 404 JSON when the
        pack does not exist.
    """
    with get_conn() as conn:
        pack = svc.load_pack(conn, pack_id)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    return {
        "pack": {k: pack.get(k) for k in ("id", "slug", "name", "description")},
        "entities": pack.get("entities", []),
        "attributes": pack.get("attributes", []),
        "relations": pack.get("relations", []),
        "nodes": pack.get("nodes", []),
        "edges": pack.get("edges", []),
        "formulas": pack.get("formulas", []),
        "rules": pack.get("rules", []),
    }


@router.get("/api/admin/ontology/health")
async def admin_ontology_health(
    pack_id: Optional[int] = None,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Structural integrity report (orphan nodes, alias collisions, connectivity…).

    Args:
        pack_id (Optional[int]): Pack to inspect; defaults to the active pack.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: The health report augmented with ``pack_id`` and
        ``pack_name``; 404 JSON when no pack is active or the pack is missing.
    """
    with get_conn() as conn:
        pid = pack_id if pack_id is not None else adm.active_pack_id(conn)
        if pid is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        pack = svc.load_pack(conn, pid)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    health = svc.ontology_health(pack)
    health["pack_id"] = pack["id"]
    health["pack_name"] = pack["name"]
    return health


class SyncGraphIn(BaseModel):
    """Request body for rebuilding the curated graph.

    Attributes:
        pack_id (int): Pack whose graph nodes/edges are regenerated.
    """

    pack_id: int


@router.post("/api/admin/ontology/sync-graph")
async def admin_sync_graph(
    data: SyncGraphIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Rebuild the curated impact graph (graph_nodes/edges) from entities/relations.

    Keeps both graph layers corresponding.

    Args:
        data (SyncGraphIn): Pack to sync.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true, ...counts}`` of nodes and edges written.
    """
    with get_conn() as conn:
        try:
            counts = adm.sync_visual_graph(conn, admin["username"], data.pack_id)
            adm.bump_pack_version(conn, admin["username"], data.pack_id)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, e.status_code)
    return {"ok": True, **counts}


@router.post("/api/admin/elements/{kind}")
async def admin_upsert(
    kind: str, data: ElementIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Create or update one ontology element of the given kind.

    Args:
        kind (str): Element kind — ``entity``, ``relation``, ``attribute``,
            ``node``, ``edge``, ``formula`` or ``rule``.
        data (ElementIn): Pack id, element payload and optional expected version.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true}``; 404 JSON for an unknown kind,
        4xx JSON on validation/conflict errors.
    """
    if kind not in _ONTO_KINDS:
        return JSONResponse(status_code=404, content={"error": "Unknown element kind."})
    with get_conn() as conn:
        try:
            if data.expected_version is not None:
                adm.check_pack_version(conn, data.pack_id, data.expected_version)
            _ONTO_KINDS[kind][0](conn, admin["username"], data.pack_id, data.payload)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"ok": True}


@router.delete("/api/admin/elements/{kind}")
async def admin_delete(
    kind: str,
    pack_id: int,
    key: str,
    entity_key: str = "",
    expected_version: Optional[int] = None,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Delete one ontology element of the given kind.

    Args:
        kind (str): Element kind — ``entity``, ``relation``, ``attribute``,
            ``node``, ``edge``, ``formula`` or ``rule``.
        pack_id (int): Pack the element belongs to.
        key (str): Element key to delete.
        entity_key (str): Owning entity, required for ``attribute`` deletions.
        expected_version (Optional[int]): Optimistic-locking guard.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true}``; 404 JSON for an unknown kind,
        4xx JSON on validation/conflict errors.
    """
    if kind not in _ONTO_KINDS:
        return JSONResponse(status_code=404, content={"error": "Unknown element kind."})
    with get_conn() as conn:
        try:
            if expected_version is not None:
                adm.check_pack_version(conn, pack_id, expected_version)
            if kind == "attribute":
                _ONTO_KINDS[kind][1](conn, admin["username"], pack_id, entity_key, key)
            else:
                _ONTO_KINDS[kind][1](conn, admin["username"], pack_id, key)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"ok": True}


@router.get("/api/admin/proposals")
async def admin_proposals(
    pack_id: int,
    status: str = "pending",
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """List AI induction proposals for a pack.

    Args:
        pack_id (int): Pack to inspect.
        status (str): Filter by proposal status (default ``pending``).
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"proposals": [...], "pending": int}``.
    """
    with get_conn() as conn:
        return {
            "proposals": induction.list_proposals(conn, pack_id, status),
            "pending": induction.pending_count(conn, pack_id),
        }


@router.post("/api/admin/proposals/{proposal_id}/approve")
async def admin_proposal_approve(
    proposal_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Apply one proposed ontology change to the pack.

    Args:
        proposal_id (int): Proposal to approve.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Result of ``induction.approve_proposal``.
    """
    with get_conn() as conn:
        return induction.approve_proposal(conn, admin["username"], proposal_id)


@router.post("/api/admin/proposals/{proposal_id}/reject")
async def admin_proposal_reject(
    proposal_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Discard one proposed ontology change.

    Args:
        proposal_id (int): Proposal to reject.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Result of ``induction.reject_proposal``.
    """
    with get_conn() as conn:
        return induction.reject_proposal(conn, admin["username"], proposal_id)


@router.post("/api/admin/packs/{pack_id}/proposals/approve-all")
async def admin_proposals_approve_all(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Approve every pending proposal of a pack in one action.

    Args:
        pack_id (int): Pack whose proposals are approved.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Result of ``induction.approve_all`` (counts applied/skipped).
    """
    with get_conn() as conn:
        return induction.approve_all(conn, admin["username"], pack_id)


@router.get("/api/admin/questions")
async def admin_questions(
    pack_id: int,
    search: str = "",
    category: str = "",
    limit: int = 500,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Search a pack's question bank.

    Args:
        pack_id (int): Pack whose questions are listed.
        search (str): Optional text filter.
        category (str): Optional category filter.
        limit (int): Maximum number of rows (default 500).
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"questions": [{"id", "question", "category", ...}, ...]}``.
    """
    with get_conn() as conn:
        return {"questions": adm.list_questions(conn, pack_id, search, category, limit)}


@router.post("/api/admin/questions")
async def admin_question_add(
    data: QuestionIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Add a question to a pack's bank.

    Args:
        data (QuestionIn): Pack id, question text, category and source.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true}``; 409 JSON when the question
        already exists.
    """
    with get_conn() as conn:
        created = adm.add_question(
            conn,
            data.pack_id,
            data.question,
            data.category,
            data.source,
            admin["username"],
        )
    if not created:
        return JSONResponse(
            status_code=409,
            content={"error": "This question already exists in the bank."},
        )
    return {"ok": True}


@router.delete("/api/admin/questions/{qid}")
async def admin_question_delete(
    qid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Remove one question from the bank.

    Args:
        qid (int): Question id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        adm.delete_question(conn, admin["username"], qid)
    return {"ok": True}


@router.post("/api/admin/questions/import")
async def admin_question_import(
    pack_id: int = Form(...),
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Bulk-import questions from an uploaded CSV/XLSX/TXT file.

    Args:
        pack_id (int): Pack that receives the questions.
        file (UploadFile): Uploaded question file.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: Import summary (added/skipped counts).

    Raises:
        imp.UploadError: When the upload is invalid or badly formatted.
    """
    content = await file.read()
    imp.validate_upload(file.filename or "", len(content))
    items = imp.parse_question_file(file.filename, content)
    with get_conn() as conn:
        summary = adm.import_questions(conn, admin["username"], pack_id, items)
    return summary


@router.get("/api/admin/instance-graph")
async def admin_instance_graph(
    pack_id: int,
    search: str = "",
    entity_filter: str = "",
    node_limit: int = 300,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Instance-level graph of actual data rows for a pack.

    Args:
        pack_id (int): Pack to load.
        search (str): Optional text filter over node labels.
        entity_filter (str): Restrict to one entity key.
        node_limit (int): Maximum nodes returned (default 300).
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"nodes": [...], "links": [...]}`` from ``adm.instance_graph``.
    """
    with get_conn() as conn:
        return adm.instance_graph(
            conn,
            pack_id,
            node_limit=node_limit,
            entity_filter=entity_filter or None,
            search=search or None,
        )
