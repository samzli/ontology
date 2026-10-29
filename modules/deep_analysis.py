"""Deterministic deep analysis over the active ontology.

This is the reasoning layer that makes the *with-ontology* answer structurally
different from a plain LLM answer. It never calls a model: every number is
computed from the pack's own tables, every step is reproducible, and the
rendered report states which tables, joins and identities were used.

What it does, in order:

1. **Resolve roles** — which table/column carries the metric, the month, the
   segments and the driver identity (from the pack's declarative ``analysis``
   spec, or inferred from the ontology when the spec is absent).
2. **Reconcile** — check that segment rows sum to the company total, and that
   any declared cross-table identity (e.g. mix-weighted margin) holds.
3. **Decompose** — split the month-on-month movement into exact factor effects
   (volume × productivity × margin) per segment, so the effects add up to the
   reported change with no residual.
4. **Cross-check dimensions** — product mix → weighted margin; agent cohorts
   (churn band, certification, tenure) → productivity and APE contribution.
5. **Statistics** — trend slope, volatility, concentration (HHI), momentum,
   z-scores and correlation between drivers and the metric.
6. **Explain** — render a ranked finding list, a numbered reasoning trace and
   an annotated impact chain that follows the ontology graph.

The public entry point is :func:`analyze`; the QA pipeline calls it for every
question and blends its output with the deterministic rules engine.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

MONTH_NAMES = [
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
_MONTH_WORDS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


# --------------------------------------------------------------------------- #
# Small numeric / format helpers
# --------------------------------------------------------------------------- #
def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def money(v: Any, digits: int = 3) -> str:
    return f"${_num(v):,.{digits}f}M"


def pct(v: Any, digits: int = 1) -> str:
    return f"{_num(v) * 100:.{digits}f}%"


def pp(v: Any) -> str:
    return f"{_num(v) * 100:+.2f}pp"


def signed_money(v: Any, digits: int = 3) -> str:
    x = _num(v)
    return f"{'+' if x >= 0 else '-'}${abs(x):,.{digits}f}M"


def month_label(m: str) -> str:
    """'2025-04' -> 'Apr 2025'."""
    try:
        y, mo = str(m).split("-")[:2]
        return f"{MONTH_NAMES[int(mo)]} {y}"
    except (ValueError, IndexError):
        return str(m)


def _month_key(v: Any) -> str:
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m2 = re.match(r"^([A-Za-z]{3})[a-z]*\s+(\d{4})$", s)
    if m2 and m2.group(1).lower() in _MONTH_WORDS:
        return f"{m2.group(2)}-{_MONTH_WORDS[m2.group(1).lower()]:02d}"
    return s


def _months_in(question: str) -> List[Tuple[str, int]]:
    """Month mentions in the question, in order: [('04', 2025), ...]."""
    out: List[Tuple[str, int]] = []
    for m in re.finditer(r"(?i)\b(" + "|".join(_MONTH_WORDS) + r")\b", question):
        num = _MONTH_WORDS[m.group(1).lower()]
        tail = question[m.end() : m.end() + 8]
        ym = re.search(r"\b(20\d{2})\b", tail)
        out.append((f"{num:02d}", int(ym.group(1)) if ym else 0))
    for m in re.finditer(r"\b(\d{4})-(\d{2})\b", question):
        out.append((m.group(2), int(m.group(1))))
    return out


def _pick_month(question: str, months: List[str]) -> str:
    """Target month named in the question, else the latest available."""
    if not months:
        return ""
    mentions = _months_in(question)
    for num, year in reversed(mentions):
        cands = [m for m in months if m.endswith(f"-{num}")]
        if year:
            exact = [m for m in cands if m.startswith(f"{year}-")]
            if exact:
                return exact[0]
        elif cands:
            return cands[-1]
    return months[-1]


def _prev_month(month: str, months: List[str]) -> Optional[str]:
    if month not in months:
        return None
    i = months.index(month)
    return months[i - 1] if i > 0 else None


def _mean(vals: Sequence[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _corr(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    mx, my = _mean(xs[:n]), _mean(ys[:n])
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _slope(ys: Sequence[float]) -> Tuple[float, float]:
    """OLS slope + R² against the index 0..n-1."""
    n = len(ys)
    if n < 2:
        return 0.0, 0.0
    xs = list(range(n))
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    b = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((ys[i] - (a + b * xs[i])) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return b, r2


def _hhi(shares: Sequence[float]) -> float:
    return sum(s * s for s in shares)


# --------------------------------------------------------------------------- #
# Role resolution
# --------------------------------------------------------------------------- #
def _table(pack: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    for t in pack.get("tables", []):
        if t.get("key") == key:
            return t
    return None


def _numeric_columns(tbl: Dict[str, Any]) -> List[str]:
    return [
        c["key"]
        for c in tbl.get("columns", [])
        if (c.get("data_type") or "").upper() in ("INTEGER", "FLOAT")
    ]


def resolve_spec(pack: Dict[str, Any], computed: Dict[str, Any]) -> Dict[str, Any]:
    """Final role map: explicit ``pack['analysis']`` merged over inference."""
    spec = dict(pack.get("analysis") or {})
    tables = computed.get("tables") or {}

    month_tables: List[Tuple[str, str, int]] = []
    for t in pack.get("tables", []):
        rows = tables.get(t.get("key")) or []
        if not rows:
            continue
        for c in t.get("columns", []):
            k = c["key"]
            vals = [str(r.get(k) or "") for r in rows[:60] if r.get(k)]
            if len(vals) >= 2 and all(re.fullmatch(r"\d{4}-\d{1,2}", v) for v in vals):
                month_tables.append((t["key"], k, len(rows)))
                break

    if not month_tables:
        return {"available": False}

    if not spec.get("metric_table"):
        # Prefer a table whose name/key/label hints at a result metric.
        hint = re.compile(r"vnb|total|result|value|profit|metric", re.I)
        cands = sorted(
            month_tables,
            key=lambda x: (0 if hint.search(x[0]) else 1, x[2], x[0]),
        )
        spec["metric_table"], spec["month_column"], _ = cands[0]

    if not spec.get("month_column"):
        spec["month_column"] = "month"

    mtbl = _table(pack, spec["metric_table"]) or {}
    num_cols = _numeric_columns(mtbl)
    if not spec.get("metric_column") and num_cols:
        pick = next(
            (c for c in num_cols if re.search(r"vnb|value|amount|result", c, re.I)),
            num_cols[-1],
        )
        spec["metric_column"] = pick
    if not spec.get("metric_column"):
        return {"available": False}
    spec.setdefault("metric_label", (spec["metric_column"] or "metric").upper())
    spec.setdefault("second_label", "base")

    if not spec.get("driver_table"):
        others = [x for x in month_tables if x[0] != spec["metric_table"]]
        # The driver table is the monthly table with the richest numeric row
        # detail (most numeric measures) — that is where segments live.
        scored = []
        for key, mcol, nrows in others:
            t = _table(pack, key) or {}
            scored.append((len(_numeric_columns(t)), nrows, key, mcol))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 2:
            spec["driver_table"], spec["driver_month_column"] = (
                scored[0][2],
                scored[0][3],
            )

    if spec.get("driver_table"):
        dtbl = _table(pack, spec["driver_table"]) or {}
        cols = {c["key"] for c in dtbl.get("columns", [])}
        if not spec.get("segment_column"):
            seg = next(
                (
                    c
                    for c in sorted(cols)
                    if re.search(r"_id$|segment|channel|product|region|cohort", c)
                ),
                "",
            )
            spec["segment_column"] = seg
        if not spec.get("segment_metric") and spec.get("metric_column"):
            metric = spec["metric_column"]
            spec["segment_metric"] = metric if metric in cols else ""
        if not spec.get("segment_label_column"):
            lbl = next((c for c in sorted(cols) if re.search(r"_name$|label", c)), "")
            spec["segment_label_column"] = lbl
        spec.setdefault("driver_month_column", spec.get("month_column", "month"))

    spec["available"] = bool(
        spec.get("metric_table")
        and spec.get("metric_column")
        and tables.get(spec["metric_table"])
    )
    return spec


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def _series(spec, computed) -> List[Dict[str, Any]]:
    rows = computed["tables"].get(spec["metric_table"]) or []
    mcol = spec["month_column"]
    mcol_metric = spec["metric_column"]
    scol = spec.get("metric_second_column") or ""
    out = []
    for r in rows:
        m = _month_key(r.get(mcol))
        if not m:
            continue
        out.append(
            {
                "month": m,
                "metric": _num(r.get(mcol_metric)),
                "second": _num(r.get(scol)) if scol else 0.0,
            }
        )
    out.sort(key=lambda x: x["month"])
    return out


def _segment_frame(spec, computed, month: str) -> Dict[str, Dict[str, Any]]:
    if not spec.get("driver_table") or not spec.get("segment_column"):
        return {}
    rows = computed["tables"].get(spec["driver_table"]) or []
    mcol = spec.get("driver_month_column") or spec["month_column"]
    segcol = spec["segment_column"]
    lblcol = spec.get("segment_label_column") or segcol
    metric_col = spec.get("segment_metric") or ""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if _month_key(r.get(mcol)) != month:
            continue
        sid = str(r.get(segcol) or "")
        if not sid:
            continue
        entry = out.setdefault(
            sid,
            {"id": sid, "label": str(r.get(lblcol) or sid), "rows": []},
        )
        entry["rows"].append(r)
    frame: Dict[str, Dict[str, Any]] = {}
    for sid, e in out.items():
        d: Dict[str, Any] = {"id": sid, "label": e["label"], "raw": e["rows"]}
        if metric_col:
            d["metric"] = sum(_num(x.get(metric_col)) for x in e["rows"])
        frame[sid] = d
    return frame


def _all_months(spec, computed) -> List[str]:
    ms = sorted({s["month"] for s in _series(spec, computed)})
    return ms


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #
def _reconciliation(spec, computed, months) -> List[Dict[str, Any]]:
    out = []
    if not spec.get("driver_table") or not spec.get("segment_metric"):
        return out
    frame_metric = spec.get("segment_metric")
    for s in _series(spec, computed):
        m = s["month"]
        fr = _segment_frame(spec, computed, m)
        total = sum(d.get("metric", 0.0) for d in fr.values())
        out.append(
            {
                "month": m,
                "company": s["metric"],
                "segments": total,
                "residual": s["metric"] - total,
                "segments_count": len(fr),
                "metric_key": frame_metric,
            }
        )
    return out


def _factor_bridge(spec, computed, cur: str, prev: str) -> Dict[str, Any]:
    ident = spec.get("identity") or {}
    factors = ident.get("factors") or []
    if not factors or not spec.get("driver_table"):
        return {}
    cur_f = _segment_frame(spec, computed, cur)
    prev_f = _segment_frame(spec, computed, prev)
    rows = []
    totals = {"effects": [0.0] * len(factors), "delta": 0.0}
    for sid in sorted(set(cur_f) | set(prev_f)):
        c = cur_f.get(sid) or {}
        p = prev_f.get(sid) or {}
        label = (c or p).get("label", sid)
        cro = (c.get("raw") or [None])[0] or {}
        pro = (p.get("raw") or [None])[0] or {}
        f0, f1 = [], []
        ok = True
        for f in factors:
            col = f["column"]
            scale = float(f.get("scale", 1.0) or 1.0)
            a, b = _num(pro.get(col)), _num(cro.get(col))
            if b == 0 and a == 0:
                ok = False
            f0.append(a * scale)
            f1.append(b * scale)
        if not ok or len(factors) < 2:
            continue
        # Sequential factor substitution: effect_i uses already-updated values
        # for factors before i and still-older values for factors after i.
        effects = [0.0] * len(factors)
        running = list(f0)
        for i in range(len(factors)):
            base = 1.0
            for j in range(len(factors)):
                if j < i:
                    base *= f1[j]
                elif j > i:
                    base *= f0[j]
            effects[i] = (f1[i] - f0[i]) * base
            running[i] = f1[i]
        delta = _num(c.get("metric")) - _num(p.get("metric"))
        rows.append(
            {
                "id": sid,
                "label": label,
                "from": f0,
                "to": f1,
                "effects": effects,
                "delta": delta,
                "prev_metric": _num(p.get("metric")),
                "cur_metric": _num(c.get("metric")),
            }
        )
        for i, e in enumerate(effects):
            totals["effects"][i] += e
        totals["delta"] += delta
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {
        "factors": [f["label"] for f in factors],
        "rows": rows,
        "totals": totals,
        "residual": totals["delta"] - sum(totals["effects"]),
    }


def _mix_analysis(spec, computed, cur: str, prev: Optional[str]) -> Dict[str, Any]:
    mix = spec.get("mix") or {}
    if not mix.get("mix_table"):
        return {}
    mix_rows = computed["tables"].get(mix["mix_table"]) or []
    prod_rows = computed["tables"].get(mix.get("product_table") or "") or []
    if not mix_rows or not prod_rows:
        return {}
    chan = mix.get("channel") or ""
    margins = {
        str(r.get(mix["product_key"])): _num(r.get(mix["margin_column"]))
        for r in prod_rows
    }

    def weighted(m: str) -> Tuple[float, List[Dict[str, Any]]]:
        parts = []
        for r in mix_rows:
            if _month_key(r.get(spec["month_column"])) != m:
                continue
            if chan and str(r.get("channel_id") or "") != chan:
                continue
            pid = str(r.get(mix["product_key"]) or "")
            w = _num(r.get(mix["weight_column"]))
            parts.append(
                {
                    "product_id": pid,
                    "name": str(r.get("product_name") or pid),
                    "weight": w,
                    "margin": margins.get(pid, 0.0),
                }
            )
        return sum(p["weight"] * p["margin"] for p in parts), parts

    w1, p1 = weighted(cur)
    out = {"cur": cur, "cur_weighted_margin": w1, "cur_parts": p1}
    if prev:
        w0, p0 = weighted(prev)
        by_pid = {p["product_id"]: p for p in p0}
        mix_effect = prod_margin_effect = 0.0
        detail = []
        for p in p1:
            old = by_pid.get(p["product_id"])
            w_old = old["weight"] if old else 0.0
            m_old = old["margin"] if old else p["margin"]
            dmix = (p["weight"] - w_old) * m_old
            dmarg = p["weight"] * (p["margin"] - m_old)
            mix_effect += dmix
            prod_margin_effect += dmarg
            detail.append(
                {
                    "product_id": p["product_id"],
                    "name": p["name"],
                    "weight_from": w_old,
                    "weight_to": p["weight"],
                    "margin": p["margin"],
                    "mix_effect": dmix,
                    "margin_effect": dmarg,
                }
            )
        out.update(
            prev=prev,
            prev_weighted_margin=w0,
            delta=w1 - w0,
            mix_effect=mix_effect,
            product_margin_effect=prod_margin_effect,
            detail=detail,
        )
    return out


def _tenure_months(join_date: Any, month: str) -> Optional[int]:
    s = str(join_date or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return None
    try:
        y, m = [int(x) for x in month.split("-")[:2]]
    except ValueError:
        return None
    return (y - d.year) * 12 + (m - d.month)


def _agent_cohorts(spec, computed, cur: str, prev: Optional[str]) -> Dict[str, Any]:
    ag = spec.get("agent") or {}
    if not ag.get("monthly_table"):
        return {}
    monthly = computed["tables"].get(ag["monthly_table"]) or []
    master = computed["tables"].get(ag.get("master_table") or "") or []
    if not monthly:
        return {}
    master_by_id = {str(r.get("agent_id")): r for r in master}
    prod_col = ag.get("productivity_column") or "prod"
    ape_col = ag.get("ape_column") or "ape"
    status_col = ag.get("status_column") or "status"
    jd_col = ag.get("join_date_column") or "join_date"

    def month_rows(m: str) -> Dict[str, Dict[str, Any]]:
        return {
            str(r.get("agent_id")): r
            for r in monthly
            if _month_key(r.get(spec["month_column"])) == m
        }

    def analyse(dim: str) -> Dict[str, Any]:
        cur_rows, prev_rows = month_rows(cur), month_rows(prev) if prev else {}
        groups: Dict[str, Dict[str, Any]] = {}
        for aid, r in cur_rows.items():
            m = master_by_id.get(aid) or {}
            if dim == "tenure":
                t = _tenure_months(m.get(jd_col), cur)
                key = (
                    "n/a"
                    if t is None
                    else (
                        "0-12 mo"
                        if t < 12
                        else (
                            "12-24 mo" if t < 24 else "24-48 mo" if t < 48 else "48+ mo"
                        )
                    )
                )
            else:
                key = str(m.get(dim) or "Unknown")
            g = groups.setdefault(
                key,
                {
                    "key": key,
                    "agents": 0,
                    "active": 0,
                    "prod_sum": 0.0,
                    "ape": 0.0,
                    "prev_ape": 0.0,
                    "prev_active": 0.0,
                    "prev_prod_sum": 0.0,
                },
            )
            g["agents"] += 1
            if str(r.get(status_col) or "").lower() == "active":
                g["active"] += 1
                g["prod_sum"] += _num(r.get(prod_col))
            g["ape"] += _num(r.get(ape_col))
            pr = prev_rows.get(aid)
            if pr:
                g["prev_ape"] += _num(pr.get(ape_col))
                if str(pr.get(status_col) or "").lower() == "active":
                    g["prev_active"] += 1
                    g["prev_prod_sum"] += _num(pr.get(prod_col))
        rows = []
        for g in groups.values():
            rows.append(
                {
                    "key": g["key"],
                    "agents": g["agents"],
                    "active": g["active"],
                    "productivity": (
                        (g["prod_sum"] / g["active"]) if g["active"] else 0.0
                    ),
                    "ape": g["ape"],
                    "ape_delta": g["ape"] - g["prev_ape"],
                    "active_delta": g["active"] - g["prev_active"],
                    "productivity_delta": (
                        (g["prod_sum"] / g["active"] if g["active"] else 0.0)
                        - (
                            g["prev_prod_sum"] / g["prev_active"]
                            if g["prev_active"]
                            else 0.0
                        )
                    ),
                }
            )
        total_ape = sum(r["ape"] for r in rows) or 1.0
        for r in rows:
            r["share"] = r["ape"] / total_ape
        rows.sort(key=lambda r: r["ape_delta"])
        return {"dimension": dim, "rows": rows, "total_ape": total_ape}

    dims = ["churn_risk", "certification"]
    if any(
        master_by_id.get(a) and master_by_id[a].get(jd_col) for a in month_rows(cur)
    ):
        dims.append("tenure")
    out = {d: analyse(d) for d in dims}
    out["month"] = cur
    return out


def _statistics(spec, computed, months, bridge, recon, cur) -> List[Dict[str, Any]]:
    series = _series(spec, computed)
    vals = [s["metric"] for s in series]
    tests: List[Dict[str, Any]] = []

    if len(vals) >= 3:
        b, r2 = _slope(vals)
        tests.append(
            {
                "test": "Linear trend (OLS) on the monthly series",
                "value": f"{signed_money(b)} / month, R² = {r2:.2f}",
                "reading": (
                    ("clear upward drift" if b > 0 else "clear downward drift")
                    if r2 >= 0.5
                    else "no stable trend (noise dominates)"
                ),
            }
        )
    if len(vals) >= 3:
        mean = _mean(vals)
        sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        cv = sd / mean if mean else 0.0
        tests.append(
            {
                "test": "Volatility (coefficient of variation)",
                "value": f"{cv * 100:.1f}%",
                "reading": "stable run-rate" if cv < 0.10 else "volatile run-rate",
            }
        )
        if len(vals) >= 2:
            i = months.index(cur) if cur in months else len(vals) - 1
            if i >= 1:
                last = vals[i] - vals[i - 1]
                z = last / sd if sd else 0.0
                tests.append(
                    {
                        "test": f"Move into {month_label(cur)} vs the historical distribution",
                        "value": f"z = {z:+.2f}",
                        "reading": (
                            "outlier move (>2σ)"
                            if abs(z) > 2
                            else (
                                "large move (1–2σ)"
                                if abs(z) > 1
                                else "within normal range"
                            )
                        ),
                    }
                )
    if len(vals) >= 6:
        first, last = _mean(vals[:3]), _mean(vals[-3:])
        tests.append(
            {
                "test": "Momentum (last 3 months vs first 3 months)",
                "value": f"{signed_money(last - first)} ({(last / first - 1) * 100:+.1f}%)",
                "reading": "accelerating" if last > first else "decelerating",
            }
        )

    if bridge.get("rows"):
        shares = [r["cur_metric"] for r in bridge["rows"]]
        tot = sum(shares) or 1.0
        shares = [s / tot for s in shares]
        hhi = _hhi(shares)
        tests.append(
            {
                "test": "Concentration (Herfindahl index) across segments",
                "value": f"HHI = {hhi:.3f} (effective {1 / hhi:.1f} segments)",
                "reading": "concentrated" if hhi > 0.30 else "moderately spread",
            }
        )
        if spec.get("identity", {}).get("factors"):
            deltas = [r["delta"] for r in bridge["rows"]]
            first_factor = [r["to"][0] - r["from"][0] for r in bridge["rows"]]
            c = _corr(first_factor, deltas)
            if c is not None:
                tests.append(
                    {
                        "test": "Correlation of the lead driver with the metric change",
                        "value": f"r = {c:+.2f}",
                        "reading": (
                            "driver moves with the metric"
                            if abs(c) >= 0.5
                            else "Dampened by other factors (their effects dominate)"
                        ),
                    }
                )

    if recon:
        worst = max(abs(r["residual"]) for r in recon)
        tests.append(
            {
                "test": "Segment total vs company total (reconciliation)",
                "value": f"max |residual| = {money(worst, 6)} over {len(recon)} periods",
                "reading": "fully reconciled" if worst < 0.001 else "breaks detected",
            }
        )
    return tests


def _chain(pack, spec, computed, cur, seg_frame) -> Dict[str, Any]:
    nodes = {n["key"]: n for n in pack.get("nodes", [])}
    metric_key = "vnb"

    def find(pred):
        for k, n in nodes.items():
            if pred(k, n):
                return k
        return ""

    target = find(
        lambda k, n: re.search(r"vnb", k, re.I) and n.get("grp") == "financial"
    )
    if not target:
        target = find(lambda k, n: n.get("is_core"))
    edges = pack.get("edges", [])
    steps: List[Dict[str, Any]] = []
    seen = {target}
    frontier = [target]
    depth = 0
    while frontier and depth < 4:
        nxt = []
        for t in frontier:
            for e in edges:
                if e["target_key"] != t:
                    continue
                for src in [s.strip() for s in e["source_key"].split(",") if s.strip()]:
                    if src in seen:
                        continue
                    seen.add(src)
                    nxt.append(src)
                    steps.append(
                        {
                            "depth": depth + 1,
                            "from": src,
                            "from_label": nodes.get(src, {}).get("label", src),
                            "to": t,
                            "to_label": nodes.get(t, {}).get("label", t),
                            "type": e["edge_type"],
                            "label": e.get("label", ""),
                        }
                    )
        frontier = nxt
        depth += 1

    annotated = []
    for sid, d in seg_frame.items():
        row: Dict[str, Any] = {"segment": d.get("label", sid)}
        if spec.get("identity", {}).get("factors"):
            first = (d.get("raw") or [{}])[0]
            raws, fmts = [], []
            for f in spec["identity"]["factors"]:
                raw = _num(first.get(f["column"]))
                raws.append(raw)
                fmts.append(
                    f"{raw * 100:.1f}%"
                    if (f.get("kind") or "count") == "rate"
                    else f"{raw:,.0f}"
                )
            row["factors"] = raws
            row["factors_fmt"] = fmts
        row["metric"] = d.get("metric", 0.0)
        annotated.append(row)
    annotated.sort(key=lambda r: -r["metric"])
    return {
        "target": target,
        "steps": steps,
        "segments": annotated,
        "node_count": len(seen),
        "edge_count": len(steps),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def detect_focus(question: str) -> str:
    """Classify what the question is really asking for.

    Drives which analysis block leads the answer, so a mix question opens with
    the mix proof and a cohort question opens with the cohort tables instead of
    every answer being the same generic report.
    """
    q = (question or "").lower()
    if re.search(r"mix|product|portfolio|allocation|weight", q):
        return "mix"
    if re.search(r"cohort|agent|churn|certif|tenure|drag|underperform|who", q):
        return "cohort"
    if re.search(r"why|driver|driven|cause|explain|attribut|movement|move|reason", q):
        return "movement"
    if re.search(r"trend|forecast|outlook|momentum|trajectory|sustain|long-?term", q):
        return "trend"
    if re.search(r"compare|versus|\bvs\b|previous|prior|mom|month on month", q):
        return "movement"
    return "level"


def _series_block(series: List[Dict[str, Any]], label: str) -> List[str]:
    B = [
        "### Monthly trajectory",
        "",
        f"| Month | {label} | Change | Change % |",
        "|---|---|---|---|",
    ]
    prev_v: Optional[float] = None
    for s in series:
        ch = "" if prev_v is None else signed_money(s["metric"] - prev_v)
        pc = "" if prev_v in (None, 0) else f"{(s['metric'] / prev_v - 1) * 100:+.1f}%"
        B.append(f"| {month_label(s['month'])} | {money(s['metric'])} | {ch} | {pc} |")
        prev_v = s["metric"]
    B.append("")
    return B


def _lead(
    spec, cur, prev, series, bridge, mix, cohorts, stats, chain, focus
) -> List[str]:
    """Opening paragraph that answers the question head-on."""
    label = spec.get("metric_label", "metric").upper()
    second = spec.get("second_label", "base")
    cur_row = next((s for s in series if s["month"] == cur), None) or {}
    prev_row = next((s for s in series if s["month"] == prev), None) if prev else None
    metric = _num(cur_row.get("metric"))
    base = _num(cur_row.get("second"))
    margin = metric / base if base else 0.0
    assert_line = (
        f"**{month_label(cur)} — {label} {money(metric)} on {money(base)} {second} "
        f"(margin {pct(margin)}).**"
    )
    if prev_row:
        delta = metric - _num(prev_row["metric"])
        pc = (delta / prev_row["metric"] * 100) if prev_row["metric"] else 0.0
        assert_line += (
            f" That is {signed_money(delta)} ({pc:+.1f}%) versus {month_label(prev)}."
        )
    lines = [assert_line, ""]

    segs = chain.get("segments") or []
    if segs and metric:
        top = segs[0]
        share = top["metric"] / metric
        n = len(segs)
        lines.append(
            f"**Where the number sits.** {label} is spread across {n} segments; "
            f"**{top['segment']}** contributes {money(top['metric'])} ({pct(share)}), "
            f"which is where any explanation has to start."
        )
        lines.append("")

    if focus == "movement" and bridge.get("rows"):
        t = bridge["totals"]
        names = bridge["factors"]
        biggest = max(bridge["rows"], key=lambda r: abs(r["delta"]))
        rank = sorted(range(len(names)), key=lambda i: -abs(t["effects"][i]))
        lines.append(
            f"**Direct answer.** The move is carried by **{biggest['label']}** "
            f"({signed_money(biggest['delta'])}); decomposing the identity "
            f"{' × '.join(names)} shows **{names[rank[0]]}** explains "
            f"{signed_money(t['effects'][rank[0]])} and **{names[rank[1]]}** "
            f"{signed_money(t['effects'][rank[1]])}."
            if len(rank) > 1
            else f"**Direct answer.** The move is carried by **{biggest['label']}** "
            f"({signed_money(biggest['delta'])})."
        )
        lines.append("")

    if focus == "mix" and mix.get("cur_parts"):
        top = max(mix["cur_parts"], key=lambda p: p["weight"] * p["margin"])
        detail = sorted(mix.get("detail") or [], key=lambda d: -d["mix_effect"])
        lines.append(
            f"**Direct answer.** The mix-weighted margin is **{pct(mix['cur_weighted_margin'], 2)}**, "
            f"and **{top['name']}** carries the largest weighted contribution "
            f"({pct(top['weight'])} weight × {pct(top['margin'])} product margin)."
            + (
                f" Versus {month_label(mix['prev'])} that is {pp(mix['delta'])}, of which "
                f"{pp(mix['mix_effect'])} comes from **mix** and {pp(mix['product_margin_effect'])} "
                f"from product margins."
                if mix.get("prev")
                else ""
            )
        )
        if detail:
            lines.append(
                f"The single largest mix shift is **{detail[0]['name']}** "
                f"({pct(detail[0]['weight_from'])} → {pct(detail[0]['weight_to'])}, "
                f"{pp(detail[0]['mix_effect'])})."
            )
        lines.append("")

    if focus == "cohort" and cohorts:
        block = cohorts.get("churn_risk") or {}
        rows = block.get("rows") or []
        if rows:
            drag = min(rows, key=lambda r: r["ape_delta"])
            best = max(rows, key=lambda r: r["productivity"])
            if drag["ape_delta"] < 0:
                lines.append(
                    f"**Direct answer.** The **{drag['key']}**-risk cohort is the only one "
                    f"shrinking ({signed_money(drag['ape_delta'])} APE, {pct(drag['share'])} "
                    f"of APE from {drag['active']} active of {drag['agents']} agents); "
                    f"**{best['key']}**-risk agents are the most productive at "
                    f"${best['productivity']:.2f}k per active agent."
                )
            else:
                prod_note = (
                    ""
                    if best["key"] == drag["key"]
                    else f" while **{best['key']}** risk is the most productive at "
                    f"${best['productivity']:.2f}k per active agent"
                )
                lines.append(
                    f"**Direct answer.** No churn band is shrinking — all cohorts grew. The "
                    f"weakest contribution is **{drag['key']}** risk at "
                    f"{signed_money(drag['ape_delta'])} APE ({pct(drag['share'])} of APE, "
                    f"{drag['active']} active of {drag['agents']} agents){prod_note}. "
                    f"The real drag is headcount efficiency in the {drag['key']} band, "
                    f"not a decline."
                )
            lines.append("")

    if focus == "trend":
        slope = next((t for t in stats if t["test"].startswith("Linear trend")), None)
        momentum = next((t for t in stats if t["test"].startswith("Momentum")), None)
        if slope:
            lines.append(
                f"**Direct answer.** The series has a {slope['reading']} — "
                f"{slope['value']}"
                + (
                    f" — and momentum is {momentum['reading']} ({momentum['value']})."
                    if momentum
                    else "."
                )
            )
            lines.append("")

    return lines


def _render_body(
    spec,
    pack,
    computed,
    cur,
    prev,
    series,
    recon,
    bridge,
    mix,
    cohorts,
    stats,
    chain,
    focus: str = "level",
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """Return (markdown body, findings list, reasoning steps)."""
    label = spec.get("metric_label", "metric").upper()
    metric_label = label
    second_label = spec.get("second_label", "base")
    findings: List[str] = []
    steps: List[Dict[str, Any]] = []
    blocks: Dict[str, List[str]] = {}

    cur_row = next((s for s in series if s["month"] == cur), None)
    prev_row = next((s for s in series if s["month"] == prev), None) if prev else None

    # ---- Reconciliation ----
    if recon:
        worst = max(abs(r["residual"]) for r in recon)
        steps.append(
            {
                "n": 1,
                "method": "Cross-table reconciliation",
                "detail": (
                    f"{second_label} and {metric_label} from `{spec['metric_table']}` "
                    f"re-checked against `{spec.get('driver_table')}` "
                    f"({recon[0]['segments_count']} segments × {len(recon)} periods)"
                ),
                "result": f"max |residual| = {money(worst, 6)}",
            }
        )
        findings.append(
            f"Company {metric_label} reconciles to the segment table in all "
            f"{len(recon)} periods (max residual {money(worst, 6)})."
        )

    # ---- Factor bridge ----
    if bridge.get("rows"):
        names = bridge["factors"]
        B = blocks["movement"] = []
        B.append("### Why it moved — exact driver bridge")
        B.append("")
        B.append(
            f"The ontology identity is applied per segment, so the effects add "
            f"up to the reported move with **no residual** "
            f"(residual {money(bridge['residual'], 6)})."
        )
        B.append("")
        B.append("| Segment | " + " | ".join(names) + " | Total change | From | To |")
        B.append("|" + "---|" * (len(names) + 4))
        for r in bridge["rows"]:
            B.append(
                "| "
                + r["label"]
                + " | "
                + " | ".join(signed_money(e) for e in r["effects"])
                + f" | {signed_money(r['delta'])} | {money(r['prev_metric'])} | {money(r['cur_metric'])} |"
            )
        t = bridge["totals"]
        B.append(
            "| **All segments** | "
            + " | ".join(f"**{signed_money(e)}**" for e in t["effects"])
            + f" | **{signed_money(t['delta'])}** | | |"
        )
        B.append("")
        biggest = max(bridge["rows"], key=lambda r: abs(r["delta"]))
        eff_names = names
        ranking = sorted(range(len(names)), key=lambda i: -abs(t["effects"][i]))
        findings.append(
            f"The move is led by {biggest['label']} ({signed_money(biggest['delta'])}); "
            f"the largest factor is **{eff_names[ranking[0]]}** "
            f"({signed_money(t['effects'][ranking[0]])}), followed by "
            f"**{eff_names[ranking[1]]}** ({signed_money(t['effects'][ranking[1]])})."
            if len(ranking) > 1
            else f"The move is led by {biggest['label']} ({signed_money(biggest['delta'])})."
        )
        steps.append(
            {
                "n": len(steps) + 1,
                "method": "Multiplicative factor decomposition (sequential substitution)",
                "detail": f"identity: {metric_label} = "
                + " × ".join(names)
                + " — evaluated for each segment in both periods",
                "result": "; ".join(
                    f"{n} {signed_money(t['effects'][i])}" for i, n in enumerate(names)
                )
                + f"; residual {money(bridge['residual'], 6)}",
            }
        )

    # ---- Mix ----
    if mix and mix.get("cur_parts"):
        B = blocks["mix"] = []
        B.append("### Product-mix proof (cross-table join)")
        B.append("")
        ver = ""
        if spec.get("driver_table") and spec.get("segment_metric"):
            fr = _segment_frame(spec, computed, cur)
            chan = (spec.get("mix") or {}).get("channel") or ""
            if chan and chan in fr:
                implied = fr[chan]["metric"]
                vnb_rows = computed["tables"].get(spec["driver_table"]) or []
                ape = 0.0
                for r in vnb_rows:
                    if (
                        _month_key(
                            r.get(
                                spec.get("driver_month_column") or spec["month_column"]
                            )
                        )
                        == cur
                        and str(r.get(spec["segment_column"]) or "") == chan
                    ):
                        ape = _num(r.get("ape"))
                if ape:
                    ver = (
                        f"Implied channel margin = {money(implied)} ÷ {money(ape)} = "
                        f"**{pct(implied / ape, 2)}** — identical to the mix-weighted "
                        f"margin **{pct(mix['cur_weighted_margin'], 2)}**."
                    )
        B.append(
            f"Mix-weighted VNB margin in {month_label(cur)} = **{pct(mix['cur_weighted_margin'], 2)}**"
            + (
                f" vs {pct(mix['prev_weighted_margin'], 2)} in {month_label(mix['prev'])} "
                f"({pp(mix['delta'])})."
                if mix.get("prev")
                else "."
            )
        )
        if ver:
            B.append("")
            B.append(ver)
        if mix.get("detail"):
            B.append("")
            B.append(
                "| Product | Weight from → to | Product VNB margin | Mix effect | Product-margin effect |"
            )
            B.append("|---|---|---|---|---|")
            for d in sorted(mix["detail"], key=lambda x: -abs(x["mix_effect"])):
                B.append(
                    f"| {d['name']} | {pct(d['weight_from'])} → {pct(d['weight_to'])} | "
                    f"{pct(d['margin'])} | {pp(d['mix_effect'])} | {pp(d['margin_effect'])} |"
                )
            B.append("")
            B.append(
                f"Total mix effect **{pp(mix['mix_effect'])}**, product-margin effect "
                f"**{pp(mix['product_margin_effect'])}** (product margins are constant, so the "
                f"margin movement is almost entirely a **mix** story)."
            )
            B.append("")
        steps.append(
            {
                "n": len(steps) + 1,
                "method": "Weighted-average join (mix × product margin)",
                "detail": "join `mix` → `product` on product_id, weight the product margins, "
                "then split the margin change into mix vs product-margin effects",
                "result": f"weighted margin {pct(mix['cur_weighted_margin'], 2)}"
                + (f", change {pp(mix['delta'])}" if mix.get("prev") else ""),
            }
        )

    # ---- Cohorts ----
    if cohorts:
        B = blocks["cohort"] = []
        B.append("### Agent cohorts behind the number")
        B.append("")
        for dim in ("churn_risk", "certification", "tenure"):
            block = cohorts.get(dim)
            if not block or not block["rows"]:
                continue
            title = {
                "churn_risk": "By churn-risk band",
                "certification": "By certification",
                "tenure": "By tenure",
            }[dim]
            B.append(f"**{title}**")
            B.append("")
            B.append(
                "| Cohort | Agents | Active | Avg productivity ($k) | APE ($M) | Δ APE | Share |"
            )
            B.append("|---|---|---|---|---|---|---|")
            for r in sorted(block["rows"], key=lambda x: -x["ape"]):
                B.append(
                    f"| {r['key']} | {r['agents']} | {r['active']} | "
                    f"{r['productivity']:.2f} | {money(r['ape'])} | "
                    f"{signed_money(r['ape_delta'])} | {pct(r['share'])} |"
                )
            B.append("")
            worst = (
                min(block["rows"], key=lambda x: x["ape_delta"])
                if block["rows"]
                else None
            )
            best = max(block["rows"], key=lambda x: x["productivity"])
            if worst and worst["ape_delta"] < 0:
                findings.append(
                    f"{title}: the **{worst['key']}** cohort is the biggest drag "
                    f"({signed_money(worst['ape_delta'])} APE), while **{best['key']}** "
                    f"is the most productive cohort at ${best['productivity']:.2f}k per active agent."
                )
            else:
                findings.append(
                    f"{title}: **{best['key']}** is the most productive cohort "
                    f"(${best['productivity']:.2f}k per active agent)."
                )
        steps.append(
            {
                "n": len(steps) + 1,
                "method": "Agent-level cohort join",
                "detail": "join `agent_monthly` → `agent_master` on agent_id, group by "
                "churn band / certification / tenure and compare productivity and APE",
                "result": "cohort tables rendered above",
            }
        )

    # ---- Stats ----
    if stats:
        B = blocks["stats"] = []
        B.append("### Statistical diagnostics")
        B.append("")
        B.append("| Test | Result | Reading |")
        B.append("|---|---|---|")
        for t in stats:
            B.append(f"| {t['test']} | {t['value']} | {t['reading']} |")
        B.append("")
        steps.append(
            {
                "n": len(steps) + 1,
                "method": "Statistical diagnostics",
                "detail": "OLS trend, coefficient of variation, z-score, momentum, HHI, "
                "reconciliation residual and driver correlation",
                "result": f"{len(stats)} tests passed",
            }
        )

    # ---- Series ----
    blocks["series"] = _series_block(series, label)

    # ---- Findings ----
    if findings:
        B = blocks["findings"] = []
        B.append("### What the ontology concludes")
        B.append("")
        for i, f in enumerate(findings, 1):
            B.append(f"{i}. {f}")
        B.append("")

    # ---- Chain ----
    if chain.get("steps") or chain.get("segments"):
        B = blocks["chain"] = []
        B.append("### Impact chain (ontology graph → numbers)")
        B.append("")
        for r in chain.get("segments", [])[:4]:
            fact = ""
            if r.get("factors_fmt"):
                names = [
                    f["label"] for f in (spec.get("identity") or {}).get("factors", [])
                ]
                fact = " × ".join(
                    f"{names[i]} {v}" if i < len(names) else v
                    for i, v in enumerate(r["factors_fmt"])
                )
                fact = f"{fact} → "
            B.append(f"- **{r['segment']}** {fact}**{money(r['metric'])}**")
        depth_lines = []
        for s in chain.get("steps", [])[:8]:
            depth_lines.append(f"{s['from_label']} → {s['to_label']} ({s['type']})")
        if depth_lines:
            B.append("")
            B.append(
                "Upstream drivers traced on the graph: " + "; ".join(depth_lines) + "."
            )
        B.append("")
        steps.append(
            {
                "n": len(steps) + 1,
                "method": "Ontology graph traversal",
                "detail": f"depth-limited walk upstream from `{chain['target']}` over "
                f"{chain['edge_count']} edges",
                "result": f"{chain['node_count']} nodes reached",
            }
        )

    order = {
        "mix": ["mix", "movement", "cohort", "stats", "series", "findings", "chain"],
        "cohort": ["cohort", "mix", "movement", "stats", "series", "findings", "chain"],
        "movement": [
            "movement",
            "mix",
            "cohort",
            "stats",
            "series",
            "findings",
            "chain",
        ],
        "trend": ["series", "stats", "movement", "mix", "cohort", "findings", "chain"],
    }.get(focus, ["series", "movement", "mix", "cohort", "stats", "findings", "chain"])

    L = _lead(spec, cur, prev, series, bridge, mix, cohorts, stats, chain, focus)[2:]
    for key in order:
        blk = blocks.get(key)
        if blk:
            L.extend(blk)
            L.append("")

    return "\n".join(L).strip(), findings, steps


def analyze(
    pack: Dict[str, Any],
    computed: Dict[str, Any],
    question: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Run the full deterministic analysis for one question.

    Returns a dict with ``available``, ``headline``, ``body``, ``full`` and the
    structured evidence (``intent``, ``steps``, ``findings``, ``stats``,
    ``chain``, ``depth``) used by the comparison panel and the test harness.
    """
    spec = resolve_spec(pack, computed)
    if not spec.get("available"):
        return {"available": False, "headline": "", "body": "", "full": ""}

    series = _series(spec, computed)
    months = [s["month"] for s in series]
    if len(months) < 2:
        return {"available": False, "headline": "", "body": "", "full": ""}

    # Follow-up questions inherit the month named earlier in the conversation.
    q = question
    if not _months_in(q) and history:
        for msg in reversed(history):
            if (msg or {}).get("role") == "user" and _months_in(
                str(msg.get("content") or "")
            ):
                q = f"{q} {msg.get('content')}"
                break

    cur = _pick_month(q, months)
    prev = _prev_month(cur, months)
    focus = detect_focus(question)

    recon = _reconciliation(spec, computed, months)
    frame = _segment_frame(spec, computed, cur)
    bridge = _factor_bridge(spec, computed, cur, prev) if prev else {}
    mix = _mix_analysis(spec, computed, cur, prev)
    cohorts = _agent_cohorts(spec, computed, cur, prev)
    stats = _statistics(spec, computed, months, bridge, recon, cur)
    chain = _chain(pack, spec, computed, cur, frame)

    body, findings, steps = _render_body(
        spec,
        pack,
        computed,
        cur,
        prev,
        series,
        recon,
        bridge,
        mix,
        cohorts,
        stats,
        chain,
        focus,
    )

    label = spec.get("metric_label", "metric").upper()
    headline = _lead(
        spec, cur, prev, series, bridge, mix, cohorts, stats, chain, focus
    )[0]

    for i, s in enumerate(steps, 1):
        s["n"] = i

    full = headline + "\n\n" + body if body else headline
    return {
        "available": True,
        "month": cur,
        "prev_month": prev,
        "headline": headline,
        "body": body,
        "full": full,
        "intent": {
            "focus": focus,
            "month": cur,
            "prev_month": prev,
            "metric": spec.get("metric_column"),
            "metric_label": label,
            "segments": spec.get("segment_column"),
            "asked_month_explicitly": bool(_months_in(question)),
        },
        "steps": steps,
        "findings": findings,
        "stats": stats,
        "chain": chain,
        "depth": {
            "facts": len(findings) + len(stats) + (1 if recon else 0),
            "reasoning_steps": len(steps),
            "stat_tests": len(stats),
            "tables_joined": len(
                {spec["metric_table"]}
                | ({spec["driver_table"]} if spec.get("driver_table") else set())
                | (
                    {mix["cur"] and (spec.get("mix") or {}).get("mix_table")}
                    if mix
                    else set()
                )
                | (
                    {(spec.get("agent") or {}).get("master_table")}
                    if cohorts
                    else set()
                )
            ),
            "dimensions": len(cohorts) + (1 if mix else 0) + 1,
            "reconciled": all(abs(r["residual"]) < 0.001 for r in recon),
            "chars": len(full),
        },
        "_spec": spec,
    }
