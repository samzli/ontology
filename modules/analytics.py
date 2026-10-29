"""Time-series, attribution, scenario and agent analytics.

Pure functions over the ontology's instance tables (lists of row dicts).
Nothing here touches the database or FastAPI — the service layer
(``ontology_service``) and the API pass in the computed pack tables and render
the results.

The analytics activate when the active pack carries monthly tables
(``vnb_monthly``, ``channel_monthly``, ``agent_monthly``, ...); otherwise the
functions simply return no data and the UI shows an empty-state message.

Identity used for month-on-month decomposition (per channel)::

    VNB ($M) = active_agents x avg_productivity ($k) / 1000 x weighted_VNB_margin

so a change in VNB splits, without an unexplained residual, into:

* **active-agent effect**  (A1-A0) x P0/1000 x M0
* **productivity effect**  A1 x (P1-P0)/1000 x M0
* **mix/margin effect**    A1 x P1/1000 x (M1-M0)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


AGENCY_CHANNEL = "CH001"
NON_AGENCY_CHANNELS = ("CH002", "CH003")
MONTH_FMT = "%Y-%m-%d"

# Table keys this module reads. They are the built-in VNB/APE monthly schema
# (a `month` column plus the channel / agency / margin columns below), so the
# analytics views only light up for packs that carry these tables — other
# ontologies get an explicit "what is missing" message instead.
REQUIRED_TABLES = ("vnb_monthly", "channel_monthly", "agent_monthly", "agent_master")
KEY_COLUMNS = {
    "vnb_monthly": "month",
    "channel_monthly": "month, channel_id",
    "agent_monthly": "month, channel_id",
    "agent_master": "agent_id, channel_id",
}
# Plain-language purpose of each table, so the UI empty-state can tell a user
# what uploading each missing table will actually unlock.
TABLE_PURPOSE = {
    "vnb_monthly": "company-level monthly totals — powers the trend chart and the month-on-month VNB bridge",
    "channel_monthly": "monthly figures per channel — powers the channel breakdown and the driver attribution",
    "agent_monthly": "monthly figures per agent — powers the agent productivity table",
    "agent_master": "the agent roster — supplies names and reporting lines for the agent view",
}


def requirements(data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Report whether this pack can power the analytics views and, if not,
    which of the required monthly tables are missing (used by the UI copy)."""
    present = sorted(data.keys())
    missing = [t for t in REQUIRED_TABLES if not _rows(data, t)]
    return {
        "available": not missing,
        "required_tables": list(REQUIRED_TABLES),
        "key_columns": dict(KEY_COLUMNS),
        "table_purpose": dict(TABLE_PURPOSE),
        "present_tables": present,
        "missing_tables": missing,
    }


# --------------------------------------------------------------------------- #
# Generic monthly series — for packs that are NOT the built-in VNB/APE schema
# --------------------------------------------------------------------------- #
# Any pack with its own monthly table (a `report_month` / `month` column holding
# YYYY-MM values) can still show a month-by-month trend here: the series below is
# built from the pack's own columns, so the page adapts to whatever ontology and
# data are active instead of only lighting up for VNB / APE.
MONTH_COLUMNS = ("report_month", "month", "period_month", "experience_month")
_RATEISH = re.compile(r"(rate|ratio|ppt|pct|percent|share|margin)", re.I)


def _is_month(v: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}", str(v)[:7]))


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _month_source(data: Dict[str, List[Dict[str, Any]]]):
    """The best (table, month_column) pair in this pack, or (None, None).

    Prefers a `report_month` column over a bare `month`, and a table with more
    rows over one with fewer, so the pick is stable and meaningful.
    """
    best = None
    for tname, rows in data.items():
        if not rows:
            continue
        for col in MONTH_COLUMNS:
            vals = [r.get(col) for r in rows[:200] if r.get(col)]
            if not vals or not all(_is_month(v) for v in vals[:50]):
                continue
            rank = (0 if col == "report_month" else 1, -len(rows))
            if best is None or rank < best[0]:
                best = (rank, tname, col)
    return (best[1], best[2]) if best else (None, None)


def generic_monthly(data: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """A month-by-month series over whichever table this pack itself treats as
    monthly, with the numeric columns it can measure. Returns None when the pack
    has no usable monthly table."""
    tname, mcol = _month_source(data)
    if not tname:
        return None
    rows = _rows(data, tname)
    months = sorted({_month_key(r.get(mcol)) for r in rows if r.get(mcol)})
    if len(months) < 2:
        return None
    numeric = []
    for c in rows[0].keys():
        if c == mcol:
            continue
        vals = [r.get(c) for r in rows[:400] if r.get(c) not in (None, "")]
        if vals and all(_is_num(v) for v in vals):
            numeric.append(c)
    measures = [
        {
            "key": c,
            "label": c.replace("_", " "),
            "agg": "mean" if _RATEISH.search(c) else "sum",
        }
        for c in numeric
    ]
    # Annualise a lapse count over exposure when both columns exist, so the
    # trend's headline metric matches the dashboard's "Annualised lapse rate"
    # KPI (12 x lapses / exposure) rather than a raw monthly count.
    exp_col = next((c for c in numeric if "exposure" in c), None)
    lap_col = next((c for c in numeric if "lapse" in c and "rate" not in c), None)
    derived = "_annualised_lapse_rate"
    if exp_col and lap_col:
        measures.insert(
            0,
            {
                "key": derived,
                "label": f"annualised lapse rate ({lap_col} / {exp_col} x 12)",
                "agg": "rate",
            },
        )
    aggs = {m["key"]: m["agg"] for m in measures}
    series = []
    for m in months:
        sel = [r for r in rows if _month_key(r.get(mcol)) == m]
        item: Dict[str, Any] = {
            "month": m,
            "label": month_label(m),
            "records": len(sel),
        }
        for c in numeric:
            vs = [_num(r.get(c)) for r in sel]
            item[c] = (sum(vs) / len(vs)) if (aggs[c] == "mean" and vs) else sum(vs)
        if exp_col and lap_col:
            e = sum(_num(r.get(exp_col)) for r in sel)
            lapse = sum(_num(r.get(lap_col)) for r in sel)
            item[derived] = (lapse / e * 12.0) if e else 0.0
        series.append(item)
    default = next(
        (m["key"] for m in measures if re.search(r"lapse|a_e|rate", m["key"])), None
    )
    return {
        "table": tname,
        "month_column": mcol,
        "months": months,
        "labels": [month_label(m) for m in months],
        "measures": measures,
        "series": series,
        "default_measure": default or (measures[0]["key"] if measures else None),
    }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _num(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _rows(data: Dict[str, List[Dict[str, Any]]], key: str) -> List[Dict[str, Any]]:
    return data.get(key, []) or []


def _month_key(v: Any) -> str:
    """Normalise an Excel/pandas date or 'YYYY-MM-DD' string to YYYY-MM."""
    if v is None:
        return ""
    s = str(v)[:10]
    return s[:7] if len(s) >= 7 else s


def month_label(m: str) -> str:
    """'2025-06' -> 'Jun 2025'."""
    names = [
        "",
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    try:
        y, mo = m.split("-")
        return f"{names[int(mo)]} {y}"
    except (ValueError, IndexError):
        return m


def sorted_months(rows: List[Dict[str, Any]]) -> List[str]:
    months = sorted(
        {_month_key(r.get("month")) for r in rows if r.get("month") is not None}
    )
    return months


# --------------------------------------------------------------------------- #
# Company + channel time series
# --------------------------------------------------------------------------- #
def company_series(data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for r in _rows(data, "vnb_monthly"):
        out.append(
            {
                "month": _month_key(r.get("month")),
                "label": month_label(_month_key(r.get("month"))),
                "total_ape": _num(r.get("total_ape")),
                "total_vnb": _num(r.get("total_vnb")),
                "margin": _num(r.get("total_margin")),
                "agency_ape": _num(r.get("agency_ape")),
                "agency_vnb": _num(r.get("agency_vnb")),
                "agency_share": _num(r.get("agency_share")),
                "mom_vnb": _num(r.get("mom_vnb")),
                "agency_mom": _num(r.get("agency_mom")),
                "agency_contrib": _num(r.get("agency_contrib")),
                "narrative": str(r.get("narrative") or ""),
            }
        )
    out.sort(key=lambda x: x["month"])
    return out


def channel_series(data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for r in _rows(data, "channel_monthly"):
        out.append(
            {
                "month": _month_key(r.get("month")),
                "label": month_label(_month_key(r.get("month"))),
                "channel_id": str(r.get("channel_id") or ""),
                "channel_name": str(r.get("channel_name") or ""),
                "registered": _num(r.get("registered")),
                "active": _num(r.get("active")),
                "active_ratio": _num(r.get("active_ratio")),
                "avg_prod": _num(r.get("avg_prod")),
                "ape": _num(r.get("ape")),
                "margin": _num(r.get("margin")),
                "vnb": _num(r.get("vnb")),
                "mom_vnb": _num(r.get("mom_vnb")),
            }
        )
    out.sort(key=lambda x: (x["month"], x["channel_id"]))
    return out


def _channel_row(
    series: List[Dict[str, Any]], month: str, cid: str
) -> Optional[Dict[str, Any]]:
    for r in series:
        if r["month"] == month and r["channel_id"] == cid:
            return r
    return None


# --------------------------------------------------------------------------- #
# Month-on-month driver attribution
# --------------------------------------------------------------------------- #
def _channel_factor(prev: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, float]:
    """Decompose one channel's delta VNB into agents / productivity / margin.

    VNB = A x P/1000 x M  (sequential factor substitution; effects sum exactly).
    """
    a0, a1 = _num(prev["active"]), _num(cur["active"])
    p0, p1 = _num(prev["avg_prod"]), _num(cur["avg_prod"])
    m0, m1 = _num(prev["margin"]), _num(cur["margin"])
    agent_eff = (a1 - a0) * (p0 / 1000.0) * m0
    prod_eff = a1 * ((p1 - p0) / 1000.0) * m0
    mix_eff = a1 * (p1 / 1000.0) * (m1 - m0)
    return {
        "agent_effect": agent_eff,
        "productivity_effect": prod_eff,
        "mix_effect": mix_eff,
        "delta_vnb": _num(cur["vnb"]) - _num(prev["vnb"]),
        "check": agent_eff + prod_eff + mix_eff,
    }


def mom_attribution(
    data: Dict[str, List[Dict[str, Any]]],
    month: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Attribute the chosen month's VNB movement versus the prior month."""
    cs = channel_series(data)
    comp = company_series(data)
    months = sorted_months(_rows(data, "vnb_monthly"))
    if len(months) < 2:
        return None
    cur_month = month or months[-1]
    if cur_month not in months:
        return None
    idx = months.index(cur_month)
    if idx == 0:
        return None
    prev_month = months[idx - 1]

    channels = sorted({r["channel_id"] for r in cs})
    per_channel = []
    totals = {
        "agent_effect": 0.0,
        "productivity_effect": 0.0,
        "mix_effect": 0.0,
        "delta_vnb": 0.0,
    }
    for cid in channels:
        p = _channel_row(cs, prev_month, cid)
        c = _channel_row(cs, cur_month, cid)
        if not p or not c:
            continue
        f = _channel_factor(p, c)
        per_channel.append(
            {
                "channel_id": cid,
                "channel_name": c["channel_name"],
                "prev_vnb": _num(p["vnb"]),
                "cur_vnb": _num(c["vnb"]),
                **f,
            }
        )
        for k in totals:
            totals[k] += f[k]

    cur_company = next((x for x in comp if x["month"] == cur_month), {})
    prev_company = next((x for x in comp if x["month"] == prev_month), {})
    agency = next((x for x in per_channel if x["channel_id"] == AGENCY_CHANNEL), None)
    return {
        "month": cur_month,
        "label": month_label(cur_month),
        "prev_month": prev_month,
        "prev_label": month_label(prev_month),
        "prev_total_vnb": prev_company.get("total_vnb", 0.0),
        "cur_total_vnb": cur_company.get("total_vnb", 0.0),
        "delta_total_vnb": cur_company.get("total_vnb", 0.0)
        - prev_company.get("total_vnb", 0.0),
        "mom_pct": cur_company.get("mom_vnb", 0.0),
        "narrative": cur_company.get("narrative", ""),
        "factors": totals,
        "channels": per_channel,
        "agency": agency,
    }


# --------------------------------------------------------------------------- #
# Agent-level analytics (latest month)
# --------------------------------------------------------------------------- #
def agent_stats(data: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    monthly = _rows(data, "agent_monthly")
    master = _rows(data, "agent_master")
    if not monthly:
        return None
    months = sorted_months(monthly)
    latest = months[-1]
    cur = [r for r in monthly if _month_key(r.get("month")) == latest]

    def _counts(rows: List[Dict[str, Any]], field: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in rows:
            k = str(r.get(field) or "Unknown")
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    active = [r for r in cur if str(r.get("status")).lower() == "active"]
    by_channel: Dict[str, Dict[str, Any]] = {}
    for r in cur:
        cid = str(r.get("channel_id") or "")
        ch = by_channel.setdefault(
            cid,
            {"channel_id": cid, "registered": 0, "active": 0, "prod_sum": 0.0},
        )
        ch["registered"] += 1
        if str(r.get("status")).lower() == "active":
            ch["active"] += 1
            ch["prod_sum"] += _num(r.get("prod"))
    channel_summary = []
    for cid, ch in by_channel.items():
        channel_summary.append(
            {
                "channel_id": cid,
                "registered": ch["registered"],
                "active": ch["active"],
                "active_ratio": (
                    (ch["active"] / ch["registered"]) if ch["registered"] else 0.0
                ),
                "avg_prod": (ch["prod_sum"] / ch["active"]) if ch["active"] else 0.0,
            }
        )
    channel_summary.sort(key=lambda x: x["channel_id"])

    tenure_buckets = {"0-12 mo": 0, "12-24 mo": 0, "24-48 mo": 0, "48+ mo": 0}
    for r in active:
        t = _num(r.get("tenure"))
        if t < 12:
            tenure_buckets["0-12 mo"] += 1
        elif t < 24:
            tenure_buckets["12-24 mo"] += 1
        elif t < 48:
            tenure_buckets["24-48 mo"] += 1
        else:
            tenure_buckets["48+ mo"] += 1

    return {
        "month": latest,
        "label": month_label(latest),
        "total_registered": len(cur),
        "total_active": len(active),
        "active_ratio": (len(active) / len(cur)) if cur else 0.0,
        "by_channel": channel_summary,
        "churn_risk": _counts(active, "churn_risk"),
        "certification": _counts(active, "certification"),
        "status": _counts(cur, "status"),
        "tenure_buckets": tenure_buckets,
        "master_count": len(master),
    }


# --------------------------------------------------------------------------- #
# Scenario back-solve
# --------------------------------------------------------------------------- #
def run_scenario(
    data: Dict[str, List[Dict[str, Any]]],
    multiplier: float = 2.0,
    planned_active: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Back-solve the operational effort needed to reach a target total VNB.

    Holds non-Agency (Banca + Brokerage) VNB flat at the latest month, so the
    Agency channel must deliver the residual. The current Agency weighted
    margin is assumed unchanged (mix lever reported separately).
    """
    comp = company_series(data)
    cs = channel_series(data)
    if not comp or not cs:
        return None
    latest = comp[-1]
    cur_month = latest["month"]
    agency = _channel_row(cs, cur_month, AGENCY_CHANNEL)
    if not agency:
        return None

    multiplier = max(1.0, _num(multiplier) or 2.0)
    non_agency_vnb = sum(
        (_channel_row(cs, cur_month, cid) or {}).get("vnb", 0.0)
        for cid in NON_AGENCY_CHANNELS
    )
    current_total = latest["total_vnb"]
    target_total = current_total * multiplier
    required_agency_vnb = target_total - non_agency_vnb
    margin = agency["margin"]
    required_agency_ape = required_agency_vnb / margin if margin else 0.0

    cur_active = agency["active"]
    cur_prod = agency["avg_prod"]
    planned = int(planned_active) if planned_active else int(cur_active)
    planned = max(planned, 1)

    # Lever A: hold (or plan) headcount -> required productivity
    required_prod = required_agency_ape * 1000.0 / planned
    # Lever B: hold productivity -> required headcount
    required_active = required_agency_ape * 1000.0 / cur_prod if cur_prod else 0.0

    return {
        "month": cur_month,
        "label": latest["label"],
        "multiplier": multiplier,
        "current_total_vnb": current_total,
        "target_total_vnb": target_total,
        "gap_total_vnb": target_total - current_total,
        "non_agency_vnb_held": non_agency_vnb,
        "agency_margin": margin,
        "current_agency_vnb": agency["vnb"],
        "required_agency_vnb": required_agency_vnb,
        "agency_vnb_uplift": (
            (required_agency_vnb / agency["vnb"] - 1.0) if agency["vnb"] else 0.0
        ),
        "required_agency_ape": required_agency_ape,
        "current_agency_ape": agency["ape"],
        "lever_productivity": {
            "planned_active": planned,
            "active_uplift": (planned / cur_active - 1.0) if cur_active else 0.0,
            "current_prod": cur_prod,
            "required_prod": required_prod,
            "prod_uplift": (required_prod / cur_prod - 1.0) if cur_prod else 0.0,
        },
        "lever_headcount": {
            "current_prod": cur_prod,
            "current_active": int(cur_active),
            "required_active": required_active,
            "active_uplift": (
                (required_active / cur_active - 1.0) if cur_active else 0.0
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Roll-up used by the dashboard and template context
# --------------------------------------------------------------------------- #
def overview(data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    comp = company_series(data)
    latest = comp[-1] if comp else None
    return {
        "months": [c["month"] for c in comp],
        "latest_month": latest["month"] if latest else "",
        "latest_label": latest["label"] if latest else "",
        "company": comp,
        "channels": channel_series(data),
        "attribution": mom_attribution(data),
        "agents": agent_stats(data),
        "scenario": run_scenario(data),
    }
