"""Shared seed helper for verify_t7 / verify_t8.

The ``lapse_experience_synthetic_demo`` pack is a *test fixture*, not
product content — it is no longer shipped in the database. Both tests call
:func:`ensure_synthetic_pack` instead: when the pack is missing it is
created on the fly from ``data/Lapse_mockdata_v3 - Scenario Demo.xlsx``
(the real Scenario-Demo workbook the client uses) through the platform's
own admin APIs (create pack -> batch-import every sheet as its own table),
and the ``Monthly_Experience`` table is renamed to the canonical key
``experience`` that the routing assertions expect. The rename is applied
consistently to entities, attributes and graph nodes/edges.

Callers receive ``(pack_id, created_by_helper)`` and should delete the pack
afterwards only when they created it, so a developer's own seeded pack is
never removed.
"""

from __future__ import annotations

from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8010"
ADMIN = {"username": "admin", "password": "Onti@2026"}
PACK_SLUG = "lapse_experience_synthetic_demo"
PACK_NAME = "Lapse Experience Synthetic Demo"
# Real Scenario-Demo workbook, NOT the legacy SYNTHETIC_DEMO.xlsx.
XLSX = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "Lapse_mockdata_v3 - Scenario Demo.xlsx"
)

from modules.db import get_conn  # noqa: E402


def ensure_synthetic_pack(base: str = BASE) -> tuple[int, bool]:
    """Return ``(pack_id, created)`` — creating the pack when missing."""
    with httpx.Client(base_url=base, timeout=120) as c:
        c.post("/api/auth/login", json=ADMIN).raise_for_status()
        packs = c.get("/api/admin/packs").json().get("packs", [])
        pk = next((p for p in packs if p["slug"] == PACK_SLUG), None)
        if pk:
            return int(pk["id"]), False

        pid = int(
            c.post(
                "/api/admin/packs",
                json={
                    "slug": PACK_SLUG,
                    "name": PACK_NAME,
                    "description": "Throwaway fixture for verify_t7/t8.",
                },
            )
            .raise_for_status()
            .json()["id"]
        )
        mime = "application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"
        resp = c.post(
            "/api/admin/data/batch-upload",
            data={"pack_id": str(pid), "commit": "true"},
            files={"files": (XLSX.name, XLSX.read_bytes(), mime)},
        )
        resp.raise_for_status()

    # Canonical routing key: sheet Monthly_Experience must hit the table
    # keyed 'experience' (the tests assert this exact target).
    with get_conn() as conn:
        old = "monthly_experience"
        new = "experience"
        for sql, params in (
            (
                "UPDATE pack_tables SET key=?, label='Monthly Experience' "
                "WHERE pack_id=? AND key=?",
                (new, pid, old),
            ),
            ("UPDATE entities SET key=? WHERE pack_id=? AND key=?", (new, pid, old)),
            (
                "UPDATE attributes SET entity_key=? WHERE pack_id=? AND entity_key=?",
                (new, pid, old),
            ),
            ("UPDATE graph_nodes SET key=? WHERE pack_id=? AND key=?", (new, pid, old)),
            (
                "UPDATE graph_edges SET source_key=? WHERE pack_id=? AND source_key=?",
                (new, pid, old),
            ),
            (
                "UPDATE graph_edges SET target_key=? WHERE pack_id=? AND target_key=?",
                (new, pid, old),
            ),
        ):
            conn.execute(sql, params)
    return pid, True


def delete_pack(base: str = BASE, pack_id: int = 0) -> None:
    """Remove a helper-created pack (admin API, cascades its content)."""
    with httpx.Client(base_url=base, timeout=60) as c:
        c.post("/api/auth/login", json=ADMIN).raise_for_status()
        c.delete(f"/api/admin/packs/{pack_id}").raise_for_status()
