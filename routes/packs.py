"""HTTP routes for the packs area."""

from __future__ import annotations

import re

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import admin_service as adm
from modules import analytics
from modules import auth
from modules import ontology_service as svc
from modules.db import audit
from modules.db import get_conn
from modules.db import set_setting
from modules.seed import PACK_SLUG as SAMPLE_PACK_SLUG

from routes.common import _ensure_auto_report
from routes.common import pack_data_sources
from routes.common import resolve_pack
from routes.common import user_pack_id

router = APIRouter()


@router.get("/api/packs")
async def list_packs(user: Dict[str, Any] = Depends(auth.current_user)):
    """Scenario picker data: every ontology pack with its provenance.

    Lets the user switch the working scenario from the portal header.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"packs": [{"id", "name", "description", "origin",
        "created_at", "entities", "datasets", "is_active", "selected"}],
        "selected_id": int | None}``.
    """
    with get_conn() as conn:
        chosen = user_pack_id(conn, user)
        active = adm.active_pack_id(conn)
        out = []
        for r in conn.execute(
            "SELECT p.id, p.slug, p.name, p.description, p.origin, p.is_builtin, "
            "p.created_at, "
            "(SELECT COUNT(*) FROM entities e WHERE e.pack_id=p.id) AS entities, "
            "(SELECT COUNT(*) FROM datasets d WHERE d.pack_id=p.id) AS datasets "
            "FROM ontology_packs p ORDER BY p.id"
        ).fetchall():
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "origin": r["origin"]
                    or (
                        "Built-in sample pack"
                        if (r["is_builtin"] or r["slug"] == SAMPLE_PACK_SLUG)
                        else ""
                    ),
                    "created_at": r["created_at"],
                    "entities": r["entities"],
                    "datasets": r["datasets"],
                    "is_active": r["id"] == active,
                    "selected": r["id"] == chosen,
                }
            )
    return {"packs": out, "selected_id": chosen}


class MyPackIn(BaseModel):
    """Request body for the per-user scenario selection.

    Attributes:
        pack_id (Optional[int]): Pack to select; ``None`` resets to the globally
            active pack.
    """

    pack_id: Optional[int] = None


@router.post("/api/my/pack")
async def choose_pack(
    data: MyPackIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Persist the user's scenario selection (survives reloads).

    Passing ``pack_id=null`` falls back to the globally active pack.

    Args:
        data (MyPackIn): Requested pack id (or ``None`` to reset).
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true, "pack_id": int}``; 404 JSON when the
        pack does not exist.

    Example:
        >>> choose_pack(MyPackIn(pack_id=3), user)
        {'ok': True, 'pack_id': 3}
    """
    with get_conn() as conn:
        if data.pack_id is None:
            set_setting(conn, f"user_pack:{user['id']}", "")
            audit(conn, user["username"], "pack.reset", "back to system default")
            return {"ok": True, "pack_id": adm.active_pack_id(conn)}
        if not conn.execute(
            "SELECT 1 FROM ontology_packs WHERE id=?", (data.pack_id,)
        ).fetchone():
            return JSONResponse(status_code=404, content={"error": "Unknown pack."})
        set_setting(conn, f"user_pack:{user['id']}", str(data.pack_id))
        name = conn.execute(
            "SELECT name FROM ontology_packs WHERE id=?", (data.pack_id,)
        ).fetchone()["name"]
        audit(
            conn,
            user["username"],
            "pack.select",
            f"pack={data.pack_id} ({name})",
        )
    return {"ok": True, "pack_id": data.pack_id}


@router.get("/api/dashboard")
async def dashboard_data(user: Dict[str, Any] = Depends(auth.current_user)):
    """Everything the Dashboard needs in one call.

    Returns the scenario banner (which pack, from what data), computed KPIs and
    the monthly timeseries.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"pack": {"id", "name", "description", "narrative"} | None,
        "sources": [...], "kpis": [...], "formula_count": int,
        "timeseries": {"months", "labels", "company"} | None,
        "auto_report": {"uid", "title", "created_at"} | None}``.
    """
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if pack is None:
            return {"pack": None, "sources": [], "kpis": [], "timeseries": None}
        sources = pack_data_sources(conn, pack["id"])
        auto = _ensure_auto_report(conn, user, pack)
    computed = svc.compute_pack(pack, strict=False)
    metrics = computed.get("metrics") or {}
    formulas = pack.get("formulas", [])

    # KPI cards are driven by whatever metric formulas this ontology declares,
    # so any pack (VNB/APE, lapse experience, motor, …) shows its own computed
    # metrics — not a fixed VNB/APE key list. A formula whose input table has no
    # rows evaluates to None and is left out of the cards.
    kpis: List[Dict[str, Any]] = []
    for f in formulas:
        val = metrics.get(f["key"])
        if val is None:
            continue
        expr = f.get("expression") or ""
        kpis.append(
            {
                "key": f["key"],
                "label": f.get("label") or f["key"].replace("_", " ").title(),
                "value": svc._metric_display(f["key"], val, formulas),
                "unit": f.get("unit") or "",
                # Exposed so a card can explain itself: the formula text and the
                # instance table(s) it reads, i.e. rows('…').
                "definition": expr,
                "source_tables": sorted(
                    set(re.findall(r"rows\(\s*'([^']+)'\s*\)", expr))
                ),
                "ok": True,
            }
        )
    data = svc.pack_tables(pack)
    company = analytics.company_series(data)
    # Always-available cards: latest-month totals straight from the monthly
    # series (the pack's own declared metrics may reference tables that are
    # empty — e.g. the stated/derived gap pair).
    latest = company[-1] if company else None
    if latest is not None:
        prev = company[-2] if len(company) > 1 else None
        mom = None
        if prev and prev.get("total_vnb"):
            mom = (latest.get("total_vnb") or 0) / prev["total_vnb"] - 1
        fnum = lambda v, d=3: ("—" if v is None else f"{v:,.{d}f}")  # noqa: E731
        latest_kpis = [
            {
                "key": "latest_ape",
                "label": f"Total APE ({latest['label']})",
                "value": fnum(latest.get("total_ape")),
                "unit": "$M",
                "ok": True,
            },
            {
                "key": "latest_vnb",
                "label": f"Total VNB ({latest['label']})",
                "value": fnum(latest.get("total_vnb")),
                "unit": "$M",
                "ok": True,
            },
            {
                "key": "latest_margin",
                "label": f"VNB Margin ({latest['label']})",
                "value": (
                    "—"
                    if latest.get("margin") is None
                    else f"{latest['margin'] * 100:.2f}%"
                ),
                "unit": "",
                "ok": True,
            },
            {
                "key": "latest_mom",
                "label": "VNB MoM change",
                "value": ("—" if mom is None else f"{mom * 100:+.1f}%"),
                "unit": "",
                "ok": True,
            },
        ]
        have = {k["key"] for k in kpis}
        kpis += [k for k in latest_kpis if k["key"] not in have]
    return {
        "pack": {
            "id": pack["id"],
            "name": pack["name"],
            "description": pack.get("description", ""),
            "narrative": pack.get("narrative", ""),
        },
        "sources": sources,
        "kpis": [k for k in kpis if k["ok"]],
        "formula_count": len(formulas),
        "timeseries": {
            "months": [c["month"] for c in company],
            "labels": [c["label"] for c in company],
            "company": company,
        },
        "auto_report": auto,
    }
