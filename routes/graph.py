"""HTTP routes for the graph area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import JSONResponse

from modules import auth
from modules import ontology_service as svc
from modules.db import get_conn

from routes.common import resolve_pack
from routes.common import user_pack_id
from modules.constants import GRAPH_SNIPPET_ATTRS
from modules.constants import GRAPH_SNIPPET_EDGES
from modules.constants import GRAPH_SNIPPET_METRICS
from modules.constants import GRAPH_SNIPPET_RELATIONS

router = APIRouter()


@router.get("/api/graph")
async def graph(user: Dict[str, Any] = Depends(auth.current_user)):
    """Impact graph plus narrative for the user's active pack.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"graph": {"nodes": [...], "links": [...]}, "narrative": str}``;
        empty graph when no pack is active.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"graph": {"nodes": [], "links": []}, "narrative": ""}
        pack = svc.load_pack(conn, pid)
    visual = svc.visual_graph(pack)
    return {"graph": visual, "narrative": svc.render_narrative(pack)}


@router.get("/api/class-graph")
async def class_graph(user: Dict[str, Any] = Depends(auth.current_user)):
    """Class-level relationship graph (entities + relations).

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"graph": {"nodes": [...], "links": [...]}}`` built from entities
        and relations rather than the curated graph nodes/edges.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"graph": {"nodes": [], "links": []}}
        pack = svc.load_pack(conn, pid)
    return {"graph": svc.class_graph(pack)}


@router.get("/api/graph/path")
async def graph_path(
    source: str,
    target: str,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Shortest directed path between two graph nodes (BFS).

    Args:
        source (str): Key of the start node.
        target (str): Key of the destination node.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"found": bool, "path": [...], "edge_ids": [...]}`` (plus a
        ``"reason"`` when no path exists or no pack is active).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {
                "found": False,
                "reason": "No active pack.",
                "path": [],
                "edge_ids": [],
            }
        pack = svc.load_pack(conn, pid)
    return svc.shortest_path(pack, source, target)


@router.get("/api/graph/impact")
async def graph_impact(
    node: str,
    hops: int = 2,
    direction: str = "downstream",
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """N-hop downstream / upstream impact analysis from a node.

    Args:
        node (str): Key of the starting node.
        hops (int): Maximum traversal depth (default 2).
        direction (str): ``"downstream"`` (default) or ``"upstream"``.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"found": bool, "nodes": [...], "edge_ids": [...]}`` (plus a
        ``"reason"`` when the node is unknown or no pack is active).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {
                "found": False,
                "reason": "No active pack.",
                "nodes": [],
                "edge_ids": [],
            }
        pack = svc.load_pack(conn, pid)
    return svc.impact_analysis(pack, node, hops=hops, direction=direction)


@router.get("/api/graph/cycles")
async def graph_cycles(user: Dict[str, Any] = Depends(auth.current_user)):
    """Directed cycles in the impact graph (should normally be zero).

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"count": int, "cycles": [...]}``.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"count": 0, "cycles": []}
        pack = svc.load_pack(conn, pid)
    return svc.detect_cycles(pack)


@router.get("/api/admin/visual-graph")
async def admin_visual_graph(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Impact graph (what users see on the dashboard) for any pack.

    Args:
        pack_id (int): Pack to visualise.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict | JSONResponse: ``{"graph": {"nodes", "links"}}``; 404 JSON when the
        pack does not exist.
    """
    with get_conn() as conn:
        pack = svc.load_pack(conn, pack_id)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    return {"graph": svc.visual_graph(pack)}


@router.get("/api/formulas/{formula_key}/impact")
async def formula_impact_endpoint(
    formula_key: str, user: Dict[str, Any] = Depends(auth.current_user)
):
    """What depends on a formula — downstream formulas, graph nodes, rules.

    Args:
        formula_key (str): Key of the formula to analyse.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: The impact payload; 404 JSON when no pack is active.
    """
    with get_conn() as conn:
        pack = svc.load_pack(conn)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    return svc.formula_impact(pack, formula_key)


@router.get("/api/search")
async def ontology_search(
    q: str,
    pack_id: Optional[int] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Full-text search across the active ontology.

    Searches entities, attributes, relations, formulas and rules, returning
    snippets plus the kind and key of each hit.

    Args:
        q (str): Free-text query.
        pack_id (Optional[int]): Search a specific pack instead of the caller's
            active one.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"results": [{"kind", "key", "snippet", ...}, ...]}``.
    """
    with get_conn() as conn:
        pack = (
            svc.load_pack(conn, pack_id)
            if pack_id is not None
            else resolve_pack(conn, user)
        )
    if pack is None:
        return {"results": []}
    return {"results": svc.search_ontology(pack, q)}


def _insight_analysis(
    conn,
    pack: Dict[str, Any],
    results: List[Dict[str, Any]],
    q: str,
) -> Optional[Dict[str, Any]]:
    """Build the human-readable analysis card for the top search match.

    Explains what it is, which tables back it, how it connects and which
    formulas use it.

    Args:
        conn: Open database connection (used for entity/table counts).
        pack (Dict[str, Any]): The active pack.
        results (List[Dict[str, Any]]): Search hits from ``svc.search_ontology``.
        q (str): The original query text.

    Returns:
        Optional[Dict[str, Any]]: ``{"kind", "key", "title", "description",
        "facts", "paragraph"}``, or ``None`` when there is nothing to explain.

    Example:
        >>> _insight_analysis(conn, pack, results, "APE")["kind"]
        'entity'
    """
    if not results:
        return None
    preferred = ("entity", "node", "formula")
    top = next((r for r in results if r["kind"] in preferred), results[0])
    kind, key = top["kind"], top["key"]
    ent = next((e for e in pack["entities"] if e["key"] == key), None)
    node = next((n for n in pack["nodes"] if n["key"] == key), None)
    if ent is None and node is None:
        # attribute match — describe the entity that owns it; relation match —
        # describe the relation's source entity
        if kind == "attribute":
            attr = next(
                (a for a in pack.get("attributes", []) if a.get("key") == key),
                None,
            )
            if attr is not None:
                ent = next(
                    (
                        e
                        for e in pack["entities"]
                        if e["key"] == attr.get("owned_class", "")
                    ),
                    None,
                )
        elif kind == "relation":
            rel = next(
                (r for r in pack.get("relations", []) if r.get("key") == key),
                None,
            )
            if rel is not None:
                ent = next(
                    (
                        e
                        for e in pack["entities"]
                        if e["key"] == rel.get("source_key", "")
                    ),
                    None,
                )
        elif kind == "formula":
            f = next((m for m in pack["formulas"] if m["key"] == key), None)
            if f is not None:
                facts = [
                    ["Expression", f.get("expression", "")],
                    ["Unit", f.get("unit", "") or "—"],
                ]
                return {
                    "kind": "formula",
                    "key": key,
                    "title": f.get("label") or key,
                    "description": "Derived metric formula in the active " "ontology.",
                    "facts": facts,
                    "paragraph": f"The formula **{f.get('label') or key}** "
                    f"computes `{f.get('expression', '')}`"
                    + (f" (unit: {f['unit']})." if f.get("unit") else ".")
                    + " It is evaluated from the pack's data tables at "
                    "question time by the rules engine.",
                }
        return None

    facts: List[List[str]] = []
    title = (ent or node).get("label_en") or (ent or node).get("label") or key
    desc = (ent or node).get("description", "")

    if ent is not None:
        ents = conn.execute(
            "SELECT COUNT(*) AS c FROM entities WHERE pack_id=?", (pack["id"],)
        ).fetchone()["c"]
        facts.append(["Entity", f"{ent['key']} ({ents} entities in pack)"])
        attrs = [a for a in pack["attributes"] if a.get("owned_class") == ent["key"]]
        if attrs:
            facts.append(
                [
                    "Attributes",
                    f"{len(attrs)}: "
                    + ", ".join(
                        (a.get("label_en") or a["key"])
                        for a in attrs[:GRAPH_SNIPPET_ATTRS]
                    )
                    + ("…" if len(attrs) > GRAPH_SNIPPET_ATTRS else ""),
                ]
            )
        # source tables backing this entity
        tbls = [t for t in pack["tables"] if t.get("entity_key") == ent["key"]]
        if tbls:
            names = []
            for t in tbls:
                ds = conn.execute(
                    "SELECT name, filename FROM datasets WHERE table_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (t["id"],),
                ).fetchone()
                src = ds["filename"] or ds["name"] if ds else "seed data"
                names.append(f"{t['label']} ({src})")
            facts.append(["Source tables", "; ".join(names)])
        rels = [
            r
            for r in pack["relations"]
            if r.get("source_key") == ent["key"] or r.get("target_key") == ent["key"]
        ]
        if rels:
            reltxt = []
            for r in rels[:GRAPH_SNIPPET_RELATIONS]:
                arrow = (
                    f"{r['source_key']} → {r['target_key']}"
                    if r.get("source_key") == ent["key"]
                    else f"← {r['source_key']}"
                )
                lbl = r.get("label_en") or ""
                reltxt.append(f"{lbl} ({arrow})" if lbl else arrow)
            facts.append(["Relations", f"{len(rels)}: " + "; ".join(reltxt)])
        formulas = [
            m
            for m in pack["formulas"]
            if ent["key"] in (m.get("expression") or "").lower()
            or ent["key"] in (m.get("key") or "").lower()
        ]
        if formulas:
            facts.append(
                [
                    "Formulas using it",
                    ", ".join(
                        (m.get("label") or m["key"])
                        for m in formulas[:GRAPH_SNIPPET_METRICS]
                    ),
                ]
            )
        paragraph = (
            f"**{title}** (`{ent['key']}`) is an entity in the active " f"ontology."
        )
        if tbls:
            paragraph += (
                f" Its data is stored in the knowledge-base table"
                f"{'s' if len(tbls) > 1 else ''} "
                + ", ".join(t["label"] for t in tbls)
                + " — search it in the Knowledge library and Data centre."
            )
        if rels:
            paragraph += (
                f" {len(rels)} relation"
                f"{'s' if len(rels) > 1 else ''} connect it to other "
                "entities, so questions about it resolve along those links."
            )
        if desc:
            paragraph += f" {desc}"
    else:
        outs = [e for e in pack["edges"] if e.get("source_key") == node["key"]]
        ins = [e for e in pack["edges"] if e.get("target_key") == node["key"]]
        facts.append(["Node", f"{node['key']} · group {node.get('grp', '')}"])
        if node.get("value"):
            facts.append(["Current value", node["value"]])
        if ins:
            facts.append(
                [
                    "Driven by",
                    "; ".join(
                        f"{e['source_key']} ({e.get('label') or e.get('edge_type', '')})"
                        for e in ins[:GRAPH_SNIPPET_EDGES]
                    ),
                ]
            )
        if outs:
            facts.append(
                [
                    "Drives",
                    "; ".join(
                        f"{e['target_key']} ({e.get('label') or e.get('edge_type', '')})"
                        for e in outs[:GRAPH_SNIPPET_EDGES]
                    ),
                ]
            )
        paragraph = f"**{title}** is a KPI node in the impact graph."
        if node.get("value"):
            paragraph += f" Current value: **{node['value']}**."
        if ins or outs:
            paragraph += (
                f" It sits in the causal chain with {len(ins)} upstream "
                f"and {len(outs)} downstream links — click it in the "
                "dashboard graph to trace the full impact path."
            )
        if desc:
            paragraph += f" {desc}"
    return {
        "kind": "entity" if ent is not None else "node",
        "key": key,
        "title": title,
        "description": desc,
        "facts": facts,
        "paragraph": paragraph,
    }


@router.get("/api/search/insight")
async def ontology_search_insight(
    q: str,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Ontology search plus instant understanding.

    Returns matched chips, the affected graph neighbourhood (matched nodes and
    their 1-hop neighbours) and a generated text analysis for the top match.

    Args:
        q (str): Free-text query.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"results": [...], "node_keys": [str], "subgraph": {"nodes",
        "links"}, "analysis": {...} | None}``.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if pack is None:
            return {
                "results": [],
                "node_keys": [],
                "subgraph": {"nodes": [], "links": []},
                "analysis": None,
            }
        results = svc.search_ontology(pack, q)
        ql = q.strip().lower()
        node_keys: List[str] = []
        if ql:
            for n in pack["nodes"]:
                hay = " ".join(
                    [n.get("key", ""), n.get("label") or "", n.get("description") or ""]
                    + (n.get("aliases") or [])
                )
                if ql in hay.lower():
                    node_keys.append(n["key"])
            for e in pack["entities"]:
                if ql in ((e.get("key", "") + " " + (e.get("label_en") or "")).lower()):
                    node_keys.append(e["key"])
            node_keys = [
                k
                for k in dict.fromkeys(node_keys)
                if any(n["key"] == k for n in pack["nodes"])
            ]
        # 1-hop neighbourhood around the matched nodes
        keep = set(node_keys)
        for e in pack["edges"]:
            if e.get("source_key") in keep:
                keep.add(e.get("target_key"))
            if e.get("target_key") in keep:
                keep.add(e.get("source_key"))
        sub_nodes = [
            {
                "key": n["key"],
                "label": n.get("label") or n["key"],
                "grp": n.get("grp", ""),
                "value": n.get("value") or "",
                "is_core": n.get("is_core", 0),
            }
            for n in pack["nodes"]
            if n["key"] in keep
        ]
        sub_links = [
            {
                "key": e["key"],
                "source": e.get("source_key"),
                "target": e.get("target_key"),
                "label": e.get("label") or "",
                "edge_type": e.get("edge_type") or e.get("type") or "",
            }
            for e in pack["edges"]
            if e.get("source_key") in keep and e.get("target_key") in keep
        ]
        analysis = _insight_analysis(conn, pack, results, q)
    return {
        "results": results,
        "node_keys": node_keys,
        "subgraph": {"nodes": sub_nodes, "links": sub_links},
        "analysis": analysis,
    }
