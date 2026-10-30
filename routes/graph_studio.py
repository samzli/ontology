"""HTTP routes for the graph studio area."""

from __future__ import annotations

import json
import re

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import importer as imp
from modules import llm_client as llm
from modules import ontology_service as svc
from modules.constants import STUDIO_MAX_COLUMNS
from modules.constants import STUDIO_MAX_QUESTIONS
from modules.constants import STUDIO_RUN_DETAIL_CHARS
from modules.constants import STUDIO_RUN_TITLE_CHARS
from modules.constants import STUDIO_SAMPLE_CHARS
from modules.constants import STUDIO_SLUG_BASE
from modules.constants import STUDIO_SLUG_MAX
from modules import db as db_mod
from modules.db import audit
from modules.db import get_conn

from routes.common import _auto_pack_report
from routes.common import _err
from routes.common import resolve_pack

router = APIRouter()


class GraphBuildIn(BaseModel):
    """Request body for composing a studio graph.

    Attributes:
        view (str): Graph view — ``schema`` (default), ``lineage`` or
            ``columns``.
        table_ids (Optional[List[int]]): Tables to include; ``None`` means all.
        include_unmatched (bool): Keep nodes that match no ontology concept.
    """

    view: str = "schema"  # schema | lineage | columns
    table_ids: Optional[List[int]] = None
    include_unmatched: bool = True


@router.get("/api/graph-studio/sources")
async def graph_studio_sources(user: Dict[str, Any] = Depends(auth.current_user)):
    """Sources the user can pick from: ontology schema plus data tables.

    Each table carries its dataset provenance (which upload/file, and when).

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"tables": [{"id", "dataset_name", ...}], ...}``;
        404 JSON when no pack is active.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        ds = {}
        if pack is not None:
            pid = pack.get("id")
            if pid is not None:
                for r in conn.execute(
                    "SELECT table_id, name, filename, kind, row_count, created_at "
                    "FROM datasets WHERE pack_id=?",
                    (pid,),
                ).fetchall():
                    ds.setdefault(r["table_id"], []).append(dict(r))
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    out = svc.graph_studio_sources(pack)
    for t in out.get("tables", []):
        rows = ds.get(t["id"]) or []
        if rows:
            d = rows[-1]
            t["dataset_name"] = d["name"]
            t["dataset_file"] = d["filename"]
            t["dataset_kind"] = d["kind"]
            t["dataset_updated"] = d["created_at"]
        else:
            t["dataset_name"] = ""
            t["dataset_file"] = ""
            t["dataset_kind"] = ""
            t["dataset_updated"] = ""
    return out


def _ai_enrich_ontology(
    conn,
    pack: Dict[str, Any],
    payload: Dict[str, Any],
    table_ids: List[int],
) -> List[str]:
    """Optional AI pass over a freshly inferred ontology.

    Fills entity/attribute descriptions and proposes starter questions.
    Best-effort: returns ``[]`` when no model is configured or anything goes
    wrong.

    Args:
        conn: Open database connection (used to resolve the default model).
        pack (Dict[str, Any]): Source pack providing the tables.
        payload (Dict[str, Any]): Inferred ontology payload, enriched in place.
        table_ids (List[int]): Tables that were included in the inference.

    Returns:
        List[str]: Suggested business questions, or ``[]`` when unavailable.

    Example:
        >>> _ai_enrich_ontology(conn, pack, payload, [1, 2])
        ['What drove VNB growth last month?', ...]
    """
    try:
        model_cfg = llm.get_default_client_config(conn)
    except Exception:
        model_cfg = None
    if model_cfg is None:
        return []
    tables = [t for t in pack.get("tables", []) if t.get("id") in set(table_ids)]
    lines = []
    for t in tables:
        cols = ", ".join(
            f"{c['key']}({c.get('data_type', 'STRING')})"
            for c in (t.get("columns") or [])[:STUDIO_MAX_COLUMNS]
        )
        sample = (t.get("rows") or [{}])[0] if t.get("rows") else {}
        sample_s = json.dumps(sample, ensure_ascii=False, default=str)[
            :STUDIO_SAMPLE_CHARS
        ]
        lines.append(f"Table {t['key']} (columns: {cols}); sample row: {sample_s}")
    ents = "\n".join(
        f"- {e['key']}: {e.get('description', '')}" for e in payload.get("entities", [])
    )
    prompt = (
        "You are helping to document a freshly generated insurance ontology. "
        "Below are the source tables, the derived entities and their current "
        "auto-generated descriptions.\n\nSOURCE TABLES:\n"
        + "\n".join(lines)
        + "\n\nENTITIES:\n"
        + ents
        + '\n\nReturn STRICT JSON only (no markdown fence): {"entities": '
        '{"<entity_key>": "one-sentence business description"}, "attributes": '
        '{"<entity_key>.<attribute_key>": "short description"}, "questions": '
        "[5 example business questions answerable from these tables]}"
    )
    try:
        res = llm.ConfiguredLLM(model_cfg).answer(
            "You document insurance ontologies. Reply with STRICT JSON only.",
            prompt,
        )
        text = (res.get("response") or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        obj = json.loads(text)
        ent_desc = obj.get("entities") or {}
        attr_desc = obj.get("attributes") or {}
        for e in payload.get("entities", []):
            d = ent_desc.get(e["key"])
            if d and not (e.get("description") or "").strip():
                e["description"] = str(d)
        for a in payload.get("attributes", []):
            d = attr_desc.get(f"{a.get('entity_key')}.{a.get('key')}")
            if d and not (a.get("description") or "").strip():
                a["description"] = str(d)
        return [str(q) for q in (obj.get("questions") or [])][:STUDIO_MAX_QUESTIONS]
    except Exception:
        return []


@router.post("/api/graph-studio/build")
async def graph_studio_build(
    data: GraphBuildIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Compose one of the three compact analysis graphs from picked sources.

    Every node carries a provenance record.

    Args:
        data (GraphBuildIn): View, table selection and unmatched-node flag.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"nodes", "links", "sources_used"}``; 404 JSON
        when no pack is active.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    view = data.view if data.view in ("schema", "lineage", "columns") else "schema"
    return svc.build_studio_graph(
        pack,
        view=view,
        table_ids=data.table_ids,
        include_unmatched=data.include_unmatched,
    )


@router.post("/api/graph-studio/upload")
async def graph_studio_upload(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Build an ad-hoc graph from an uploaded data file or ontology JSON.

    Accepts CSV/XLSX (columns are matched against the active ontology) or an
    ontology JSON definition. Everything stays in memory — nothing is written
    to the database apart from the studio history entry.

    Args:
        file (UploadFile): The uploaded ``.csv``/``.xlsx``/``.json`` file.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"nodes", "links", "sources_used"}`` for the uploaded content.

    Raises:
        imp.UploadError: When the JSON is invalid or is not an ontology
            definition, or the data file cannot be parsed.
    """
    filename = file.filename or "upload"
    content = await file.read()
    with get_conn() as conn:
        pack = resolve_pack(conn, user)

    if filename.lower().endswith(".json"):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise imp.UploadError(f"Invalid JSON file: {e}", 422)
        if not isinstance(payload, dict) or not (
            payload.get("entities") or payload.get("classes")
        ):
            raise imp.UploadError(
                "JSON must be an ontology definition (with entities) — "
                "BP2 or onti-pack/v1 format.",
                422,
            )
        norm = adm._normalize_pack_payload(payload)
        virtual_pack = {
            "id": 0,
            "entities": norm.get("entities", []),
            "attributes": norm.get("attributes", []),
            "relations": norm.get("relations", []),
            "tables": [],
        }
        graph = svc.build_studio_graph(virtual_pack, view="schema")
        for n in graph["nodes"]:
            n["source"]["kind"] = "Uploaded ontology"
            n["source"]["detail"] = (
                "From uploaded file '" + filename + "': " + n["source"]["detail"]
            )
        graph["sources_used"] = sorted(
            {n["source"]["kind"] + ": " + n["source"]["detail"] for n in graph["nodes"]}
        )
        with get_conn() as conn:
            _record_studio(
                conn,
                user,
                pack.get("id") if pack else None,
                pack.get("name", "") if pack else "",
                "ontology-upload",
                f"Ontology graph from {filename}",
                f"Uploaded ontology JSON '{filename}' previewed against "
                f"{len(graph.get('nodes', []))} nodes",
            )
        return graph

    df = imp.parse_data_file(filename, content)
    sample = df.iloc[0].to_dict() if len(df) else {}
    graph = svc.graph_from_upload(
        pack, filename, [str(c) for c in df.columns], len(df), sample
    )
    with get_conn() as conn:
        _record_studio(
            conn,
            user,
            pack.get("id") if pack else None,
            pack.get("name", "") if pack else "",
            "data-upload",
            f"Graph from {filename}",
            f"CSV/XLSX '{filename}' — {len(df)} rows x {len(df.columns)} columns "
            "previewed against the active ontology",
        )
    return graph


class InferOntoIn(BaseModel):
    """Request body for previewing an inferred ontology.

    Attributes:
        table_ids (List[int]): Source tables to infer from.
        name (str): Optional pack name used in labels/history.
        slug (str): Optional slug; derived from ``name`` when blank.
        ai_enrich (bool): Whether to run the AI description/question pass.
    """

    table_ids: List[int]
    name: str = ""
    slug: str = ""
    ai_enrich: bool = False


class CreateOntoIn(BaseModel):
    """Request body for creating and activating an inferred ontology.

    Attributes:
        table_ids (List[int]): Source tables to infer from.
        name (str): Pack name (required).
        slug (str): Optional slug; derived from ``name`` when blank.
        ai_enrich (bool): Whether to run the AI description/question pass.
    """

    table_ids: List[int]
    name: str
    slug: str = ""
    ai_enrich: bool = False


@router.post("/api/graph-studio/infer")
async def graph_studio_infer(
    data: InferOntoIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Preview the ontology that would be generated from the picked tables.

    Returns entities, attributes, inferred relations and counts. Nothing is
    written to the database.

    Args:
        data (InferOntoIn): Table selection, optional name/slug and AI flag.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"counts", "entities", "relations",
        "ai_questions", "graph"}``; 400 JSON on an evaluation error, 404 JSON
        when no pack is active.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    ai_questions: List[str] = []
    try:
        payload = svc.infer_ontology(pack, data.table_ids, data.name, data.slug)
        if data.ai_enrich:
            with get_conn() as conn:
                ai_questions = _ai_enrich_ontology(conn, pack, payload, data.table_ids)
        with get_conn() as conn:
            _record_studio(
                conn,
                user,
                pack.get("id"),
                pack.get("name", ""),
                "preview",
                data.name.strip() or f"Preview ({len(data.table_ids)} tables)",
                f"Ontology preview from {len(data.table_ids)} table(s): "
                f"{len(payload['entities'])} entities, "
                f"{len(payload['relations'])} relations"
                + (" + AI enrichment" if data.ai_enrich else ""),
            )
    except svc.EvalError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    preview_pack = {
        "entities": payload["entities"],
        "attributes": payload["attributes"],
        "relations": payload["relations"],
        "tables": [],
    }
    graph = svc.build_studio_graph(preview_pack, view="schema")
    return {
        "counts": {
            "entities": len(payload["entities"]),
            "attributes": len(payload["attributes"]),
            "relations": len(payload["relations"]),
        },
        "entities": [
            {"key": e["key"], "label": e["label_en"], "description": e["description"]}
            for e in payload["entities"]
        ],
        "relations": [
            {
                "key": r["key"],
                "label": r["label_en"],
                "cardinality": r["cardinality"],
                "source": r["source_key"],
                "target": r["target_key"],
                "description": r["description"],
            }
            for r in payload["relations"]
        ],
        "ai_questions": ai_questions,
        "graph": graph,
    }


@router.post("/api/graph-studio/create-ontology")
async def graph_studio_create(
    data: CreateOntoIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Generate an ontology from picked tables, import it and activate it.

    The data is copied into the new pack so the user can immediately ask
    questions and build reports. A default analytics report is generated
    automatically and the action is written to the studio history.

    Args:
        data (CreateOntoIn): Table selection, pack name/slug and AI flag.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"pack_id", "slug", "name", "report"}``; 422
        JSON when the name is blank, 400 JSON on evaluation/import errors, 404
        JSON when no pack is active.
    """
    name = data.name.strip()
    if not name:
        return JSONResponse(status_code=422, content={"error": "Name is required."})
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if pack is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        base = (
            re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:STUDIO_SLUG_BASE]
            or "ontology"
        )
        slug = base
        i = 2
        while conn.execute(
            "SELECT 1 FROM ontology_packs WHERE slug=?", (slug,)
        ).fetchone():
            slug = f"{base}-{i}"[:STUDIO_SLUG_MAX]
            i += 1
        try:
            payload = svc.infer_ontology(pack, data.table_ids, name, slug)
            if data.ai_enrich:
                _ai_enrich_ontology(conn, pack, payload, data.table_ids)
            pid = adm.import_pack(conn, user["username"], payload, activate=True)
            # Provenance: record how this ontology was created and from what.
            # Table ids refer to the SOURCE pack — resolve names there.
            src_names = [
                r["label"] or r["key"]
                for r in conn.execute(
                    "SELECT key, label FROM pack_tables WHERE pack_id=? AND id IN "
                    f"({','.join('?' * len(data.table_ids))})",
                    (pack["id"], *data.table_ids),
                ).fetchall()
            ]
            origin = (
                f"Generated in Graph studio by {user['username']} from "
                f"{len(data.table_ids)} table(s): "
                + (", ".join(src_names) or "unknown")
                + (" + AI enrichment" if data.ai_enrich else "")
            )
            conn.execute("UPDATE ontology_packs SET origin=? WHERE id=?", (origin, pid))
            _record_studio(
                conn,
                user,
                pid,
                name,
                "ontology",
                f"{name} created from {len(data.table_ids)} table(s)",
                origin,
            )
            audit(
                conn,
                user["username"],
                "pack.infer_create",
                f"pack={pid} tables={data.table_ids}",
            )
            # The new pack is now active for this user: generate its default
            # report automatically (MD + HTML + PDF) so there is always a
            # consumable output right after creation.
            new_pack = svc.load_pack(conn, pid)
            report = None
            if new_pack is not None:
                try:
                    report = _auto_pack_report(conn, user, new_pack, f"{name} Report")
                    _record_studio(
                        conn,
                        user,
                        pid,
                        name,
                        "report",
                        f"Auto report for {name}",
                        report.get("uid", ""),
                    )
                except Exception:  # noqa: BLE001 - report must not block creation
                    report = None
        except svc.EvalError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"pack_id": pid, "slug": slug, "name": name, "report": report}


def _record_studio(
    conn,
    user: Dict[str, Any],
    pack_id: Optional[int],
    pack_name: str,
    action: str,
    title: str,
    detail: str,
) -> None:
    """Append an entry to the user's Graph studio generation history.

    Args:
        conn: Open database connection.
        user (Dict[str, Any]): Actor (needs ``id`` and ``username``).
        pack_id (Optional[int]): Related pack, or ``None``.
        pack_name (str): Related pack name (may be empty).
        action (str): Action label, e.g. ``preview``, ``ontology``, ``report``.
        title (str): Short title, truncated to ``STUDIO_RUN_TITLE_CHARS``.
        detail (str): Longer detail, truncated to ``STUDIO_RUN_DETAIL_CHARS``.

    Returns:
        None: The row is inserted; nothing is returned.

    Example:
        >>> _record_studio(conn, user, 3, "VNB", "preview", "Preview", "2 tables")
    """
    conn.execute(
        "INSERT INTO studio_history(user_id, username, pack_id, pack_name, "
        "action, title, detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            user["id"],
            user["username"],
            pack_id,
            pack_name,
            action,
            title[:STUDIO_RUN_TITLE_CHARS],
            (detail or "")[:STUDIO_RUN_DETAIL_CHARS],
            db_mod.now(),
        ),
    )


@router.get("/api/graph-studio/history")
async def graph_studio_history(user: Dict[str, Any] = Depends(auth.current_user)):
    """The caller's generation history (newest first).

    Covers graphs built from uploads, ontology previews/creations and
    auto-generated reports.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"history": [{"id", "pack_id", "pack_name", "action", "title",
        "detail", "created_at"}, ...]}`` (up to 50 entries).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, pack_id, pack_name, action, title, detail, created_at "
            "FROM studio_history WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}
