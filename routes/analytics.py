"""HTTP routes for the analytics area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends

from modules import analytics
from modules import auth
from modules import ontology_service as svc
from modules.db import get_conn

from routes.common import user_pack_id

router = APIRouter()


@router.get("/api/analytics/timeseries")
async def api_timeseries(user: Dict[str, Any] = Depends(auth.current_user)):
    """Monthly trend series for the active pack.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"months", "labels", "company", "channels", "generic",
        "requirements"}``; empty series when no pack is active.
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"months": [], "company": [], "channels": [], "requirements": None}
    data = svc.pack_tables(pack)
    company = analytics.company_series(data)
    return {
        "months": [c["month"] for c in company],
        "labels": [c["label"] for c in company],
        "company": company,
        "channels": analytics.channel_series(data),
        # Fallback series built from whatever monthly table THIS pack has, so a
        # non-VNB/APE ontology (a lapse study, for example) still gets a trend.
        "generic": analytics.generic_monthly(data),
        "requirements": analytics.requirements(data),
    }


@router.get("/api/analytics/attribution")
async def api_attribution(
    month: Optional[str] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Month-over-month attribution breakdown.

    Args:
        month (Optional[str]): Month label to analyse; defaults to the latest.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"attribution": {...} | None}`` (``None`` when no pack is active).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"attribution": None}
    return {"attribution": analytics.mom_attribution(svc.pack_tables(pack), month)}


@router.get("/api/analytics/scenario")
async def api_scenario(
    multiplier: float = 2.0,
    planned_active: Optional[int] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Interactive what-if scenario (e.g. volume multiplier).

    Args:
        multiplier (float): Scale factor applied to the baseline (default 2.0).
        planned_active (Optional[int]): Override for planned active policies.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"scenario": {...} | None}`` (``None`` when no pack is active).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"scenario": None}
    return {
        "scenario": analytics.run_scenario(
            svc.pack_tables(pack), multiplier=multiplier, planned_active=planned_active
        )
    }


@router.get("/api/analytics/agent-stats")
async def api_agent_stats(user: Dict[str, Any] = Depends(auth.current_user)):
    """Agent utilisation statistics for the active pack.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"agents": {...} | None}`` (``None`` when no pack is active).
    """
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"agents": None}
    return {"agents": analytics.agent_stats(svc.pack_tables(pack))}
