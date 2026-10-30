"""AI-assisted ontology induction.

After a dataset is committed (or when an administrator asks manually), the
configured LLM inspects a table's columns and sample rows and **proposes**
ontology additions:

* entities (business concepts the table represents),
* relations between entities (including foreign-key-like columns),
* graph nodes / directed edges (the causal / structural chain),
* metric formulas in the platform's safe expression language.

Nothing is applied automatically — every proposal is stored with
``status='pending'`` and an administrator reviews, then approves or rejects it
in the console. Approval validates the payload through the same
``admin_service.upsert_*`` functions used by the manual builder, so an invalid
proposal can never corrupt the ontology.

The module degrades gracefully: no configured model, a network error or an
unparseable response simply returns a ``skipped`` result and never blocks the
underlying data upload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from modules import admin_service as adm
from modules import llm_client as llm
from modules import ontology_service as svc
from modules.db import audit, get_setting, now

# Approval order: edges require nodes; relations require entities; formulas
# reference existing metric/table names.
KIND_ORDER = {"entity": 0, "node": 1, "relation": 2, "edge": 3, "formula": 4}
SUPPORTED_KINDS = tuple(KIND_ORDER)
SAMPLE_ROWS = 3

_UPSERTERS = {
    "entity": adm.upsert_entity,
    "relation": adm.upsert_relation,
    "node": adm.upsert_node,
    "edge": adm.upsert_edge,
    "formula": adm.upsert_formula,
}

SYSTEM_PROMPT = """You are a senior insurance/FP&A ontology architect. \
Given an existing ontology and one uploaded data table (columns + sample \
values), you propose ADDITIVE ontology elements that make the new data \
queryable through the graph.

Rules:
1. Output STRICT JSON only, no markdown, no commentary, matching this schema:
{"entities":[{"key","label_en","layer","description","rationale"}],
 "relations":[{"key","source_key","target_key","label_en","cardinality","description","rationale"}],
 "nodes":[{"key","label","grp","description","is_core","rationale"}],
 "edges":[{"key","source_key","target_key","edge_type","label","description","rationale"}],
 "formulas":[{"key","label","expression","unit","fmt","decimals","rationale"}]}
2. Only propose elements that are missing. Reuse the EXACT existing keys \
listed below when referencing something — never invent near-duplicates.
3. Keys: entities/nodes use PascalCase (e.g. TotalVNB); relations/edges \
PascalCase with an R/E prefix style; formulas snake_case.
4. Every edge endpoint must be either an existing node key or a node you \
define in this same response. edge_type is one of STRUCTURAL, OPERATIONAL, \
MIX, CROSS_LINK. grp is one of financial, operational, mix, cross.
5. Every relation endpoint must be an existing entity key or an entity you \
define here. cardinality is ONE_TO_ONE, ONE_TO_MANY or MANY_TO_MANY.
6. Formulas use ONLY the safe functions rows(), where(), lookup(), cell(), \
sum(), avg(), min(), max(), count(), wavg(), round(), abs(), money_k() and \
existing metric keys. No Python imports, no attribute access. Prefer few, \
high-confidence formulas; omit the section if unsure.
7. Be conservative: at most a handful of elements. Do not propose attributes \
(table columns already cover them). Give every element a clear business \
description. Empty arrays are acceptable.
8. Every element MUST carry a short `rationale` naming the specific \
table column(s) and/or sample value(s) that justify it — the administrator \
sees this evidence and decides whether to approve or reject."""


# --------------------------------------------------------------------------- #
# Context gathering
# --------------------------------------------------------------------------- #


def _table_meta(conn, table_id: int) -> Optional[Dict[str, Any]]:
    """Load one uploaded table with its ordered column definitions.

    Args:
        conn (Any): Open database connection.
        table_id (int): Id in ``pack_tables``.

    Returns:
        Optional[Dict[str, Any]]: ``{"table": ..., "columns": [...]}``, or
        ``None`` when the table does not exist.

    Example:
        >>> _table_meta(conn, 12)["table"]["key"]
        'channel_monthly'
    """
    row = conn.execute("SELECT * FROM pack_tables WHERE id=?", (table_id,)).fetchone()
    if row is None:
        return None
    cols = [
        dict(c)
        for c in conn.execute(
            "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id",
            (table_id,),
        ).fetchall()
    ]
    return {"table": dict(row), "columns": cols}


def _ontology_context(pack: Dict[str, Any]) -> Dict[str, List[str]]:
    """Collect the existing ontology keys handed to the model.

    Args:
        pack (Dict[str, Any]): Loaded ontology pack.

    Returns:
        Dict[str, List[str]]: Key lists for entities, nodes, relations, edges,
        formulas and tables.

    Example:
        >>> _ontology_context(pack)["nodes"][:1]
        ['VNB']
    """
    return {
        "entities": [e["key"] for e in pack.get("entities", [])],
        "nodes": [n["key"] for n in pack.get("nodes", [])],
        "relations": [r["key"] for r in pack.get("relations", [])],
        "edges": [e["key"] for e in pack.get("edges", [])],
        "formulas": [f["key"] for f in pack.get("formulas", [])],
        "tables": [t["key"] for t in pack.get("tables", [])],
    }


def _build_user_prompt(
    pack: Dict[str, Any],
    meta: Dict[str, Any],
    sample_rows: List[Dict[str, Any]],
) -> str:
    """Render the user prompt for one table's induction pass.

    Args:
        pack (Dict[str, Any]): Loaded ontology pack.
        meta (Dict[str, Any]): Table and column metadata from ``_table_meta``.
        sample_rows (List[Dict[str, Any]]): Sample rows shown to the model.

    Returns:
        str: User prompt listing existing keys, columns and sample values.

    Example:
        >>> _build_user_prompt(pack, meta, rows).startswith("Pack:")
        True
    """
    ctx = _ontology_context(pack)
    tbl = meta["table"]
    col_lines = []
    for c in meta["columns"]:
        col_lines.append(
            f"- {c['key']} ({c.get('data_type', 'STRING')}) — {c.get('label', '')}"
        )
    sample = json.dumps(sample_rows[:SAMPLE_ROWS], ensure_ascii=False, default=str)
    return (
        f"Pack: {pack.get('name', '')}\n"
        f"Existing ontology keys (reuse, do not duplicate):\n"
        f"{json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"Uploaded table: key={tbl.get('key')} label={tbl.get('label')}\n"
        f"Columns:\n" + "\n".join(col_lines) + "\n\n"
        f"Sample rows:\n{sample}\n\n"
        f"Propose the additive ontology as strict JSON."
    )


# --------------------------------------------------------------------------- #
# Response parsing / normalisation
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def _parse_response(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse the model's JSON answer into normalised proposal lists.

    Args:
        text (str): Raw model response, possibly fenced in Markdown.

    Returns:
        Dict[str, List[Dict[str, Any]]]: One list per kind, with items that
        carry no ``key`` dropped.

    Raises:
        ValueError: If the text contains no JSON object.

    Example:
        >>> _parse_response('{"nodes": [{"key": "VNB"}]}')["node"]
        [{'key': 'VNB'}]
    """
    cleaned = _FENCE.sub("", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model returned no JSON object.")
    data = json.loads(cleaned[start : end + 1])
    out: Dict[str, List[Dict[str, Any]]] = {}
    plural = {
        "entities": "entity",
        "relations": "relation",
        "nodes": "node",
        "edges": "edge",
        "formulas": "formula",
    }
    for plural_key, kind in plural.items():
        items = data.get(plural_key) or []
        if isinstance(items, dict):
            items = [items]
        norm = []
        for it in items:
            if isinstance(it, dict) and it.get("key"):
                norm.append(it)
        out[kind] = norm
    return out


def _existing_keys(conn, pack_id: int) -> Dict[str, set]:
    """Load the ontology keys already present for a pack.

    Args:
        conn (Any): Open database connection.
        pack_id (int): Pack id to load.

    Returns:
        Dict[str, set]: Known keys per kind, used to skip duplicates.

    Example:
        >>> _existing_keys(conn, 4)["node"]
        {'VNB', 'APE'}
    """
    pack = svc.load_pack(conn, pack_id)
    return {
        "entity": {e["key"] for e in pack.get("entities", [])},
        "relation": {r["key"] for r in pack.get("relations", [])},
        "node": {n["key"] for n in pack.get("nodes", [])},
        "edge": {e["key"] for e in pack.get("edges", [])},
        "formula": {f["key"] for f in pack.get("formulas", [])},
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _build_evidence(
    meta: Dict[str, Any],
    sample_rows: List[Dict[str, Any]],
    dataset_id: Optional[int],
) -> Dict[str, Any]:
    """Snapshot what the model actually saw for one proposal batch.

    The evidence is stored beside each proposal so an administrator can judge
    whether to approve or reject it.

    Args:
        meta (Dict[str, Any]): Table and column metadata.
        sample_rows (List[Dict[str, Any]]): Sample rows shown to the model.
        dataset_id (Optional[int]): Dataset the rows came from, if any.

    Returns:
        Dict[str, Any]: Table identity, column descriptors and sample rows.

    Example:
        >>> _build_evidence(meta, rows, 9)["table"]
        'channel_monthly'
    """
    tbl = meta["table"]
    return {
        "table": tbl.get("key"),
        "table_label": tbl.get("label") or tbl.get("key"),
        "dataset_id": dataset_id,
        "columns": [
            {
                "key": c["key"],
                "label": c.get("label", ""),
                "data_type": c.get("data_type", "STRING"),
            }
            for c in meta["columns"]
        ],
        "sample_rows": sample_rows[:SAMPLE_ROWS],
    }


def _save_proposals(
    conn,
    pack_id: int,
    items: Dict[str, List[Dict[str, Any]]],
    *,
    table_id: Optional[int],
    dataset_id: Optional[int],
    evidence: Optional[Dict[str, Any]] = None,
) -> int:
    """Store new proposals as ``pending`` rows, skipping known duplicates.

    Args:
        conn (Any): Open database connection.
        pack_id (int): Pack the proposals belong to.
        items (Dict[str, List[Dict[str, Any]]]): Parsed proposals per kind.
        table_id (Optional[int]): Source table id.
        dataset_id (Optional[int]): Source dataset id.
        evidence (Optional[Dict[str, Any]]): Snapshot shown beside proposals.

    Returns:
        int: Number of rows inserted.

    Example:
        >>> _save_proposals(conn, 4, items, table_id=12, dataset_id=9,
        ...                 evidence=ev)
        3
    """
    existing = _existing_keys(conn, pack_id)
    pending = {
        row["kind"] + ":" + row["key"]
        for row in conn.execute(
            "SELECT kind, key FROM ontology_proposals WHERE pack_id=? AND status='pending'",
            (pack_id,),
        ).fetchall()
    }
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False)
    saved = 0
    for kind in SUPPORTED_KINDS:
        for payload in items.get(kind, []):
            p = dict(payload)
            key = str(p.pop("key", "")).strip()
            if not key or key in existing.get(kind, set()):
                continue
            dedup = f"{kind}:{key}"
            if dedup in pending:
                continue
            rationale = str(p.pop("rationale", ""))
            conn.execute(
                "INSERT INTO ontology_proposals(ts, pack_id, table_id, dataset_id, "
                "kind, key, payload, rationale, evidence, status) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    now(),
                    pack_id,
                    table_id,
                    dataset_id,
                    kind,
                    key,
                    json.dumps(p, ensure_ascii=False),
                    rationale,
                    evidence_json,
                ),
            )
            saved += 1
    return saved


def induce_from_table(
    conn,
    pack_id: int,
    table_id: int,
    *,
    dataset_id: Optional[int] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Run one induction pass for a table; never raises for model problems.

    Args:
        conn (Any): Open database connection.
        pack_id (int): Pack receiving the proposals.
        table_id (int): Uploaded table to inspect.
        dataset_id (Optional[int]): Dataset version to sample rows from.
        actor (str): Audit actor label.

    Returns:
        Dict[str, Any]: ``status`` of ``"ok"``, ``"skipped"`` or ``"error"``,
        a ``proposals`` count, and a ``reason`` when the status is not ``ok``.

    Example:
        >>> induce_from_table(conn, 4, 12)["status"]
        'ok'
    """
    model_cfg = llm.get_default_client_config(conn)
    if model_cfg is None:
        return {"status": "skipped", "reason": "no_model", "proposals": 0}
    if get_setting(conn, "ai_auto_propose", "1") != "1":
        return {"status": "skipped", "reason": "disabled", "proposals": 0}

    meta = _table_meta(conn, table_id)
    if meta is None:
        return {"status": "skipped", "reason": "table_not_found", "proposals": 0}
    rows_res = adm.table_rows(conn, table_id, dataset_id, SAMPLE_ROWS)
    sample = rows_res["rows"]

    pack = svc.load_pack(conn, pack_id)
    prompt = _build_user_prompt(pack, meta, sample)
    client = llm.ConfiguredLLM(model_cfg)
    resp = client.answer(SYSTEM_PROMPT, prompt, temperature=0.1, max_tokens=2200)
    if resp.get("status") != "success":
        return {
            "status": "error",
            "reason": str(resp.get("error", "model error"))[:300],
            "proposals": 0,
        }
    try:
        items = _parse_response(resp["response"])
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"unparseable: {e}"[:300], "proposals": 0}

    evidence = _build_evidence(meta, sample, dataset_id)
    saved = _save_proposals(
        conn,
        pack_id,
        items,
        table_id=table_id,
        dataset_id=dataset_id,
        evidence=evidence,
    )
    audit(
        conn,
        actor,
        "ai.proposed",
        f"pack={pack_id} table={table_id} proposals={saved}",
    )
    return {"status": "ok", "proposals": saved}


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #


def list_proposals(conn, pack_id: int, status: str = "pending") -> List[Dict[str, Any]]:
    """List proposals for a pack, newest first.

    Args:
        conn (Any): Open database connection.
        pack_id (int): Pack to read.
        status (str): Filter on proposal status; ``""`` returns all statuses.

    Returns:
        List[Dict[str, Any]]: Rows with ``payload`` and ``evidence`` decoded.

    Example:
        >>> len(list_proposals(conn, 4))
        3
    """
    sql = (
        "SELECT * FROM ontology_proposals WHERE pack_id=? "
        + ("AND status=? " if status else "")
        + "ORDER BY id DESC"
    )
    params: Tuple[Any, ...] = (pack_id, status) if status else (pack_id,)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        for field in ("payload", "evidence"):
            try:
                d[field] = json.loads(d[field] or "{}")
            except Exception:  # noqa: BLE001
                d[field] = {}
        out.append(d)
    return out


def pending_count(conn, pack_id: int) -> int:
    """Count the pending proposals of a pack.

    Args:
        conn (Any): Open database connection.
        pack_id (int): Pack to count.

    Returns:
        int: Number of rows still awaiting review.

    Example:
        >>> pending_count(conn, 4)
        3
    """
    return conn.execute(
        "SELECT COUNT(*) n FROM ontology_proposals WHERE pack_id=? AND status='pending'",
        (pack_id,),
    ).fetchone()["n"]


def _decide(conn, proposal_id: int, actor: str, status: str, result: str = "") -> None:
    """Write the review verdict of one proposal.

    Args:
        conn (Any): Open database connection.
        proposal_id (int): Proposal being decided.
        actor (str): Administrator recorded as ``decided_by``.
        status (str): New status, usually ``"approved"`` or ``"rejected"``.
        result (str): Optional failure detail stored with the verdict.

    Example:
        >>> _decide(conn, 21, "admin", "approved")
    """
    conn.execute(
        "UPDATE ontology_proposals SET status=?, decided_by=?, decided_at=?, result=? "
        "WHERE id=?",
        (status, actor, now(), result, proposal_id),
    )


def approve_proposal(conn, actor: str, proposal_id: int) -> Dict[str, Any]:
    """Apply one proposal through the normal admin upsert path.

    Args:
        conn (Any): Open database connection.
        actor (str): Administrator approving the proposal.
        proposal_id (int): Proposal to apply.

    Returns:
        Dict[str, Any]: ``id`` and the new ``status``.

    Raises:
        adm.AdminValueError: If the proposal is missing (404), already decided
            (409) or invalid for the ontology (422); invalid proposals are left
            marked rejected.

    Example:
        >>> approve_proposal(conn, "admin", 21)["status"]
        'approved'
    """
    row = conn.execute(
        "SELECT * FROM ontology_proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise adm.AdminValueError("Proposal not found.", 404)
    if row["status"] != "pending":
        raise adm.AdminValueError(f"Proposal already {row['status']}.", 409)
    payload = json.loads(row["payload"])
    payload["key"] = row["key"]
    try:
        _UPSERTERS[row["kind"]](conn, actor, row["pack_id"], payload)
    except Exception as e:  # noqa: BLE001 — surface why the proposal was invalid
        _decide(conn, proposal_id, actor, "rejected", f"Invalid: {e}"[:500])
        audit(conn, actor, "ai.proposal.rejected", f"id={proposal_id}: {e}")
        raise adm.AdminValueError(f"Proposal invalid, left rejected: {e}", 422)
    _decide(conn, proposal_id, actor, "approved")
    audit(
        conn,
        actor,
        "ai.proposal.approved",
        f"id={proposal_id} {row['kind']}:{row['key']}",
    )
    return {"id": proposal_id, "status": "approved"}


def reject_proposal(conn, actor: str, proposal_id: int) -> Dict[str, Any]:
    """Mark one proposal as rejected without touching the ontology.

    Args:
        conn (Any): Open database connection.
        actor (str): Administrator rejecting the proposal.
        proposal_id (int): Proposal to reject.

    Returns:
        Dict[str, Any]: ``id`` and the new ``status``.

    Raises:
        adm.AdminValueError: If the proposal does not exist (404).

    Example:
        >>> reject_proposal(conn, "admin", 21)["status"]
        'rejected'
    """
    row = conn.execute(
        "SELECT * FROM ontology_proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise adm.AdminValueError("Proposal not found.", 404)
    _decide(conn, proposal_id, actor, "rejected")
    audit(conn, actor, "ai.proposal.rejected", f"id={proposal_id}")
    return {"id": proposal_id, "status": "rejected"}


def approve_all(conn, actor: str, pack_id: int) -> Dict[str, Any]:
    """Approve every pending proposal in dependency order; skip invalid ones.

    Args:
        conn (Any): Open database connection.
        actor (str): Administrator approving the batch.
        pack_id (int): Pack whose pending proposals are processed.

    Returns:
        Dict[str, Any]: ``approved`` and ``rejected`` id lists.

    Example:
        >>> approve_all(conn, "admin", 4)["approved"]
        [21, 22]
    """
    rows = list_proposals(conn, pack_id, "pending")
    rows.sort(key=lambda r: KIND_ORDER.get(r["kind"], 99))
    approved, rejected = [], []
    for r in rows:
        try:
            approve_proposal(conn, actor, r["id"])
            approved.append(r["id"])
        except adm.AdminValueError:
            rejected.append(r["id"])
    return {"approved": approved, "rejected": rejected}
