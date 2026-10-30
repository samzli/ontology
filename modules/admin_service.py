"""Admin backend services: packs, ontology CRUD with referential integrity,
question bank, data tables/datasets, instance graph and dashboard stats.

All mutating functions take an ``actor`` username and write audit_log rows.
Integrity problems raise :class:`AdminConflict` (409) carrying a machine-
readable ``conflicts`` list; bad payloads raise :class:`AdminValueError`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from modules.db import audit, jloads, jdumps, now, set_setting
from modules import ontology_service as svc

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,48}$")


class AdminValueError(Exception):
    """Validation or not-found error raised by admin services.

    The API layer maps ``status_code`` onto the HTTP response.

    Attributes:
        message (str): Human-readable error description.
        status_code (int): HTTP status code to return.

    Example:
        >>> raise AdminValueError("Ontology pack not found.", 404)
    """

    def __init__(self, message: str, status_code: int = 422):
        """Initialize the error.

        Args:
            message (str): Human-readable error description.
            status_code (int): HTTP status code to return. Defaults to 422.

        Example:
            >>> AdminValueError("Pack name is required.").status_code
            422
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AdminConflict(Exception):
    """Integrity conflict (HTTP 409) raised when a change would break references.

    Attributes:
        message (str): Human-readable error description.
        status_code (int): Always 409.
        conflicts (List[Dict[str, Any]]): Machine-readable conflict records.

    Example:
        >>> raise AdminConflict("Entity 'policy' is in use.", [{"type": "attributes"}])
    """

    def __init__(self, message: str, conflicts: Optional[List[Dict[str, Any]]] = None):
        """Initialize the conflict.

        Args:
            message (str): Human-readable error description.
            conflicts (Optional[List[Dict[str, Any]]]): Conflict records to
                attach; ``None`` becomes an empty list.

        Example:
            >>> AdminConflict("duplicate", [{"type": "edge"}]).status_code
            409
        """
        super().__init__(message)
        self.message = message
        self.status_code = 409
        self.conflicts = conflicts or []


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------


def list_packs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """List every ontology pack with aggregate counts of its content.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        List[Dict[str, Any]]: One dict per pack holding the raw pack columns
            plus ``entities_n``, ``nodes_n``, ``edges_n``, ``rules_n``,
            ``questions_n`` and ``rows_n``, active pack first.

    Raises:
        sqlite3.DatabaseError: When the query fails.

    Example:
        >>> list_packs(conn)[0]["slug"]
        'vnb-ape-sample'
    """
    rows = conn.execute("""SELECT p.*,
                  (SELECT COUNT(*) FROM entities e WHERE e.pack_id=p.id) AS entities_n,
                  (SELECT COUNT(*) FROM graph_nodes g WHERE g.pack_id=p.id) AS nodes_n,
                  (SELECT COUNT(*) FROM graph_edges ge WHERE ge.pack_id=p.id) AS edges_n,
                  (SELECT COUNT(*) FROM question_rules qr WHERE qr.pack_id=p.id) AS rules_n,
                  (SELECT COUNT(*) FROM question_bank qb WHERE qb.pack_id=p.id) AS questions_n,
                  (SELECT COUNT(*) FROM pack_rows pr
                     JOIN datasets d ON d.id=pr.dataset_id WHERE d.pack_id=p.id) AS rows_n
             FROM ontology_packs p ORDER BY p.is_active DESC, p.id""").fetchall()
    return [dict(r) for r in rows]


def get_pack(conn: sqlite3.Connection, pack_id: int) -> Dict[str, Any]:
    """Fetch a single ontology pack by id.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        Dict[str, Any]: The pack row as a plain dict.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> get_pack(conn, 1)["name"]
        'VN BAPE Sample'
    """
    row = conn.execute("SELECT * FROM ontology_packs WHERE id=?", (pack_id,)).fetchone()
    if row is None:
        raise AdminValueError("Ontology pack not found.", 404)
    return dict(row)


def active_pack_id(conn: sqlite3.Connection) -> Optional[int]:
    """Return the id of the pack currently marked active.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        Optional[int]: Active pack id, or ``None`` when no pack is active.

    Raises:
        sqlite3.DatabaseError: When the query fails.

    Example:
        >>> active_pack_id(conn)
        1
    """
    row = conn.execute("SELECT id FROM ontology_packs WHERE is_active=1").fetchone()
    return row["id"] if row else None


def create_pack(
    conn: sqlite3.Connection, actor: str, *, slug: str, name: str, description: str = ""
) -> int:
    """Create an empty ontology pack.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        slug (str): Unique identifier (2-49 chars: lowercase letters, digits,
            ``-`` or ``_``).
        name (str): Human-readable pack name.
        description (str): Optional description. Defaults to "".

    Returns:
        int: Primary key of the new pack.

    Raises:
        AdminValueError: When the slug is malformed or the name is empty.
        AdminConflict: When the slug already exists.

    Example:
        >>> create_pack(conn, "admin", slug="demo-pack", name="Demo")
        7
    """
    slug = (slug or "").strip()
    name = (name or "").strip()
    if not SLUG_RE.match(slug):
        raise AdminValueError(
            "Slug must be 2-49 chars: lowercase letters, digits, '-' or '_'."
        )
    if not name:
        raise AdminValueError("Pack name is required.")
    if conn.execute("SELECT 1 FROM ontology_packs WHERE slug=?", (slug,)).fetchone():
        raise AdminConflict(
            f"Pack slug '{slug}' already exists.",
            [{"type": "duplicate_slug", "slug": slug}],
        )
    cur = conn.execute(
        "INSERT INTO ontology_packs(slug, name, description, is_active, created_at) "
        "VALUES(?,?,?,0,?)",
        (slug, name, description, now()),
    )
    pid = int(cur.lastrowid)
    audit(conn, actor, "pack.create", f"id={pid} slug={slug}")
    return pid


def bump_pack_version(
    conn: sqlite3.Connection,
    actor: str,
    pack_id: int,
    expected_version: Optional[int] = None,
) -> int:
    """Optimistically bump the pack version after a write.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        expected_version (Optional[int]): Version the caller last read, or
            ``None`` to skip the concurrency check.

    Returns:
        int: The new version number (current + 1).

    Raises:
        AdminValueError: When the pack does not exist.
        AdminConflict: When ``expected_version`` differs from the stored
            version, i.e. another session modified the pack meanwhile.

    Example:
        >>> bump_pack_version(conn, "admin", 1, expected_version=3)
        4
    """
    row = conn.execute(
        "SELECT version FROM ontology_packs WHERE id=?", (pack_id,)
    ).fetchone()
    if not row:
        raise AdminValueError(f"Pack {pack_id} not found.")
    current = row["version"]
    if expected_version is not None and expected_version != current:
        raise AdminConflict(
            f"Pack {pack_id} has been modified by another session "
            f"(expected v{expected_version}, current v{current}). "
            "Please refresh and try again.",
            [
                {
                    "type": "version_conflict",
                    "expected": expected_version,
                    "current": current,
                }
            ],
        )
    new_ver = current + 1
    conn.execute("UPDATE ontology_packs SET version=? WHERE id=?", (new_ver, pack_id))
    return new_ver


def check_pack_version(
    conn: sqlite3.Connection, pack_id: int, expected_version: int
) -> None:
    """Verify the pack version without modifying it.

    Check-only counterpart of :func:`bump_pack_version`: endpoints call this
    before a write, and the service-layer write function performs the actual
    bump.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.
        expected_version (int): Version the caller last read.

    Returns:
        None: Returns ``None`` when the stored version matches.

    Raises:
        AdminValueError: When the pack does not exist.
        AdminConflict: When the stored version differs from
            ``expected_version``.

    Example:
        >>> check_pack_version(conn, 1, 4)
    """
    row = conn.execute(
        "SELECT version FROM ontology_packs WHERE id=?", (pack_id,)
    ).fetchone()
    if not row:
        raise AdminValueError(f"Pack {pack_id} not found.")
    current = row["version"]
    if expected_version != current:
        raise AdminConflict(
            f"Pack {pack_id} has been modified by another session "
            f"(expected v{expected_version}, current v{current}). "
            "Please refresh and try again.",
            [
                {
                    "type": "version_conflict",
                    "expected": expected_version,
                    "current": current,
                }
            ],
        )


def activate_pack(conn: sqlite3.Connection, actor: str, pack_id: int) -> None:
    """Make the given pack the single active pack.

    Clears ``is_active`` on every pack, sets it on ``pack_id`` and mirrors the
    choice into ``app_settings.active_pack_id``.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack to activate.

    Returns:
        None: The activation is committed by the caller.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> activate_pack(conn, "admin", 1)
    """
    get_pack(conn, pack_id)
    conn.execute("UPDATE ontology_packs SET is_active=0")
    conn.execute("UPDATE ontology_packs SET is_active=1 WHERE id=?", (pack_id,))
    # Keep the app_settings mirror in sync so service-layer defaults (which
    # read app_settings.active_pack_id) agree with the is_active column.
    set_setting(conn, "active_pack_id", str(pack_id))
    audit(conn, actor, "pack.activate", f"id={pack_id}")


def delete_pack(conn: sqlite3.Connection, actor: str, pack_id: int) -> None:
    """Delete a pack together with the rows that cascade from it.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.
        AdminConflict: When the pack is currently active.

    Example:
        >>> delete_pack(conn, "admin", 5)
    """
    pack = get_pack(conn, pack_id)
    if pack["is_active"]:
        raise AdminConflict(
            "The active pack cannot be deleted; activate another pack first.",
            [{"type": "pack_active"}],
        )
    conn.execute("DELETE FROM ontology_packs WHERE id=?", (pack_id,))
    conn.execute("DELETE FROM app_settings WHERE key=?", (f"analysis:{pack_id}",))
    audit(conn, actor, "pack.delete", f"id={pack_id} slug={pack['slug']}")


def duplicate_pack(
    conn: sqlite3.Connection, actor: str, pack_id: int, new_slug: str, new_name: str
) -> int:
    """Clone a pack under a new slug and name via export/import.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the source pack.
        new_slug (str): Slug for the copy.
        new_name (str): Display name for the copy.

    Returns:
        int: Primary key of the new pack.

    Raises:
        AdminValueError: When the source pack is missing or its payload is
            invalid.
        AdminConflict: When ``new_slug`` already exists.

    Example:
        >>> duplicate_pack(conn, "admin", 1, "demo-copy", "Demo (copy)")
        8
    """
    get_pack(conn, pack_id)
    data = export_pack(conn, pack_id)
    data["slug"] = new_slug
    data["name"] = new_name
    pid = import_pack(conn, actor, data)
    audit(conn, actor, "pack.duplicate", f"src={pack_id} new={pid}")
    return pid


def sync_visual_graph(
    conn: sqlite3.Connection, actor: str, pack_id: int
) -> Dict[str, int]:
    """Rebuild ``graph_nodes`` / ``graph_edges`` from entities / relations.

    Makes the curated "impact graph" identical in structure to the schema
    layer, so the two graphs shown across the portals always correspond.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack to rebuild.

    Returns:
        Dict[str, int]: ``{"nodes": n, "edges": n}`` counts written.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> sync_visual_graph(conn, "admin", 1)
        {'nodes': 12, 'edges': 9}
    """
    get_pack(conn, pack_id)
    layer_grp = {1: "financial", 2: "financial", 3: "operational", 4: "mix", 5: "cross"}
    card_edge = {
        "ONE_TO_ONE": "STRUCTURAL",
        "ONE_TO_MANY": "STRUCTURAL",
        "MANY_TO_ONE": "STRUCTURAL",
        "MANY_TO_MANY": "CROSS_LINK",
    }
    entities = conn.execute(
        "SELECT * FROM entities WHERE pack_id=? ORDER BY sort,id", (pack_id,)
    ).fetchall()
    relations = conn.execute(
        "SELECT * FROM relations WHERE pack_id=? ORDER BY sort,id", (pack_id,)
    ).fetchall()
    conn.execute("DELETE FROM graph_edges WHERE pack_id=?", (pack_id,))
    conn.execute("DELETE FROM graph_nodes WHERE pack_id=?", (pack_id,))
    for i, e in enumerate(entities):
        conn.execute(
            "INSERT INTO graph_nodes(pack_id,key,label,grp,value,description,"
            "aliases,is_core,sort) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                e["key"],
                e["label_en"],
                layer_grp.get(e["layer"], "operational"),
                "",
                e["description"] or "",
                "[]",
                0,
                i,
            ),
        )
    for i, r in enumerate(relations):
        conn.execute(
            "INSERT INTO graph_edges(pack_id,key,source_key,target_key,edge_type,"
            "label,description,sort) VALUES(?,?,?,?,?,?,?,?)",
            (
                pack_id,
                r["key"],
                r["source_key"],
                r["target_key"],
                card_edge.get(r["cardinality"], "STRUCTURAL"),
                r["label_en"] or r["key"],
                r["description"] or "",
                i,
            ),
        )
    audit(
        conn,
        actor,
        "pack.sync_graph",
        f"id={pack_id} nodes={len(entities)} edges={len(relations)}",
    )
    return {"nodes": len(entities), "edges": len(relations)}


def reset_builtin_pack(
    conn: sqlite3.Connection, actor: str, slug: str = "vnb-ape-sample"
) -> None:
    """Delete the built-in pack and re-seed it from scratch.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        slug (str): Slug of the built-in pack. Defaults to "vnb-ape-sample".

    Returns:
        None: The reset is committed by the caller.

    Raises:
        sqlite3.DatabaseError: When the delete or the seed queries fail.

    Example:
        >>> reset_builtin_pack(conn, "admin")
    """
    row = conn.execute("SELECT id FROM ontology_packs WHERE slug=?", (slug,)).fetchone()
    if row:
        conn.execute("DELETE FROM ontology_packs WHERE id=?", (row["id"],))
    from modules.seed import ensure_seed

    ensure_seed(conn)
    audit(conn, actor, "pack.reset", f"slug={slug}")


# ----- export / import ------------------------------------------------------


def export_pack(conn: sqlite3.Connection, pack_id: int) -> Dict[str, Any]:
    """Serialise a pack into an ``onti-pack/v1`` JSON document.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        Dict[str, Any]: The pack document with ``entities``, ``attributes``,
            ``relations``, graph nodes and edges, metric formulas, question
            rules, tables (columns and dataset rows included), the question
            bank and the analysis spec.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> export_pack(conn, 1)["format"]
        'onti-pack/v1'
    """
    pack = get_pack(conn, pack_id)
    tables_out = []
    for t in conn.execute(
        "SELECT * FROM pack_tables WHERE pack_id=? ORDER BY id", (pack_id,)
    ):
        cols = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id",
                (t["id"],),
            )
        ]
        datasets_out = []
        for d in conn.execute(
            "SELECT * FROM datasets WHERE table_id=? ORDER BY id", (t["id"],)
        ):
            rows = [
                json.loads(r["data"])
                for r in conn.execute(
                    "SELECT data FROM pack_rows WHERE dataset_id=? ORDER BY seq,id",
                    (d["id"],),
                )
            ]
            dd = dict(d)
            dd.pop("id", None)
            dd.pop("table_id", None)
            dd["rows"] = rows
            datasets_out.append(dd)
        tt = {
            k: v
            for k, v in dict(t).items()
            if k not in ("id", "pack_id", "active_dataset_id")
        }
        tt["columns"] = [{k: v for k, v in c.items() if k != "table_id"} for c in cols]
        tt["datasets"] = datasets_out
        tables_out.append(tt)
    qb = [
        {"question": r["question"], "category": r["category"], "source": r["source"]}
        for r in conn.execute(
            "SELECT * FROM question_bank WHERE pack_id=? ORDER BY id", (pack_id,)
        )
    ]

    def dump(table: str, order: str = "sort,id", extra_skip=()) -> List[Dict[str, Any]]:
        """Collect one child table of the pack, dropping surrogate keys.

        Args:
            table (str): Table name to read.
            order (str): SQL ``ORDER BY`` expression. Defaults to "sort,id".
            extra_skip (tuple): Additional column names to drop. Defaults to
                ``()``.

        Returns:
            List[Dict[str, Any]]: Rows as plain dicts without ``id``,
                ``pack_id`` and the ``extra_skip`` columns.

        Raises:
            sqlite3.OperationalError: When the table name is invalid.

        Example:
            >>> dump("entities")[0]["key"]
            'policy'
        """
        skip = ("id", "pack_id") + extra_skip
        return [
            {k: v for k, v in dict(r).items() if k not in skip}
            for r in conn.execute(
                f"SELECT * FROM {table} WHERE pack_id=? ORDER BY {order}", (pack_id,)
            )
        ]

    spec_row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?", (f"analysis:{pack_id}",)
    ).fetchone()
    analysis = json.loads(spec_row["value"]) if spec_row else {}

    return {
        "format": "onti-pack/v1",
        "slug": pack["slug"],
        "name": pack["name"],
        "description": pack["description"],
        "narrative": pack["narrative"],
        "entities": dump("entities"),
        "attributes": dump("attributes"),
        "relations": dump("relations"),
        "graph_nodes": dump("graph_nodes"),
        "graph_edges": dump("graph_edges"),
        "metric_formulas": dump("metric_formulas"),
        "question_rules": dump("question_rules"),
        "tables": tables_out,
        "question_bank": qb,
        "analysis": analysis,
    }


def _require(obj: Dict[str, Any], keys: Tuple[str, ...], ctx: str) -> None:
    """Validate that a payload dict carries all required non-empty fields.

    Args:
        obj (Dict[str, Any]): Payload to inspect.
        keys (Tuple[str, ...]): Field names that must be present and non-empty.
        ctx (str): Context label used in the error message.

    Returns:
        None: Returns ``None`` when every field is present.

    Raises:
        AdminValueError: Listing the missing fields.

    Example:
        >>> _require({"key": "a", "label_en": "A"}, ("key", "label_en"), "entity")
    """
    missing = [k for k in keys if obj.get(k) in (None, "")]
    if missing:
        raise AdminValueError(f"{ctx}: missing fields {missing}")


def _normalize_pack_payload(data: Any) -> Dict[str, Any]:
    """Normalise any supported ontology JSON into the onti-pack/v1 layout.

    Accepts both ``onti-pack/v1`` exports and raw ontology JSON (BP2 format).
    The BP2 format uses different field names:

    * entity: ``id`` → ``key``
    * attribute: ``id`` → ``key``, ``owned_class`` → ``entity_key``,
      ``unique`` → ``is_unique``
    * relation: ``id`` → ``key``, ``source_class`` → ``source_key``,
      ``target_class`` → ``target_key``,
      ``source_class_attribute`` → ``source_attr``,
      ``target_class_attribute`` → ``target_attr``

    A file with an unknown ``format`` marker is still accepted when it
    contains recognizable ontology content, so hand-written packs do not
    fail just because of a stray marker field.

    Args:
        data (Any): Parsed JSON payload of the uploaded pack file.

    Returns:
        Dict[str, Any]: Payload converted to the onti-pack/v1 layout.

    Raises:
        AdminValueError: When the payload is not a dict, or the format marker
            is unknown and no ontology content is present.

    Example:
        >>> _normalize_pack_payload({"entities": [{"id": "Policy"}]})["format"]
        'onti-pack/v1'
    """
    if not isinstance(data, dict):
        raise AdminValueError(
            "Invalid pack file: expected a JSON object with "
            "'entities'/'attributes'/'relations' lists."
        )
    has_content = bool(
        data.get("entities")
        or data.get("attributes")
        or data.get("relations")
        or data.get("graph_nodes")
        or data.get("metric_formulas")
        or data.get("question_rules")
    )
    fmt = data.get("format")
    if fmt not in ("onti-pack/v1", None) and not has_content:
        raise AdminValueError(
            f"Unsupported pack file: unknown format marker '{fmt}'. Expected an "
            "onti-pack/v1 export or raw ontology definitions (a JSON object "
            "holding 'entities'/'attributes'/'relations' lists)."
        )
    # Detect BP2 format: has entities with 'id' instead of 'key'
    ents = data.get("entities") or []
    if ents and isinstance(ents[0], dict) and "id" in ents[0] and "key" not in ents[0]:
        return _convert_bp2_to_v1(data)
    # Detect relationship-graph style: entities as plain strings and/or a
    # 'relationships' list (from/to/type), e.g. the lapse_demo.json fixture.
    if (ents and isinstance(ents[0], str)) or data.get("relationships"):
        return _convert_relationship_graph_to_v1(data)
    # Otherwise treat as a v1 export (with or without the marker)
    data.setdefault("format", "onti-pack/v1")
    return data


def _slugify(name: Any) -> str:
    """Turn an arbitrary label into a lowercase pack-safe identifier.

    ``'ExperienceObservation'`` becomes ``'experienceobservation'``, while
    spaces and punctuation collapse to ``'_'``.

    Args:
        name (Any): Source label, converted with ``str``.

    Returns:
        str: Sanitised identifier, or ``"item"`` when nothing usable remains.

    Example:
        >>> _slugify("Monthly Experience")
        'monthly_experience'
    """
    s = re.sub(r"[^a-zA-Z0-9\-_]+", "_", str(name)).strip("_").lower()
    return s or "item"


def _convert_relationship_graph_to_v1(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a relationship-graph ontology into onti-pack/v1.

    Handles documents whose entities are plain name strings and whose
    ``relationships`` entries use from/to/type (``tables`` given as a dict),
    e.g. the ``lapse_demo.json`` fixture.

    Args:
        data (Dict[str, Any]): Parsed relationship-graph document.

    Returns:
        Dict[str, Any]: Equivalent onti-pack/v1 document.

    Raises:
        AdminValueError: When an entity or a relationship lacks its required
            name/type/endpoint fields.

    Example:
        >>> _convert_relationship_graph_to_v1({"entities": ["Policy"]})["slug"]
        'imported-pack'
    """
    slug = _slugify(data.get("slug") or data.get("id") or "imported-pack")
    name = data.get("name") or str(data.get("id") or slug).replace("_", " ").title()
    out: Dict[str, Any] = {
        "format": "onti-pack/v1",
        "slug": slug,
        "name": name,
        "description": data.get("description", ""),
        "narrative": data.get("narrative", ""),
    }
    # Entities: plain strings (or dicts with id/name)
    out["entities"] = []
    for i, e in enumerate(data.get("entities") or []):
        if isinstance(e, dict):
            key = e.get("id") or e.get("key") or e.get("name")
            label = e.get("label_en") or e.get("name") or key
        else:
            key, label = e, str(e)
        if not key:
            raise AdminValueError(f"entity #{i + 1}: missing entity name.")
        out["entities"].append({"key": _slugify(key), "label_en": label, "sort": i})
    for i, e in enumerate(data.get("deferred_entities") or []):
        key = e if isinstance(e, str) else (e.get("id") or e.get("name"))
        if not key:
            continue
        out["entities"].append(
            {
                "key": _slugify(key),
                "label_en": str(key),
                "description": "Deferred entity — defined in the source "
                "ontology but not yet active.",
                "sort": 100 + i,
            }
        )
    # Relationships: {from, to, type, keys, cardinality, ...}
    out["relations"] = []
    for i, r in enumerate(data.get("relationships") or []):
        rtype = r.get("type") or r.get("id") or r.get("key")
        src = r.get("from") or r.get("source_class") or r.get("source_key")
        tgt = r.get("to") or r.get("target_class") or r.get("target_key")
        if not rtype or not src or not tgt:
            raise AdminValueError(
                f"relationship #{i + 1}: missing 'type'/'from'/'to' fields."
            )
        notes = []
        if r.get("keys"):
            notes.append("join keys: " + ", ".join(str(k) for k in r["keys"]))
        if r.get("time_rule"):
            notes.append("time rule: " + str(r["time_rule"]))
        if r.get("rule"):
            notes.append(str(r["rule"]))
        card = str(r.get("cardinality", "many_to_many")).upper()
        if "ONE_TO_ONE" in card:
            card = "ONE_TO_ONE"
        elif "ONE_TO_MANY" in card:
            card = "ONE_TO_MANY"
        elif "MANY_TO_ONE" in card:
            card = "MANY_TO_ONE"
        else:
            card = "MANY_TO_MANY"
        out["relations"].append(
            {
                "key": _slugify(rtype),
                "label_en": str(rtype),
                "cardinality": card,
                "source_key": _slugify(src),
                "target_key": _slugify(tgt),
                "description": "; ".join(notes),
                "sort": i,
            }
        )
    # Tables: dict of {key: {sheets, fields, ...}} → pack_tables + columns
    out["tables"] = []
    for i, (tkey, t) in enumerate((data.get("tables") or {}).items()):
        if not isinstance(t, dict):
            continue
        cols = [
            {
                "key": _slugify(fk),
                "label": fk
                + (
                    " / " + " / ".join(str(x) for x in fv)
                    if isinstance(fv, list)
                    else ""
                ),
            }
            for fk, fv in (t.get("fields") or {}).items()
        ]
        out["tables"].append(
            {
                "key": _slugify(tkey),
                "label": str(tkey).replace("_", " ").title(),
                "description": "Sheets: " + ", ".join(t.get("sheets") or []),
                "columns": cols,
                "sort": i,
            }
        )
    # Narrative enrichment: definitions + open decisions give the AI/QA
    # layers useful domain context.
    extra = []
    defs = data.get("definitions") or {}
    if defs:
        extra.append("Glossary:\n" + "\n".join(f"- {k}: {v}" for k, v in defs.items()))
    if data.get("open_decisions"):
        extra.append(
            "Open decisions:\n" + "\n".join(f"- {d}" for d in data["open_decisions"])
        )
    if data.get("settings"):
        st = data["settings"]
        if isinstance(st, dict) and st.get("assumption_version"):
            extra.append(f"Assumption version: {st['assumption_version']}")
    if extra:
        out["narrative"] = (out["narrative"] + "\n\n" + "\n\n".join(extra)).strip()
    # Pass through graph / formula / rule data if present
    out["graph_nodes"] = data.get("graph_nodes", [])
    out["graph_edges"] = data.get("graph_edges", [])
    out["metric_formulas"] = data.get("metric_formulas", data.get("formulas", []))
    out["question_rules"] = data.get("question_rules", [])
    return out


def _convert_bp2_to_v1(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw ontology JSON (``ontology_BP2`` format) to onti-pack/v1.

    Args:
        data (Dict[str, Any]): Parsed BP2 document using ``id`` /
            ``owned_class`` / ``source_class`` field names.

    Returns:
        Dict[str, Any]: Equivalent onti-pack/v1 document.

    Raises:
        AdminValueError: When an entity, attribute or relation is missing its
            identifying fields.

    Example:
        >>> _convert_bp2_to_v1({"entities": [{"id": "Policy"}]})["format"]
        'onti-pack/v1'
    """
    out: Dict[str, Any] = {
        "format": "onti-pack/v1",
        "slug": data.get("slug") or "imported-pack",
        "name": data.get("name") or data.get("slug") or "Imported Pack",
        "description": data.get("description", ""),
        "narrative": data.get("narrative", ""),
    }
    out["entities"] = []
    for i, e in enumerate(data.get("entities", [])):
        key = e.get("id") or e.get("key")
        if not key:
            raise AdminValueError(f"entity #{i + 1}: missing 'id' (or 'key') field.")
        out["entities"].append(
            {
                "key": key,
                "label_en": e.get("label_en", key),
                "layer": e.get("layer", 3),
                "description": e.get("description", ""),
                "sort": i,
            }
        )
    out["attributes"] = []
    for i, a in enumerate(data.get("attributes", [])):
        key = a.get("id") or a.get("key")
        owner = a.get("owned_class") or a.get("entity_key")
        if not key:
            raise AdminValueError(f"attribute #{i + 1}: missing 'id' (or 'key') field.")
        if not owner:
            raise AdminValueError(
                f"attribute '{key}': missing 'owned_class' (owning entity) field."
            )
        out["attributes"].append(
            {
                "entity_key": owner,
                "key": key,
                "label_en": a.get("label_en", key),
                "data_type": a.get("data_type", "STRING"),
                "unit": a.get("unit", ""),
                "enum_values": a.get("enum_values", ""),
                "required": a.get("required", 0),
                "is_unique": a.get("unique", 0),
                "description": a.get("description", ""),
                "sort": i,
            }
        )
    out["relations"] = []
    for i, r in enumerate(data.get("relations", [])):
        key = r.get("id") or r.get("key")
        src = r.get("source_class") or r.get("source_key")
        tgt = r.get("target_class") or r.get("target_key")
        if not key:
            raise AdminValueError(f"relation #{i + 1}: missing 'id' (or 'key') field.")
        if not src or not tgt:
            raise AdminValueError(
                f"relation '{key}': missing 'source_class'/'target_class' fields."
            )
        out["relations"].append(
            {
                "key": key,
                "label_en": r.get("label_en", key),
                "cardinality": r.get("cardinality", "MANY_TO_MANY"),
                "source_key": src,
                "target_key": tgt,
                "source_attr": r.get("source_class_attribute", ""),
                "target_attr": r.get("target_class_attribute", ""),
                "description": r.get("description", ""),
                "sort": i,
            }
        )
    # Pass through any graph or formula data if present
    out["graph_nodes"] = data.get("graph_nodes", [])
    out["graph_edges"] = data.get("graph_edges", [])
    out["metric_formulas"] = data.get("metric_formulas", data.get("formulas", []))
    out["question_rules"] = data.get("question_rules", [])
    out["tables"] = data.get("tables", [])
    # Optional declarative analytics spec consumed by modules.deep_analysis.
    out["analysis"] = data.get("analysis") or {}
    return out


def import_pack(
    conn: sqlite3.Connection,
    actor: str,
    data: Dict[str, Any],
    slug_override: Optional[str] = None,
    activate: bool = False,
) -> int:
    """Import an ontology document into the database as a new pack.

    The payload is normalised first, then every section (entities,
    attributes, relations, graph nodes and edges, formulas, rules, tables
    with columns and dataset rows, question bank) is inserted and the pack is
    validated end-to-end. A failed import rolls the partial pack back.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        data (Dict[str, Any]): Pack document in any supported format.
        slug_override (Optional[str]): Slug to use instead of the payload's
            own slug.
        activate (bool): Activate the imported pack. Defaults to False.

    Returns:
        int: Primary key of the new pack.

    Raises:
        AdminValueError: When the slug is invalid, a required field is
            missing, or the data violates a unique constraint.
        AdminConflict: When the slug already exists.

    Example:
        >>> import_pack(conn, "admin", export_pack(conn, 1), "demo-copy")
        9
    """
    data = _normalize_pack_payload(data)
    slug = slug_override or data.get("slug")
    name = data.get("name") or slug
    if not SLUG_RE.match((slug or "").strip()):
        raise AdminValueError("Invalid pack slug.")
    if conn.execute("SELECT 1 FROM ontology_packs WHERE slug=?", (slug,)).fetchone():
        raise AdminConflict(
            f"Pack slug '{slug}' already exists.",
            [{"type": "duplicate_slug", "slug": slug}],
        )
    cur = conn.execute(
        "INSERT INTO ontology_packs(slug,name,description,narrative,is_active,created_at) "
        "VALUES(?,?,?,?,0,?)",
        (slug, name, data.get("description", ""), data.get("narrative", ""), now()),
    )
    pid = int(cur.lastrowid)
    try:
        for e in data.get("entities", []):
            _require(e, ("key", "label_en"), "entity")
            conn.execute(
                "INSERT INTO entities(pack_id,key,label_en,layer,description,sort) "
                "VALUES(?,?,?,?,?,?)",
                (
                    pid,
                    e["key"],
                    e["label_en"],
                    e.get("layer", 3),
                    e.get("description", ""),
                    e.get("sort", 0),
                ),
            )
        for a in data.get("attributes", []):
            _require(a, ("entity_key", "key", "label_en"), "attribute")
            conn.execute(
                "INSERT INTO attributes(pack_id,entity_key,key,label_en,data_type,unit,"
                "enum_values,required,is_unique,description,sort) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    a["entity_key"],
                    a["key"],
                    a["label_en"],
                    a.get("data_type", "STRING"),
                    a.get("unit", ""),
                    a.get("enum_values", ""),
                    a.get("required", 0),
                    a.get("is_unique", 0),
                    a.get("description", ""),
                    a.get("sort", 0),
                ),
            )
        for r in data.get("relations", []):
            _require(r, ("key", "source_key", "target_key"), "relation")
            conn.execute(
                "INSERT INTO relations(pack_id,key,label_en,cardinality,source_key,target_key,"
                "source_attr,target_attr,description,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    r["key"],
                    r.get("label_en", ""),
                    r.get("cardinality", "MANY_TO_MANY"),
                    r["source_key"],
                    r["target_key"],
                    r.get("source_attr", ""),
                    r.get("target_attr", ""),
                    r.get("description", ""),
                    r.get("sort", 0),
                ),
            )
        for n in data.get("graph_nodes", []):
            _require(n, ("key",), "graph_node")
            conn.execute(
                "INSERT INTO graph_nodes(pack_id,key,label,grp,value,description,aliases,"
                "is_core,sort) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    n["key"],
                    n.get("label", ""),
                    n.get("grp", "financial"),
                    n.get("value", ""),
                    n.get("description", ""),
                    n.get("aliases", "[]"),
                    n.get("is_core", 0),
                    n.get("sort", 0),
                ),
            )
        node_keys = {n["key"] for n in data.get("graph_nodes", [])}
        for g in data.get("graph_edges", []):
            _require(g, ("key", "source_key", "target_key"), "graph_edge")
            missing = [
                k
                for k in str(g["source_key"]).split(",") + [g["target_key"]]
                if k not in node_keys
            ]
            if missing:
                raise AdminValueError(
                    f"edge {g['key']} references unknown nodes {missing}"
                )
            conn.execute(
                "INSERT INTO graph_edges(pack_id,key,source_key,target_key,edge_type,label,"
                "description,sort) VALUES(?,?,?,?,?,?,?,?)",
                (
                    pid,
                    g["key"],
                    g["source_key"],
                    g["target_key"],
                    g.get("edge_type", "STRUCTURAL"),
                    g.get("label", ""),
                    g.get("description", ""),
                    g.get("sort", 0),
                ),
            )
        for f in data.get("metric_formulas", []):
            _require(f, ("key", "expression"), "metric_formula")
            conn.execute(
                "INSERT INTO metric_formulas(pack_id,key,label,expression,unit,fmt,decimals,sort)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    pid,
                    f["key"],
                    f.get("label", ""),
                    f["expression"],
                    f.get("unit", ""),
                    f.get("fmt", "number"),
                    f.get("decimals", 2),
                    f.get("sort", 0),
                ),
            )
        for q in data.get("question_rules", []):
            _require(q, ("key", "template"), "question_rule")
            conn.execute(
                "INSERT INTO question_rules(pack_id,key,category,patterns,require_groups,guards,"
                "template,evidence_nodes,path_steps,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    q["key"],
                    q.get("category", ""),
                    q.get("patterns", "[]"),
                    q.get("require_groups", "[]"),
                    q.get("guards", "[]"),
                    q["template"],
                    q.get("evidence_nodes", "[]"),
                    q.get("path_steps", "[]"),
                    q.get("sort", 0),
                ),
            )
        # tables / columns / datasets / rows
        for t in data.get("tables", []):
            _require(t, ("key",), "table")
            tcur = conn.execute(
                "INSERT INTO pack_tables(pack_id,key,label,entity_key,description) "
                "VALUES(?,?,?,?,?)",
                (
                    pid,
                    t["key"],
                    t.get("label", ""),
                    t.get("entity_key", ""),
                    t.get("description", ""),
                ),
            )
            tid = int(tcur.lastrowid)
            for c in t.get("columns", []):
                _require(c, ("key",), "column")
                conn.execute(
                    "INSERT INTO pack_columns(table_id,key,label,data_type,unit,enum_values,"
                    "expression,fmt,decimals,required,sort) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid,
                        c["key"],
                        c.get("label", ""),
                        c.get("data_type", "STRING"),
                        c.get("unit", ""),
                        c.get("enum_values", ""),
                        c.get("expression", ""),
                        c.get("fmt", "raw"),
                        c.get("decimals", 2),
                        c.get("required", 0),
                        c.get("sort", 0),
                    ),
                )
            for di, d in enumerate(t.get("datasets", [])):
                dcur = conn.execute(
                    "INSERT INTO datasets(pack_id,table_id,name,filename,row_count,kind,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        pid,
                        tid,
                        d.get("name", "Dataset"),
                        d.get("filename", ""),
                        len(d.get("rows", [])),
                        d.get("kind", "upload"),
                        now(),
                    ),
                )
                did = int(dcur.lastrowid)
                if di == 0 or d.get("kind") == "seed":
                    conn.execute(
                        "UPDATE pack_tables SET active_dataset_id=? WHERE id=?",
                        (did, tid),
                    )
                for seq, row in enumerate(d.get("rows", [])):
                    conn.execute(
                        "INSERT INTO pack_rows(dataset_id,seq,data) VALUES(?,?,?)",
                        (did, seq, json.dumps(row, ensure_ascii=False)),
                    )
        for q in data.get("question_bank", []):
            _require(q, ("question",), "question_bank")
            add_question(
                conn,
                pid,
                q["question"],
                q.get("category", ""),
                q.get("source", "imported"),
            )
        # Validate the imported pack end-to-end.
        pack = svc.load_pack(conn, pid)
        svc.compute_pack(pack)
        if data.get("analysis"):
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
                (f"analysis:{pid}", json.dumps(data["analysis"], ensure_ascii=False)),
            )
        if activate:
            activate_pack(conn, actor, pid)
        bump_pack_version(conn, actor, pid)
        audit(conn, actor, "pack.import", f"id={pid} slug={slug}")
    except sqlite3.IntegrityError as e:
        conn.execute("DELETE FROM ontology_packs WHERE id=?", (pid,))
        raise AdminValueError(
            f"Pack data conflicts with existing records: {e}. "
            "Entity/attribute/relation keys must be unique within the pack."
        )
    except Exception:
        conn.execute("DELETE FROM ontology_packs WHERE id=?", (pid,))
        raise
    return pid


# ---------------------------------------------------------------------------
# Pack versions — snapshot / list / restore
# ---------------------------------------------------------------------------


def _pack_counts(conn: sqlite3.Connection, pack_id: int) -> Dict[str, int]:
    """Count the ontology elements belonging to a pack.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        Dict[str, int]: Counts keyed by ``entities``, ``attributes``,
            ``relations``, ``nodes``, ``edges``, ``formulas``, ``rules``,
            ``tables`` and ``questions``.

    Example:
        >>> _pack_counts(conn, 1)["entities"]
        12
    """
    counts: Dict[str, int] = {}
    for table, name in (
        ("entities", "entities"),
        ("attributes", "attributes"),
        ("relations", "relations"),
        ("graph_nodes", "nodes"),
        ("graph_edges", "edges"),
        ("metric_formulas", "formulas"),
        ("question_rules", "rules"),
        ("pack_tables", "tables"),
    ):
        counts[name] = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE pack_id=?", (pack_id,)
        ).fetchone()["n"]
    counts["questions"] = conn.execute(
        "SELECT COUNT(*) AS n FROM question_bank WHERE pack_id=?", (pack_id,)
    ).fetchone()["n"]
    return counts


def snapshot_pack(
    conn: sqlite3.Connection, actor: str, pack_id: int, label: str = ""
) -> int:
    """Store a full onti-pack/v1 snapshot of the pack (auto-backup safe point).

    Only the 20 most recent versions of a pack are retained.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        label (str): Snapshot label; defaults to ``"auto-backup"``.

    Returns:
        int: Primary key of the created ``pack_versions`` row.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> snapshot_pack(conn, "admin", 1, "before-edit")
        3
    """
    pack = get_pack(conn, pack_id)
    if not label:
        label = "auto-backup"
    payload = export_pack(conn, pack_id)
    cur = conn.execute(
        "INSERT INTO pack_versions(pack_id,label,created_by,created_at,counts,payload) "
        "VALUES(?,?,?,?,?,?)",
        (
            pack_id,
            label,
            actor,
            now(),
            jdumps(_pack_counts(conn, pack_id)),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    vid = int(cur.lastrowid)
    # keep only the most recent 20 versions per pack
    conn.execute(
        "DELETE FROM pack_versions WHERE pack_id=? AND id NOT IN ("
        "SELECT id FROM pack_versions WHERE pack_id=? ORDER BY id DESC LIMIT 20)",
        (pack_id, pack_id),
    )
    audit(
        conn, actor, "pack.snapshot", f"pack={pack['slug']} version={vid} label={label}"
    )
    return vid


def list_versions(conn: sqlite3.Connection, pack_id: int) -> List[Dict[str, Any]]:
    """List the stored snapshots of a pack, newest first.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        List[Dict[str, Any]]: ``id``, ``label``, ``created_by``,
            ``created_at`` and the decoded ``counts`` per version.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> list_versions(conn, 1)[0]["label"]
        'before-edit'
    """
    get_pack(conn, pack_id)
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "counts": jloads(r["counts"], {}),
        }
        for r in conn.execute(
            "SELECT id,label,created_by,created_at,counts FROM pack_versions "
            "WHERE pack_id=? ORDER BY id DESC",
            (pack_id,),
        )
    ]


def restore_version(
    conn: sqlite3.Connection, actor: str, pack_id: int, version_id: int
) -> None:
    """Replace the pack content with a stored snapshot.

    The current state is auto-snapshotted first, so a restore is always
    reversible. Activation flag and slug are preserved.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack to restore.
        version_id (int): Primary key of the ``pack_versions`` row to apply.

    Returns:
        None: The restored pack keeps the original slug.

    Raises:
        AdminValueError: When the pack or the version is missing, or the
            stored payload cannot be imported.

    Example:
        >>> restore_version(conn, "admin", 1, 3)
    """
    pack = get_pack(conn, pack_id)
    row = conn.execute(
        "SELECT payload FROM pack_versions WHERE id=? AND pack_id=?",
        (version_id, pack_id),
    ).fetchone()
    if row is None:
        raise AdminValueError("Version not found for this pack.")
    payload = json.loads(row["payload"])

    # Safety net: snapshot the current state before overwriting it.  The
    # snapshot row belongs to the OLD pack id and pack_versions cascades on
    # pack delete — so we re-insert it under the new pack id afterwards.
    snapshot_pack(conn, actor, pack_id, label="before-restore")
    safety_row = conn.execute(
        "SELECT label,created_by,created_at,counts,payload FROM pack_versions "
        "WHERE pack_id=? ORDER BY id DESC LIMIT 1",
        (pack_id,),
    ).fetchone()

    was_active = bool(pack["is_active"])
    conn.execute("DELETE FROM ontology_packs WHERE id=?", (pack_id,))
    new_id = import_pack(conn, actor, payload, slug_override=pack["slug"])
    if safety_row is not None:
        conn.execute(
            "INSERT INTO pack_versions(pack_id,label,created_by,created_at,counts,payload) "
            "VALUES(?,?,?,?,?,?)",
            (
                new_id,
                safety_row["label"],
                safety_row["created_by"],
                safety_row["created_at"],
                safety_row["counts"],
                safety_row["payload"],
            ),
        )
    if was_active:
        conn.execute("UPDATE ontology_packs SET is_active=0")
        conn.execute("UPDATE ontology_packs SET is_active=1 WHERE id=?", (new_id,))
        set_setting(conn, "active_pack_id", str(new_id))
    audit(
        conn,
        actor,
        "pack.restore",
        f"pack={pack['slug']} version={version_id} new_id={new_id}",
    )


# ---------------------------------------------------------------------------
# Import preview (dry run) — inspect a pack file before committing it
# ---------------------------------------------------------------------------


def preview_import(
    conn: sqlite3.Connection,
    data: Dict[str, Any],
    slug_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and summarise a pack payload **without writing anything**.

    Args:
        conn (sqlite3.Connection): Open database connection.
        data (Dict[str, Any]): Pack document in any supported format.
        slug_override (Optional[str]): Slug to validate instead of the
            payload's own slug.

    Returns:
        Dict[str, Any]: ``slug``, ``name``, ``counts``, ``issues`` (blocking
            problems), ``warnings`` (advisory notes) and ``can_import``.

    Raises:
        AdminValueError: When the payload cannot be normalised at all.

    Example:
        >>> preview_import(conn, {"entities": [{"key": "a", "label_en": "A"}]})["can_import"]
        True
    """
    data = _normalize_pack_payload(data)
    slug = slug_override or data.get("slug")
    name = data.get("name") or slug
    issues: List[str] = []
    warnings: List[str] = []

    if not SLUG_RE.match((slug or "").strip()):
        issues.append("Invalid pack slug (lowercase letters, digits, '-' and '_').")
    elif conn.execute("SELECT 1 FROM ontology_packs WHERE slug=?", (slug,)).fetchone():
        issues.append(
            f"Pack slug '{slug}' already exists — rename it before importing."
        )

    counts: Dict[str, int] = {}
    entity_keys = {e.get("key") for e in data.get("entities", []) if e.get("key")}
    node_keys = {n.get("key") for n in data.get("graph_nodes", []) if n.get("key")}

    sections = (
        ("entities", ("key", "label_en"), "entity"),
        ("attributes", ("entity_key", "key", "label_en"), "attribute"),
        ("relations", ("key", "source_key", "target_key"), "relation"),
        ("graph_nodes", ("key",), "graph node"),
        ("graph_edges", ("key", "source_key", "target_key"), "graph edge"),
        ("metric_formulas", ("key", "expression"), "metric formula"),
        ("question_rules", ("key", "template"), "question rule"),
        ("tables", ("key",), "table"),
        ("question_bank", ("question",), "question bank entry"),
    )
    for section, required, label in sections:
        items = data.get(section, [])
        counts[section] = len(items)
        for i, item in enumerate(items):
            missing = [k for k in required if item.get(k) in (None, "")]
            if missing:
                issues.append(f"{label} #{i + 1}: missing field(s) {missing}.")
            if len(issues) > 30:  # cap noise
                break
    # reference checks
    for i, a in enumerate(data.get("attributes", [])):
        if a.get("entity_key") and entity_keys and a["entity_key"] not in entity_keys:
            issues.append(
                f"attribute '{a.get('key')}' references unknown entity '{a['entity_key']}'."
            )
    for i, g in enumerate(data.get("graph_edges", [])):
        missing = [
            k
            for k in str(g.get("source_key", "")).split(",") + [g.get("target_key", "")]
            if k and node_keys and k not in node_keys
        ]
        if missing:
            issues.append(
                f"graph edge '{g.get('key')}' references unknown nodes {missing}."
            )
    if not data.get("entities"):
        warnings.append("No entities defined — the ontology will be empty.")
    if data.get("graph_edges") and not data.get("graph_nodes"):
        issues.append("Pack has graph edges but no graph nodes.")
    if not data.get("tables"):
        warnings.append(
            "No data tables in this pack — metrics will have nothing to compute."
        )

    return {
        "slug": slug,
        "name": name,
        "counts": counts,
        "issues": issues[:50],
        "warnings": warnings,
        "can_import": not issues,
    }


# ---------------------------------------------------------------------------
# Ontology element CRUD
# ---------------------------------------------------------------------------


def _entity_keys(conn, pack_id: int) -> set:
    """Collect the entity keys defined in a pack.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        set: Set of entity key strings.

    Example:
        >>> sorted(_entity_keys(conn, 1))
        ['claim', 'policy']
    """
    return {
        r["key"]
        for r in conn.execute("SELECT key FROM entities WHERE pack_id=?", (pack_id,))
    }


def _node_keys(conn, pack_id: int) -> set:
    """Collect the graph node keys defined in a pack.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        set: Set of graph node key strings.

    Example:
        >>> sorted(_node_keys(conn, 1))
        ['claims', 'premium']
    """
    return {
        r["key"]
        for r in conn.execute("SELECT key FROM graph_nodes WHERE pack_id=?", (pack_id,))
    }


def upsert_entity(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update an entity and bump the pack version.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Entity fields; ``key`` and ``label_en`` are
            required, ``layer``, ``description`` and ``sort`` optional.

    Returns:
        int: Primary key of the stored entity row.

    Raises:
        AdminValueError: When the pack is missing, or the key or label is
            empty.

    Example:
        >>> upsert_entity(conn, "admin", 1, {"key": "policy", "label_en": "Policy"})
        4
    """
    get_pack(conn, pack_id)
    if not payload.get("key") or not payload.get("label_en"):
        raise AdminValueError("Entity key and label are required.")
    row = conn.execute(
        "SELECT id FROM entities WHERE pack_id=? AND key=?", (pack_id, payload["key"])
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE entities SET label_en=?,layer=?,description=?,sort=? WHERE id=?",
            (
                payload["label_en"],
                payload.get("layer", 3),
                payload.get("description", ""),
                payload.get("sort", 0),
                row["id"],
            ),
        )
        eid = row["id"]
        action = "entity.update"
    else:
        eid = conn.execute(
            "INSERT INTO entities(pack_id,key,label_en,layer,description,sort) VALUES(?,?,?,?,?,?)",
            (
                pack_id,
                payload["key"],
                payload["label_en"],
                payload.get("layer", 3),
                payload.get("description", ""),
                payload.get("sort", 0),
            ),
        ).lastrowid
        action = "entity.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(eid)


def delete_entity(conn, actor, pack_id: int, key: str) -> None:
    """Delete an entity once it has no dependents.

    Attributes, pack tables and relation endpoints referencing the entity are
    reported as conflicts instead of being deleted silently.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Entity key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the entity does not exist (404).
        AdminConflict: When other rows still reference the entity.

    Example:
        >>> delete_entity(conn, "admin", 1, "policy")
    """
    row = conn.execute(
        "SELECT id FROM entities WHERE pack_id=? AND key=?", (pack_id, key)
    ).fetchone()
    if not row:
        raise AdminValueError("Entity not found.", 404)
    conflicts = []
    for t, col in (("attributes", "entity_key"), ("pack_tables", "entity_key")):
        hits = conn.execute(
            f"SELECT key FROM {t} WHERE pack_id=? AND {col}=?", (pack_id, key)
        ).fetchall()
        conflicts += [{"type": t, "key": h["key"]} for h in hits]
    for t, col in (("relations", "source_key"), ("relations", "target_key")):
        hits = conn.execute(
            f"SELECT key FROM {t} WHERE pack_id=? AND {col}=?", (pack_id, key)
        ).fetchall()
        conflicts += [{"type": "relation_endpoint", "key": h["key"]} for h in hits]
    if conflicts:
        raise AdminConflict(
            f"Entity '{key}' is in use; remove dependents first.", conflicts
        )
    conn.execute("DELETE FROM entities WHERE id=?", (row["id"],))
    audit(conn, actor, "entity.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


def upsert_relation(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update a relation between two existing entities.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Relation fields; ``key``, ``source_key`` and
            ``target_key`` are required.

    Returns:
        int: Primary key of the stored relation row.

    Raises:
        AdminValueError: When a required field is empty or an endpoint does
            not reference an existing entity.

    Example:
        >>> upsert_relation(conn, "admin", 1, {
        ...     "key": "policy_claim", "source_key": "policy", "target_key": "claim"})
        2
    """
    keys = _entity_keys(conn, pack_id)
    for f in ("key", "source_key", "target_key"):
        if not payload.get(f):
            raise AdminValueError(f"Relation '{f}' is required.")
    if payload["source_key"] not in keys or payload["target_key"] not in keys:
        raise AdminValueError(
            "source_key/target_key must reference existing entities.", 422
        )
    row = conn.execute(
        "SELECT id FROM relations WHERE pack_id=? AND key=?", (pack_id, payload["key"])
    ).fetchone()
    vals = (
        payload.get("label_en", ""),
        payload.get("cardinality", "MANY_TO_MANY"),
        payload["source_key"],
        payload["target_key"],
        payload.get("source_attr", ""),
        payload.get("target_attr", ""),
        payload.get("description", ""),
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE relations SET label_en=?,cardinality=?,source_key=?,target_key=?,"
            "source_attr=?,target_attr=?,description=?,sort=? WHERE id=?",
            vals + (row["id"],),
        )
        rid = row["id"]
        action = "relation.update"
    else:
        rid = conn.execute(
            "INSERT INTO relations(pack_id,key,label_en,cardinality,source_key,target_key,"
            "source_attr,target_attr,description,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pack_id, payload["key"]) + vals,
        ).lastrowid
        action = "relation.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(rid)


def delete_relation(conn, actor, pack_id: int, key: str) -> None:
    """Delete a relation and bump the pack version.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Relation key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the relation does not exist (404).

    Example:
        >>> delete_relation(conn, "admin", 1, "policy_claim")
    """
    cur = conn.execute(
        "DELETE FROM relations WHERE pack_id=? AND key=?", (pack_id, key)
    )
    if cur.rowcount == 0:
        raise AdminValueError("Relation not found.", 404)
    audit(conn, actor, "relation.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


def upsert_attribute(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update an attribute owned by an existing entity.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Attribute fields; ``entity_key``, ``key``
            and ``label_en`` are required.

    Returns:
        int: Primary key of the stored attribute row.

    Raises:
        AdminValueError: When the entity is unknown, a required field is empty,
            or ``data_type`` is not STRING/INTEGER/FLOAT.

    Example:
        >>> upsert_attribute(conn, "admin", 1, {
        ...     "entity_key": "policy", "key": "premium", "label_en": "Premium"})
        6
    """
    if payload.get("entity_key") not in _entity_keys(conn, pack_id):
        raise AdminValueError("entity_key must reference an existing entity.")
    if not payload.get("key") or not payload.get("label_en"):
        raise AdminValueError("Attribute key and label are required.")
    if payload.get("data_type", "STRING") not in ("STRING", "INTEGER", "FLOAT"):
        raise AdminValueError("data_type must be STRING, INTEGER or FLOAT.")
    row = conn.execute(
        "SELECT id FROM attributes WHERE pack_id=? AND entity_key=? AND key=?",
        (pack_id, payload["entity_key"], payload["key"]),
    ).fetchone()
    vals = (
        payload["label_en"],
        payload.get("data_type", "STRING"),
        payload.get("unit", ""),
        payload.get("enum_values", ""),
        1 if payload.get("required") else 0,
        1 if payload.get("is_unique") else 0,
        payload.get("description", ""),
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE attributes SET label_en=?,data_type=?,unit=?,enum_values=?,required=?,"
            "is_unique=?,description=?,sort=? WHERE id=?",
            vals + (row["id"],),
        )
        aid = row["id"]
        action = "attribute.update"
    else:
        aid = conn.execute(
            "INSERT INTO attributes(pack_id,entity_key,key,label_en,data_type,unit,enum_values,"
            "required,is_unique,description,sort) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pack_id, payload["entity_key"], payload["key"]) + vals,
        ).lastrowid
        action = "attribute.create"
    audit(
        conn, actor, action, f"pack={pack_id} {payload['entity_key']}.{payload['key']}"
    )
    bump_pack_version(conn, actor, pack_id)
    return int(aid)


def delete_attribute(conn, actor, pack_id: int, entity_key: str, key: str) -> None:
    """Delete an attribute and bump the pack version.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        entity_key (str): Owning entity key.
        key (str): Attribute key.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the attribute does not exist (404).

    Example:
        >>> delete_attribute(conn, "admin", 1, "policy", "premium")
    """
    cur = conn.execute(
        "DELETE FROM attributes WHERE pack_id=? AND entity_key=? AND key=?",
        (pack_id, entity_key, key),
    )
    if cur.rowcount == 0:
        raise AdminValueError("Attribute not found.", 404)
    audit(conn, actor, "attribute.delete", f"pack={pack_id} {entity_key}.{key}")
    bump_pack_version(conn, actor, pack_id)


# ----- graph ----------------------------------------------------------------


def upsert_node(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update a graph node.

    Aliases are stored as a JSON list; a comma separated string is split into
    one alias per part.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Node fields; ``key`` is required.

    Returns:
        int: Primary key of the stored node row.

    Raises:
        AdminValueError: When ``key`` is empty.

    Example:
        >>> upsert_node(conn, "admin", 1, {"key": "claims", "label": "Claims"})
        3
    """
    if not payload.get("key"):
        raise AdminValueError("Node key is required.")
    aliases = payload.get("aliases", [])
    aliases_json = json.dumps(
        aliases if isinstance(aliases, list) else [str(aliases).split(",")],
        ensure_ascii=False,
    )
    row = conn.execute(
        "SELECT id FROM graph_nodes WHERE pack_id=? AND key=?",
        (pack_id, payload["key"]),
    ).fetchone()
    vals = (
        payload.get("label", payload["key"]),
        payload.get("grp", "financial"),
        payload.get("value", ""),
        payload.get("description", ""),
        aliases_json,
        1 if payload.get("is_core") else 0,
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE graph_nodes SET label=?,grp=?,value=?,description=?,aliases=?,is_core=?,"
            "sort=? WHERE id=?",
            vals + (row["id"],),
        )
        nid, action = row["id"], "node.update"
    else:
        nid = conn.execute(
            "INSERT INTO graph_nodes(pack_id,key,label,grp,value,description,aliases,is_core,sort)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (pack_id, payload["key"]) + vals,
        ).lastrowid
        action = "node.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(nid)


def delete_node(conn, actor, pack_id: int, key: str) -> None:
    """Delete a graph node that no edge references.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Node key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the node does not exist (404).
        AdminConflict: When at least one graph edge references the node.

    Example:
        >>> delete_node(conn, "admin", 1, "claims")
    """
    if not conn.execute(
        "SELECT 1 FROM graph_nodes WHERE pack_id=? AND key=?", (pack_id, key)
    ).fetchone():
        raise AdminValueError("Node not found.", 404)
    conflicts = []
    for e in conn.execute(
        "SELECT key, source_key, target_key FROM graph_edges WHERE pack_id=?",
        (pack_id,),
    ):
        ends = [s.strip() for s in str(e["source_key"]).split(",")] + [e["target_key"]]
        if key in ends:
            conflicts.append({"type": "edge", "key": e["key"]})
    if conflicts:
        raise AdminConflict(
            f"Node '{key}' is referenced by edges; delete or re-point them first.",
            conflicts,
        )
    conn.execute("DELETE FROM graph_nodes WHERE pack_id=? AND key=?", (pack_id, key))
    audit(conn, actor, "node.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


def upsert_edge(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update a graph edge between existing nodes.

    ``source_key`` may hold a comma separated list of source node keys.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Edge fields; ``key``, ``source_key`` and
            ``target_key`` are required.

    Returns:
        int: Primary key of the stored edge row.

    Raises:
        AdminValueError: When a required field is empty or an endpoint is not
            an existing graph node.

    Example:
        >>> upsert_edge(conn, "admin", 1, {
        ...     "key": "claims_premium", "source_key": "claims", "target_key": "premium"})
        5
    """
    for f in ("key", "source_key", "target_key"):
        if not payload.get(f):
            raise AdminValueError(f"Edge '{f}' is required.")
    node_keys = _node_keys(conn, pack_id)
    ends = [s.strip() for s in str(payload["source_key"]).split(",")] + [
        payload["target_key"]
    ]
    missing = [k for k in ends if k not in node_keys]
    if missing:
        raise AdminValueError(f"Edge references unknown nodes: {missing}")
    row = conn.execute(
        "SELECT id FROM graph_edges WHERE pack_id=? AND key=?",
        (pack_id, payload["key"]),
    ).fetchone()
    vals = (
        payload["source_key"],
        payload["target_key"],
        payload.get("edge_type", "STRUCTURAL"),
        payload.get("label", ""),
        payload.get("description", ""),
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE graph_edges SET source_key=?,target_key=?,edge_type=?,label=?,description=?,"
            "sort=? WHERE id=?",
            vals + (row["id"],),
        )
        eid, action = row["id"], "edge.update"
    else:
        eid = conn.execute(
            "INSERT INTO graph_edges(pack_id,key,source_key,target_key,edge_type,label,"
            "description,sort) VALUES(?,?,?,?,?,?,?,?)",
            (pack_id, payload["key"]) + vals,
        ).lastrowid
        action = "edge.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(eid)


def delete_edge(conn, actor, pack_id: int, key: str) -> None:
    """Delete a graph edge and bump the pack version.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Edge key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the edge does not exist (404).

    Example:
        >>> delete_edge(conn, "admin", 1, "claims_premium")
    """
    cur = conn.execute(
        "DELETE FROM graph_edges WHERE pack_id=? AND key=?", (pack_id, key)
    )
    if cur.rowcount == 0:
        raise AdminValueError("Edge not found.", 404)
    audit(conn, actor, "edge.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


# ----- formulas / rules -----------------------------------------------------


def upsert_formula(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update a metric formula after validating its expression.

    The expression may only reference helper functions and the other metric
    keys of the pack; the edited key itself is removed from the namespace so
    formulas cannot self-reference.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Formula fields; ``key`` and ``expression``
            are required.

    Returns:
        int: Primary key of the stored formula row.

    Raises:
        AdminValueError: When a required field is empty or the expression does
            not validate.

    Example:
        >>> upsert_formula(conn, "admin", 1, {
        ...     "key": "loss_ratio", "expression": "claims / premium"})
        2
    """
    if not payload.get("key") or not payload.get("expression"):
        raise AdminValueError("Formula key and expression are required.")
    # Allowed names: helper functions + all existing metric keys (the edited
    # key itself is removed so formulas cannot self-reference).
    pack = svc.load_pack(conn, pack_id)
    allowed = svc.formula_namespace_names(pack)
    allowed.discard(payload["key"])
    try:
        svc.validate_expression(payload["expression"], allowed)
    except Exception as e:
        raise AdminValueError(f"Invalid formula: {e}", 400)
    row = conn.execute(
        "SELECT id FROM metric_formulas WHERE pack_id=? AND key=?",
        (pack_id, payload["key"]),
    ).fetchone()
    vals = (
        payload.get("label", ""),
        payload["expression"],
        payload.get("unit", ""),
        payload.get("fmt", "number"),
        payload.get("decimals", 2),
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE metric_formulas SET label=?,expression=?,unit=?,fmt=?,decimals=?,"
            "sort=? WHERE id=?",
            vals + (row["id"],),
        )
        fid, action = row["id"], "formula.update"
    else:
        fid = conn.execute(
            "INSERT INTO metric_formulas(pack_id,key,label,expression,unit,fmt,decimals,sort)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (pack_id, payload["key"]) + vals,
        ).lastrowid
        action = "formula.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(fid)


def delete_formula(conn, actor, pack_id: int, key: str) -> None:
    """Delete a metric formula that no other formula references.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Formula key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the formula does not exist (404).
        AdminConflict: When another formula's expression uses ``key``.

    Example:
        >>> delete_formula(conn, "admin", 1, "loss_ratio")
    """
    # Other formulas referencing this key?
    others = conn.execute(
        "SELECT key, expression FROM metric_formulas WHERE pack_id=? AND key<>?",
        (pack_id, key),
    ).fetchall()
    token = re.compile(rf"\b{re.escape(key)}\b")
    conflicts = [
        {"type": "formula", "key": r["key"]}
        for r in others
        if token.search(r["expression"])
    ]
    if conflicts:
        raise AdminConflict(f"Metric '{key}' is used by other formulas.", conflicts)
    cur = conn.execute(
        "DELETE FROM metric_formulas WHERE pack_id=? AND key=?", (pack_id, key)
    )
    if cur.rowcount == 0:
        raise AdminValueError("Formula not found.", 404)
    audit(conn, actor, "formula.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


def _valid_regex_list(value: Any, field: str) -> str:
    """Validate a list of regular expressions and return it as JSON.

    Args:
        value (Any): A JSON string, a list of patterns, or ``None``.
        field (str): Field name used in error messages.

    Returns:
        str: JSON-encoded list of patterns that all compile.

    Raises:
        AdminValueError: When the value is not a list of strings or a pattern
            does not compile.

    Example:
        >>> _valid_regex_list(["premium"], "patterns")
        '["premium"]'
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            raise AdminValueError(f"{field} must be a JSON list of regex strings.")
    else:
        parsed = value or []
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        raise AdminValueError(f"{field} must be a list of strings.")
    for p in parsed:
        try:
            re.compile(p)
        except re.error as e:
            raise AdminValueError(f"{field}: invalid regex {p!r}: {e}")
    return json.dumps(parsed, ensure_ascii=False)


def upsert_rule(conn, actor, pack_id: int, payload: Dict[str, Any]) -> int:
    """Create or update a question rule.

    Patterns, require-groups and guards must be valid regex lists and the
    Jinja answer template must parse.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        payload (Dict[str, Any]): Rule fields; ``key`` and ``template`` are
            required.

    Returns:
        int: Primary key of the stored rule row.

    Raises:
        AdminValueError: When a required field is empty, a regex is invalid,
            or the template cannot be parsed.

    Example:
        >>> upsert_rule(conn, "admin", 1, {
        ...     "key": "loss_ratio", "patterns": ["loss ratio"],
        ...     "template": "The loss ratio is {{ value }}."})
        2
    """
    if not payload.get("key") or not payload.get("template"):
        raise AdminValueError("Rule key and answer template are required.")
    patterns = _valid_regex_list(payload.get("patterns", []), "patterns")
    groups = _valid_regex_list(payload.get("require_groups", []), "require_groups")
    guards = _valid_regex_list(payload.get("guards", []), "guards")
    # Jinja template must parse.
    try:
        svc.SandboxedEnvironment().parse(payload["template"])
    except Exception as e:
        raise AdminValueError(f"Invalid Jinja template: {e}")
    ev = payload.get("evidence_nodes", [])
    ev_json = ev if isinstance(ev, str) else json.dumps(ev or [], ensure_ascii=False)
    ps = payload.get("path_steps", [])
    ps_json = ps if isinstance(ps, str) else json.dumps(ps or [], ensure_ascii=False)
    row = conn.execute(
        "SELECT id FROM question_rules WHERE pack_id=? AND key=?",
        (pack_id, payload["key"]),
    ).fetchone()
    vals = (
        payload.get("category", ""),
        patterns,
        groups,
        guards,
        payload["template"],
        ev_json,
        ps_json,
        payload.get("sort", 0),
    )
    if row:
        conn.execute(
            "UPDATE question_rules SET category=?,patterns=?,require_groups=?,guards=?,"
            "template=?,evidence_nodes=?,path_steps=?,sort=? WHERE id=?",
            vals + (row["id"],),
        )
        rid, action = row["id"], "rule.update"
    else:
        rid = conn.execute(
            "INSERT INTO question_rules(pack_id,key,category,patterns,require_groups,guards,"
            "template,evidence_nodes,path_steps,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pack_id, payload["key"]) + vals,
        ).lastrowid
        action = "rule.create"
    audit(conn, actor, action, f"pack={pack_id} key={payload['key']}")
    bump_pack_version(conn, actor, pack_id)
    return int(rid)


def delete_rule(conn, actor, pack_id: int, key: str) -> None:
    """Delete a question rule and bump the pack version.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Rule key to delete.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the rule does not exist (404).

    Example:
        >>> delete_rule(conn, "admin", 1, "loss_ratio")
    """
    cur = conn.execute(
        "DELETE FROM question_rules WHERE pack_id=? AND key=?", (pack_id, key)
    )
    if cur.rowcount == 0:
        raise AdminValueError("Rule not found.", 404)
    audit(conn, actor, "rule.delete", f"pack={pack_id} key={key}")
    bump_pack_version(conn, actor, pack_id)


# ---------------------------------------------------------------------------
# Question bank
# ---------------------------------------------------------------------------


def normalize_question(text: str) -> str:
    """Normalise a question for duplicate detection.

    Lowercases the text, strips punctuation and collapses whitespace runs into
    single spaces.

    Args:
        text (str): Raw question text.

    Returns:
        str: Normalised text; empty string for empty input.

    Example:
        >>> normalize_question("What is the Loss Ratio?")
        'what is the loss ratio'
    """
    return re.sub(
        r"\s+",
        " ",
        re.sub(
            r"[\s\.,!?;:\(\)\[\]\{\}\"'`/\\\-_*&^%$#@!~|<>]+", " ", (text or "").lower()
        ),
    ).strip()


def add_question(
    conn,
    pack_id: int,
    text: str,
    category: str = "",
    source: str = "imported",
    actor: str = "system",
) -> bool:
    """Insert a question into the bank unless an equivalent one exists.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.
        text (str): Question text.
        category (str): Optional category. Defaults to "".
        source (str): Provenance marker, e.g. ``"imported"``.
        actor (str): Actor name reserved for callers; defaults to "system".

    Returns:
        bool: ``True`` when a row was inserted, ``False`` for an empty or
            already present question.

    Example:
        >>> add_question(conn, 1, "What is the loss ratio?")
        True
    """
    text = (text or "").strip()
    if not text:
        return False
    norm = normalize_question(text)
    if conn.execute(
        "SELECT 1 FROM question_bank WHERE pack_id=? AND norm=?", (pack_id, norm)
    ).fetchone():
        return False
    conn.execute(
        "INSERT INTO question_bank(pack_id,question,norm,category,source,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (pack_id, text, norm, category, source, now()),
    )
    return True


def import_questions(
    conn, actor, pack_id: int, items: List[Dict[str, str]]
) -> Dict[str, int]:
    """Bulk-import questions into a pack's question bank.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        items (List[Dict[str, str]]): Entries with ``question`` and optional
            ``category`` / ``source``.

    Returns:
        Dict[str, int]: ``inserted``, ``duplicates``, ``invalid`` and
            ``total`` counts.

    Raises:
        AdminValueError: With status 404 when the pack does not exist.

    Example:
        >>> import_questions(conn, "admin", 1, [{"question": "Loss ratio?"}])
        {'inserted': 1, 'duplicates': 0, 'invalid': 0, 'total': 1}
    """
    get_pack(conn, pack_id)
    inserted = dup = invalid = 0
    for it in items:
        text = (it.get("question") or "").strip()
        if not text:
            invalid += 1
            continue
        if add_question(
            conn,
            pack_id,
            text,
            it.get("category", ""),
            it.get("source", "imported"),
            actor,
        ):
            inserted += 1
        else:
            dup += 1
    audit(
        conn,
        actor,
        "questions.import",
        f"pack={pack_id} inserted={inserted} dup={dup} invalid={invalid}",
    )
    return {
        "inserted": inserted,
        "duplicates": dup,
        "invalid": invalid,
        "total": len(items),
    }


def list_questions(
    conn, pack_id: int, search: str = "", category: str = "", limit: int = 500
) -> List[Dict]:
    """List question-bank entries with optional search and category filters.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.
        search (str): Substring matched against the question text.
        category (str): Exact category filter.
        limit (int): Maximum number of rows. Defaults to 500.

    Returns:
        List[Dict]: Question rows, canonical entries first.

    Example:
        >>> list_questions(conn, 1, category="ratio")[0]["question"]
        'What is the loss ratio?'
    """
    sql = "SELECT * FROM question_bank WHERE pack_id=?"
    args: List[Any] = [pack_id]
    if search:
        sql += " AND question LIKE ?"
        args.append(f"%{search}%")
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY source DESC, id LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def delete_question(conn, actor, qid: int) -> None:
    """Delete a question-bank entry.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        qid (int): Primary key of the ``question_bank`` row.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: When the question does not exist (404).

    Example:
        >>> delete_question(conn, "admin", 42)
    """
    cur = conn.execute("DELETE FROM question_bank WHERE id=?", (qid,))
    if cur.rowcount == 0:
        raise AdminValueError("Question not found.", 404)
    audit(conn, actor, "question.delete", f"id={qid}")


# ---------------------------------------------------------------------------
# Instance data: tables / columns / datasets / rows
# ---------------------------------------------------------------------------


def list_tables(conn, pack_id: int) -> List[Dict]:
    """List pack tables together with their columns and dataset summaries.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.

    Returns:
        List[Dict]: Table rows plus ``columns`` and ``datasets`` lists.

    Example:
        >>> list_tables(conn, 1)[0]["key"]
        'experience'
    """
    out = []
    for t in conn.execute(
        "SELECT * FROM pack_tables WHERE pack_id=? ORDER BY id", (pack_id,)
    ):
        d = dict(t)
        d["columns"] = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id",
                (t["id"],),
            )
        ]
        d["datasets"] = [
            dict(d2)
            for d2 in conn.execute(
                "SELECT id,name,filename,row_count,kind,created_at FROM datasets WHERE table_id=? "
                "ORDER BY id",
                (t["id"],),
            )
        ]
        out.append(d)
    return out


def create_dataset(
    conn,
    actor,
    table_id: int,
    name: str,
    filename: str,
    rows: List[Dict[str, Any]],
    kind: str = "upload",
    activate: bool = False,
) -> int:
    """Insert a dataset (and its rows) for an existing pack table.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        table_id (int): Primary key of the target ``pack_tables`` row.
        name (str): Dataset display name.
        filename (str): Originating file name.
        rows (List[Dict[str, Any]]): Rows to store, in order.
        kind (str): Dataset kind, ``"upload"`` or ``"seed"``.
        activate (bool): Make the new dataset the active one.

    Returns:
        int: Primary key of the new dataset.

    Raises:
        AdminValueError: With status 404 when the target table is missing.

    Example:
        >>> create_dataset(conn, "admin", 3, "FY24", "fy24.xlsx", rows)
        9
    """
    t = conn.execute("SELECT * FROM pack_tables WHERE id=?", (table_id,)).fetchone()
    if t is None:
        raise AdminValueError("Target table not found.", 404)
    cur = conn.execute(
        "INSERT INTO datasets(pack_id,table_id,name,filename,row_count,kind,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (t["pack_id"], table_id, name, filename, len(rows), kind, now()),
    )
    did = int(cur.lastrowid)
    # One executemany beats a per-row execute loop by an order of magnitude
    # on large workbooks — the full-data upload the user complained about.
    conn.executemany(
        "INSERT INTO pack_rows(dataset_id,seq,data) VALUES(?,?,?)",
        (
            (did, seq, json.dumps(row, ensure_ascii=False))
            for seq, row in enumerate(rows)
        ),
    )
    if activate or t["active_dataset_id"] is None:
        conn.execute(
            "UPDATE pack_tables SET active_dataset_id=? WHERE id=?", (did, table_id)
        )
    audit(conn, actor, "dataset.create", f"table={table_id} id={did} rows={len(rows)}")
    return did


def list_datasets(conn, table_id: int) -> List[Dict]:
    """List the datasets belonging to a pack table.

    Args:
        conn (sqlite3.Connection): Open database connection.
        table_id (int): Primary key of the ``pack_tables`` row.

    Returns:
        List[Dict]: Dataset rows ordered by id.

    Example:
        >>> list_datasets(conn, 3)[0]["name"]
        'FY24'
    """
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM datasets WHERE table_id=? ORDER BY id", (table_id,)
        )
    ]


def set_active_dataset(conn, actor, table_id: int, dataset_id: int) -> None:
    """Mark one dataset of a table as the active dataset.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        table_id (int): Primary key of the ``pack_tables`` row.
        dataset_id (int): Primary key of the dataset.

    Returns:
        None: The change is committed by the caller.

    Raises:
        AdminValueError: When the dataset does not belong to the table (404).

    Example:
        >>> set_active_dataset(conn, "admin", 3, 9)
    """
    if not conn.execute(
        "SELECT 1 FROM datasets WHERE id=? AND table_id=?", (dataset_id, table_id)
    ).fetchone():
        raise AdminValueError("Dataset does not belong to this table.", 404)
    conn.execute(
        "UPDATE pack_tables SET active_dataset_id=? WHERE id=?", (dataset_id, table_id)
    )
    audit(conn, actor, "dataset.activate", f"table={table_id} dataset={dataset_id}")


def delete_dataset(conn, actor, dataset_id: int) -> None:
    """Delete a dataset and promote the newest remaining dataset.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        dataset_id (int): Primary key of the dataset.

    Returns:
        None: The deletion is committed by the caller.

    Raises:
        AdminValueError: With status 404 when the dataset does not exist.

    Example:
        >>> delete_dataset(conn, "admin", 9)
    """
    d = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if d is None:
        raise AdminValueError("Dataset not found.", 404)
    conn.execute(
        "UPDATE pack_tables SET active_dataset_id=NULL WHERE active_dataset_id=?",
        (dataset_id,),
    )
    conn.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
    audit(conn, actor, "dataset.delete", f"id={dataset_id}")
    # Promote the newest remaining dataset so the table stays usable.
    nxt = conn.execute(
        "SELECT id FROM datasets WHERE table_id=? ORDER BY id DESC LIMIT 1",
        (d["table_id"],),
    ).fetchone()
    if nxt:
        conn.execute(
            "UPDATE pack_tables SET active_dataset_id=? WHERE id=?",
            (nxt["id"], d["table_id"]),
        )


def table_rows(
    conn,
    table_id: int,
    dataset_id: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read a page of rows from a table's dataset.

    Args:
        conn (sqlite3.Connection): Open database connection.
        table_id (int): Primary key of the ``pack_tables`` row.
        dataset_id (Optional[int]): Dataset to read; defaults to the table's
            active dataset.
        limit (int): Maximum rows returned. Defaults to 200.
        offset (int): Rows to skip. Defaults to 0.

    Returns:
        Dict[str, Any]: ``dataset_id``, ``total`` and the decoded ``rows``.

    Raises:
        AdminValueError: With status 404 when the table does not exist.

    Example:
        >>> table_rows(conn, 3, limit=10)["total"]
        42
    """
    t = conn.execute("SELECT * FROM pack_tables WHERE id=?", (table_id,)).fetchone()
    if t is None:
        raise AdminValueError("Table not found.", 404)
    did = dataset_id or t["active_dataset_id"]
    if did is None:
        return {"dataset_id": None, "total": 0, "rows": []}
    total = conn.execute(
        "SELECT COUNT(*) n FROM pack_rows WHERE dataset_id=?", (did,)
    ).fetchone()["n"]
    rows = [
        json.loads(r["data"])
        for r in conn.execute(
            "SELECT data FROM pack_rows WHERE dataset_id=? ORDER BY seq,id LIMIT ? OFFSET ?",
            (did, limit, offset),
        )
    ]
    return {"dataset_id": did, "total": total, "rows": rows}


def table_columns(conn, table_id: int) -> List[Dict[str, Any]]:
    """Return the ordered column metadata of a pack table.

    Args:
        conn (sqlite3.Connection): Open database connection.
        table_id (int): Primary key of the ``pack_tables`` row.

    Returns:
        List[Dict[str, Any]]: Column rows ordered by ``sort`` then id.

    Example:
        >>> table_columns(conn, 3)[0]["key"]
        'policy_id'
    """
    return [
        dict(c)
        for c in conn.execute(
            "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort,id",
            (table_id,),
        ).fetchall()
    ]


def find_table_by_name(conn, pack_id: int, name: str) -> Optional[Dict[str, Any]]:
    """Route an uploaded item (file stem / sheet name) to a pack table.

    Matching order: exact key, then token-set equality on the key or the
    normalised label, then any token overlap. Tokenisation reuses
    :func:`ontology_service._key_tokens`, so e.g. sheet
    ``Monthly_Experience`` still routes to the table keyed ``experience``.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.
        name (str): File stem or sheet name to match.

    Returns:
        Optional[Dict[str, Any]]: The table row (as produced by
            :func:`list_tables`), or ``None`` when nothing matches.

    Example:
        >>> find_table_by_name(conn, 1, "Monthly_Experience")["key"]
        'experience'
    """
    wanted = str(name or "").strip()
    if not wanted:
        return None
    tables = list_tables(conn, pack_id)
    if not tables:
        return None
    for t in tables:
        if t["key"] == wanted:
            return t
    want_tokens = set(svc._key_tokens(wanted))
    if not want_tokens:
        return None
    fallback = None
    for t in tables:
        for cand in (t["key"], t.get("label", "")):
            cand_tokens = set(svc._key_tokens(cand))
            if cand_tokens and cand_tokens == want_tokens:
                return t
            if fallback is None and want_tokens & cand_tokens:
                fallback = t
    return fallback


def create_table(
    conn,
    actor,
    pack_id: int,
    key: str,
    label: str,
    entity_key: str,
    description: str,
    columns: List[Dict[str, Any]],
) -> int:
    """Create a pack table and its columns from an inferred structure.

    Mirrors the inline ``INSERT INTO pack_tables/pack_columns`` used by
    :func:`import_pack`. The caller is responsible for the matching ontology
    entity/attributes and for syncing the visual graph afterwards.

    Args:
        conn (sqlite3.Connection): Open database connection.
        actor (str): Username recorded in the audit log.
        pack_id (int): Primary key of the pack.
        key (str): Table key, unique within the pack.
        label (str): Display label; falls back to ``key``.
        entity_key (str): Ontology entity the table represents.
        description (str): Optional description.
        columns (List[Dict[str, Any]]): Column definitions; each needs ``key``.

    Returns:
        int: Primary key of the new table.

    Raises:
        AdminValueError: When the pack is missing, ``key`` is empty, or the
            table key already exists (409).

    Example:
        >>> create_table(conn, "admin", 1, "experience", "Experience", "policy", "", cols)
        4
    """
    get_pack(conn, pack_id)
    if not key:
        raise AdminValueError("Table key is required.")
    if conn.execute(
        "SELECT 1 FROM pack_tables WHERE pack_id=? AND key=?", (pack_id, key)
    ).fetchone():
        raise AdminValueError(f"Table '{key}' already exists in this pack.", 409)
    tid = int(
        conn.execute(
            "INSERT INTO pack_tables(pack_id,key,label,entity_key,description) "
            "VALUES(?,?,?,?,?)",
            (pack_id, key, label or key, entity_key or "", description or ""),
        ).lastrowid
    )
    for i, c in enumerate(columns):
        conn.execute(
            "INSERT INTO pack_columns(table_id,key,label,data_type,unit,enum_values,"
            "expression,fmt,decimals,required,sort) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                c["key"],
                c.get("label") or c["key"],
                c.get("data_type", "STRING"),
                c.get("unit", ""),
                c.get("enum_values", ""),
                c.get("expression", ""),
                c.get("fmt", "raw"),
                c.get("decimals", 2),
                1 if c.get("required") else 0,
                c.get("sort", i),
            ),
        )
    audit(
        conn, actor, "table.create", f"pack={pack_id} key={key} columns={len(columns)}"
    )
    bump_pack_version(conn, actor, pack_id)
    return tid


def list_audit(conn, limit: int = 100) -> List[Dict[str, Any]]:
    """Return the most recent audit-trail events.

    Args:
        conn (sqlite3.Connection): Open database connection.
        limit (int): Maximum number of rows, capped at 500. Defaults to 100.

    Returns:
        List[Dict[str, Any]]: ``ts``, ``actor``, ``action``, ``detail`` and
            ``ip`` per event, newest first.

    Example:
        >>> list_audit(conn, 5)[0]["action"]
        'pack.activate'
    """
    rows = conn.execute(
        "SELECT ts, username AS actor, action, detail, ip FROM audit_log "
        "ORDER BY id DESC LIMIT ?",
        (min(limit, 500),),
    ).fetchall()
    return [dict(r) for r in rows]


def instance_graph(
    conn,
    pack_id: int,
    *,
    node_limit: int = 300,
    entity_filter: str = "",
    search: str = "",
) -> Dict[str, Any]:
    """Build the two-layer instance graph of a pack.

    Schema layer: entities + relations. Instance nodes: one per data row
    (capped, with search/type filter). Instance edges: resolved from entity
    relations whose source_attr/target_attr match table columns.

    Args:
        conn (sqlite3.Connection): Open database connection.
        pack_id (int): Primary key of the pack.
        node_limit (int): Maximum number of instance nodes. Defaults to 300.
        entity_filter (str): Only include tables of this entity key.
        search (str): Case-insensitive substring filter on row content.

    Returns:
        Dict[str, Any]: ``schema`` (nodes/edges) and ``instances`` (nodes,
            edges, ``total_rows``, ``truncated``, ``node_limit``).

    Example:
        >>> instance_graph(conn, 1, node_limit=50)["instances"]["truncated"]
        True
    """
    schema_nodes = []
    for e in conn.execute(
        "SELECT key,label_en,layer FROM entities WHERE pack_id=? ORDER BY sort,id",
        (pack_id,),
    ):
        schema_nodes.append(
            {
                "id": f"schema:{e['key']}",
                "kind": "entity",
                "key": e["key"],
                "label": e["label_en"],
                "layer": e["layer"],
            }
        )
    schema_edges = []
    for r in conn.execute(
        "SELECT key,source_key,target_key,source_attr,target_attr,cardinality FROM relations "
        "WHERE pack_id=? ORDER BY sort,id",
        (pack_id,),
    ):
        schema_edges.append(
            {
                "id": f"schema:{r['key']}",
                "source": f"schema:{r['source_key']}",
                "target": f"schema:{r['target_key']}",
                "label": r["key"],
                "source_attr": r["source_attr"],
                "target_attr": r["target_attr"],
                "cardinality": r["cardinality"],
            }
        )

    tables = {
        t["key"]: dict(t)
        for t in conn.execute("SELECT * FROM pack_tables WHERE pack_id=?", (pack_id,))
    }
    # Collect row nodes.
    instance_nodes: List[Dict[str, Any]] = []
    # table_key -> {join_value: node_id}
    value_index: Dict[str, Dict[str, str]] = {}
    truncated = False
    total_rows = 0
    for tkey, t in tables.items():
        if entity_filter and t["entity_key"] != entity_filter:
            continue
        did = t["active_dataset_id"]
        if not did:
            continue
        for r in conn.execute(
            "SELECT data FROM pack_rows WHERE dataset_id=? ORDER BY seq,id", (did,)
        ):
            total_rows += 1
            if len(instance_nodes) >= node_limit:
                truncated = True
                continue
            data = json.loads(r["data"])
            label = str(
                data.get("name")
                or data.get("label")
                or data.get("id")
                or data.get("code")
                or data.get("key")
                or ""
            )[:48]
            if (
                search
                and search.lower()
                not in (label + " " + json.dumps(data, ensure_ascii=False)).lower()
            ):
                continue
            nid = f"inst:{tkey}:{len(instance_nodes)}"
            instance_nodes.append(
                {
                    "id": nid,
                    "kind": "instance",
                    "table": tkey,
                    "entity": t["entity_key"],
                    "label": label or t["label"],
                    "data": data,
                }
            )
            value_index.setdefault(tkey, {})
            for v in data.values():
                if isinstance(v, (str, int, float)):
                    value_index[tkey].setdefault(str(v), nid)

    # Resolve instance edges via entity relations + matched attr columns.
    instance_edges: List[Dict[str, Any]] = []
    seen_edges = set()
    for r in conn.execute(
        "SELECT * FROM relations WHERE pack_id=? AND source_attr<>'' AND target_attr<>''",
        (pack_id,),
    ):
        src_tables = [t for t in tables.values() if t["entity_key"] == r["source_key"]]
        tgt_tables = [t for t in tables.values() if t["entity_key"] == r["target_key"]]
        for st in src_tables:
            for tt in tgt_tables:
                src_rows = [n for n in instance_nodes if n["table"] == st["key"]]
                tgt_idx = value_index.get(tt["key"], {})
                for n in src_rows:
                    val = n["data"].get(r["source_attr"])
                    if val is None:
                        continue
                    target = tgt_idx.get(str(val))
                    if target:
                        sig = (n["id"], target, r["key"])
                        if sig not in seen_edges:
                            seen_edges.add(sig)
                            instance_edges.append(
                                {
                                    "source": n["id"],
                                    "target": target,
                                    "label": r["key"],
                                    "relation": r["key"],
                                }
                            )
    return {
        "schema": {"nodes": schema_nodes, "edges": schema_edges},
        "instances": {
            "nodes": instance_nodes,
            "edges": instance_edges,
            "total_rows": total_rows,
            "truncated": truncated,
            "node_limit": node_limit,
        },
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def dashboard_stats(conn) -> Dict[str, Any]:
    """Aggregate platform statistics for the admin dashboard.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        Dict[str, Any]: Global counts, active-pack counts, 7-day LLM call
            totals, 30-day LLM usage by model and user, user activity,
            security signals and database usage.

    Raises:
        sqlite3.DatabaseError: When a required query fails.

    Example:
        >>> dashboard_stats(conn)["users"]
        3
    """

    def n(sql, params=()):
        """Count the rows produced by a single-value query.

        Args:
            sql (str): SELECT statement returning one numeric column.
            params (tuple): Query parameters. Defaults to ``()``.

        Returns:
            Any: The first column of the first result row.

        Example:
            >>> n("SELECT COUNT(*) FROM users")
            3
        """
        return conn.execute(sql, params).fetchone()[0]

    pid = active_pack_id(conn)
    stats = {
        "packs": n("SELECT COUNT(*) FROM ontology_packs"),
        "active_pack_id": pid,
        "users": n("SELECT COUNT(*) FROM users"),
        "admins": n("SELECT COUNT(*) FROM users WHERE role='admin'"),
        "model_configs": n("SELECT COUNT(*) FROM model_configs"),
        "reports": n("SELECT COUNT(*) FROM reports"),
        "audit_events": n("SELECT COUNT(*) FROM audit_log"),
    }
    if pid:
        stats.update(
            {
                "entities": n("SELECT COUNT(*) FROM entities WHERE pack_id=?", (pid,)),
                "attributes": n(
                    "SELECT COUNT(*) FROM attributes WHERE pack_id=?", (pid,)
                ),
                "relations": n(
                    "SELECT COUNT(*) FROM relations WHERE pack_id=?", (pid,)
                ),
                "nodes": n("SELECT COUNT(*) FROM graph_nodes WHERE pack_id=?", (pid,)),
                "edges": n("SELECT COUNT(*) FROM graph_edges WHERE pack_id=?", (pid,)),
                "rules": n(
                    "SELECT COUNT(*) FROM question_rules WHERE pack_id=?", (pid,)
                ),
                "questions": n(
                    "SELECT COUNT(*) FROM question_bank WHERE pack_id=?", (pid,)
                ),
                "tables": n("SELECT COUNT(*) FROM pack_tables WHERE pack_id=?", (pid,)),
                "datasets": n("SELECT COUNT(*) FROM datasets WHERE pack_id=?", (pid,)),
                "instance_rows": n(
                    "SELECT COUNT(*) FROM pack_rows JOIN datasets d ON d.id=pack_rows.dataset_id "
                    "WHERE d.pack_id=?",
                    (pid,),
                ),
            }
        )
        pack = conn.execute(
            "SELECT name FROM ontology_packs WHERE id=?", (pid,)
        ).fetchone()
        stats["active_pack_name"] = pack["name"] if pack else ""
    stats["llm_calls_7d"] = conn.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE ts >= datetime('now','-7 days')"
    ).fetchone()[0]
    stats["llm_failures_7d"] = conn.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE success=0 AND ts >= datetime('now','-7 days')"
    ).fetchone()[0]

    # --- AI model usage & token consumption (last 30 days) --------------------
    agg = conn.execute(
        "SELECT COUNT(*) calls, COALESCE(SUM(tokens),0) tokens, "
        "COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0) failures, "
        "COALESCE(ROUND(AVG(latency_ms)),0) avg_latency "
        "FROM llm_calls WHERE ts >= datetime('now','-30 days')"
    ).fetchone()
    by_model = conn.execute(
        "SELECT model, COUNT(*) calls, COALESCE(SUM(tokens),0) tokens, "
        "COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0) failures "
        "FROM llm_calls WHERE ts >= datetime('now','-30 days') "
        "GROUP BY model ORDER BY tokens DESC"
    ).fetchall()
    by_user = conn.execute(
        "SELECT COALESCE(u.username,'(unknown)') username, COUNT(*) calls, "
        "COALESCE(SUM(c.tokens),0) tokens, COALESCE(ROUND(AVG(c.latency_ms)),0) avg_latency_ms "
        "FROM llm_calls c LEFT JOIN users u ON u.id=c.user_id "
        "WHERE c.ts >= datetime('now','-30 days') "
        "GROUP BY c.user_id ORDER BY tokens DESC LIMIT 10"
    ).fetchall()
    stats["llm_usage"] = {
        "window": "30 days",
        "calls": agg["calls"],
        "tokens": agg["tokens"],
        "failures": agg["failures"],
        "avg_latency_ms": agg["avg_latency"],
        "by_model": [dict(r) for r in by_model],
        "by_user": [dict(r) for r in by_user],
    }

    # --- user access / activity -------------------------------------------------
    users_rows = conn.execute(
        "SELECT id, username, display_name, role, enabled, last_login, login_count, "
        "created_at FROM users ORDER BY login_count DESC, last_login DESC"
    ).fetchall()
    stats["users_detail"] = [
        {
            "id": r["id"],
            "username": r["username"],
            "display_name": r["display_name"],
            "role": r["role"],
            "enabled": bool(r["enabled"]),
            "last_login": r["last_login"],
            "login_count": r["login_count"],
            "created_at": r["created_at"],
        }
        for r in users_rows
    ]
    # --- anomaly / security signals --------------------------------------------
    failed_24h = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='auth.login_failed' "
        "AND ts >= datetime('now','-1 day')"
    ).fetchone()[0]
    failed_7d = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='auth.login_failed' "
        "AND ts >= datetime('now','-7 days')"
    ).fetchone()[0]
    # top usernames with failed logins (potential brute-force targets)
    brute_force = conn.execute(
        "SELECT username, COUNT(*) n FROM audit_log "
        "WHERE action='auth.login_failed' AND ts >= datetime('now','-7 days') "
        "GROUP BY username ORDER BY n DESC LIMIT 5"
    ).fetchall()
    stats["security"] = {
        "failed_logins_24h": failed_24h,
        "failed_logins_7d": failed_7d,
        "brute_force_targets": [
            {"username": r["username"], "count": r["n"]} for r in brute_force
        ],
    }
    # --- database usage --------------------------------------------------------
    try:
        from modules.db import DB_PATH

        db_size_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        db_size_kb = round(db_size_bytes / 1024, 1)
    except Exception:
        db_size_kb = 0
    table_counts = {}
    for t in [
        "users",
        "sessions",
        "ontology_packs",
        "graph_nodes",
        "graph_edges",
        "entities",
        "attributes",
        "relations",
        "question_rules",
        "question_bank",
        "datasets",
        "pack_rows",
        "reports",
        "audit_log",
        "llm_calls",
    ]:
        try:
            table_counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            table_counts[t] = 0
    stats["db_usage"] = {
        "db_size_kb": db_size_kb,
        "table_counts": table_counts,
    }
    return stats
