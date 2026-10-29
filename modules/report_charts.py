"""Server-side chart rendering for Onti reports (matplotlib, headless Agg).

Generic chart specs -> labelled PNG charts, plus a layered directed
impact-chain diagram rendered from ontology graph nodes/edges.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# ---------------------------------------------------------------------------
# Brand palette / style
# ---------------------------------------------------------------------------

BRAND_RED = "#ED1B2E"
BRAND_NAVY = "#0E2A47"
BRAND_INK = "#1F2933"
BRAND_GRID = "#E4E7EB"
PALETTE = [
    BRAND_RED,
    BRAND_NAVY,
    "#86BC25",
    "#62B5E5",
    "#A8518E",
    "#F2A900",
    "#0B7285",
    "#963141",
]

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.edgecolor": BRAND_GRID,
        "axes.labelcolor": BRAND_INK,
        "text.color": BRAND_INK,
        "xtick.color": BRAND_INK,
        "ytick.color": BRAND_INK,
        "axes.titlecolor": BRAND_NAVY,
    }
)


def format_value(v: Any, fmt: str = "number", decimals: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if fmt == "percent":
        return f"{x * 100:.1f}%" if abs(x) <= 1.5 else f"{x:.1f}%"
    if fmt == "currency_m":
        return f"${x:,.{decimals}f}M"
    if fmt == "currency_k":
        return f"${x:,.0f}K"
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}"
    return f"{x:,.{decimals}f}"


def _wrap(text: str, width: int = 22) -> List[str]:
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# ---------------------------------------------------------------------------
# Generic data charts
# ---------------------------------------------------------------------------


def render_chart(spec: Dict[str, Any]) -> bytes:
    """Return PNG bytes for a generic chart spec.

    spec: {type: bar|hbar|line|donut, title, labels[], series[{name, values[]}],
           fmt, unit, interpretation?}
    """
    ctype = spec.get("type", "bar")
    labels = [str(l) for l in spec.get("labels", [])]
    series = spec.get("series", [])
    fmt = spec.get("fmt", "number")
    fig, ax = plt.subplots(figsize=(7.6, 4.2), dpi=150)
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.27)

    def tag(x, xi, y, ha="center", dy=0, dx=0, fs=7.6):
        ax.annotate(
            format_value(x, fmt),
            (xi, y),
            ha=ha,
            va="bottom" if dy >= 0 else "top",
            fontsize=fs,
            color=BRAND_NAVY,
            xytext=(dx, 3 + dy),
            textcoords="offset points",
        )

    if ctype in ("donut", "pie"):
        vals = series[0]["values"] if series else []
        colors = PALETTE[: len(vals)]
        wedges, _ = ax.pie(
            vals,
            colors=colors,
            startangle=90,
            wedgeprops={
                "width": 0.42 if ctype == "donut" else 0,
                "edgecolor": "white",
                "linewidth": 1.5,
            },
        )
        ax.legend(
            wedges,
            [f"{l}  {format_value(v, fmt)}" for l, v in zip(labels, vals)],
            loc="center left",
            bbox_to_anchor=(0.92, 0.5),
            frameon=False,
            fontsize=8,
        )
        ax.set_aspect("equal")
    elif ctype == "hbar":
        s = series[0]
        vals = list(s["values"])
        y = range(len(vals))
        bars = ax.barh(list(y), vals, color=PALETTE[0], height=0.62)
        ax.set_yticks(list(y), ["\n".join(_wrap(l, 18)) for l in labels])
        ax.invert_yaxis()
        ax.grid(axis="x", color=BRAND_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for rect, v in zip(bars, vals):
            ax.annotate(
                format_value(v, fmt),
                (rect.get_width(), rect.get_y() + rect.get_height() / 2),
                ha="left",
                va="center",
                fontsize=8,
                color=BRAND_NAVY,
                xytext=(4, 0),
                textcoords="offset points",
            )
        fig.subplots_adjust(left=0.24, right=0.95, top=0.84, bottom=0.10)
    elif ctype == "line":
        for i, s in enumerate(series):
            xs = range(len(s["values"]))
            ax.plot(
                list(xs),
                s["values"],
                marker="o",
                linewidth=2.2,
                color=PALETTE[i % len(PALETTE)],
                label=s.get("name", f"Series {i+1}"),
            )
            for xi, v in zip(xs, s["values"]):
                tag(v, xi, v)
        ax.set_xticks(range(len(labels)), ["\n".join(_wrap(l, 12)) for l in labels])
        ax.grid(color=BRAND_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        if len(series) > 1:
            ax.legend(
                frameon=False,
                fontsize=8,
                ncol=min(len(series), 3),
                columnspacing=1.4,
                loc="upper right",
                bbox_to_anchor=(1.0, 1.09),
            )
    else:  # grouped vertical bars (default)
        nser = len(series)
        width = 0.72 / max(nser, 1)
        for i, s in enumerate(series):
            xs = [j + (i - (nser - 1) / 2) * width for j in range(len(s["values"]))]
            bars = ax.bar(
                xs,
                s["values"],
                width=width,
                color=PALETTE[i % len(PALETTE)],
                label=s.get("name", ""),
            )
            for xi, rect in zip(xs, bars):
                # stagger labels across series so neighbouring bars don't collide
                shift = i - (nser - 1) / 2
                tag(
                    rect.get_height(),
                    xi,
                    rect.get_height(),
                    dy=shift * 11,
                    dx=shift * 10,
                    fs=7.2 if nser > 1 else 8,
                )
        ax.set_xticks(range(len(labels)), ["\n".join(_wrap(l, 11)) for l in labels])
        ax.grid(axis="y", color=BRAND_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        if nser > 1:
            ax.legend(
                frameon=False,
                fontsize=8,
                ncol=min(nser, 3),
                columnspacing=1.4,
                loc="upper right",
                bbox_to_anchor=(1.0, 1.09),
            )
    ax.set_title(
        str(spec.get("title", "")), fontsize=13, fontweight="bold", loc="left", pad=30
    )
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    import io

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Impact-chain diagram
# ---------------------------------------------------------------------------


def _layer_positions(
    nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
) -> Dict[str, int]:
    ids = [n["id"] for n in nodes]
    level: Dict[str, int] = {}
    # seed: explicit layer field if present (smaller = upstream/left)
    for n in nodes:
        if n.get("level") is not None:
            level[n["id"]] = int(n["level"])
    adj: Dict[str, List[str]] = {i: [] for i in ids}
    indeg: Dict[str, int] = {i: 0 for i in ids}
    for e in edges:
        sources = (
            str(e["source"]).split(",")
            if isinstance(e["source"], str)
            else [e["source"]]
        )
        for s in sources:
            s, t = s.strip(), e["target"]
            if s in adj and t in indeg:
                adj[s].append(t)
                indeg[t] += 1
    # longest-path layering (cycle-safe)
    order = [i for i in ids if indeg.get(i, 0) == 0]
    seen = set(order)
    qi = 0
    while qi < len(order):
        cur = order[qi]
        qi += 1
        for t in adj.get(cur, []):
            indeg[t] -= 1
            level[t] = max(level.get(t, 0), level.get(cur, 0) + 1)
            if indeg[t] <= 0 and t not in seen:
                seen.add(t)
                order.append(t)
    for i in ids:
        level.setdefault(i, 0)
    return level


def render_chain_graph(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    title: str = "Ontology Impact Chain",
) -> bytes:
    """Layered left-to-right DAG. nodes: {id,label,grp?,value?}; edges:
    {id?,source(comma OK),target,label?,edge_type?}."""
    if not nodes:
        nodes = [{"id": "_empty", "label": "No chain data captured in this scope"}]
    levels = _layer_positions(nodes, edges)
    by_level: Dict[int, List[str]] = {}
    for nid, lv in levels.items():
        by_level.setdefault(lv, []).append(nid)
    n_layers = max(levels.values()) + 1 if levels else 1
    # preserve input order inside each layer
    order_idx = {n["id"]: i for i, n in enumerate(nodes)}
    for lv in by_level:
        by_level[lv].sort(key=lambda x: order_idx.get(x, 999))

    grp_color = {
        "financial": BRAND_RED,
        "operational": BRAND_NAVY,
        "product": "#86BC25",
        "assumption": "#62B5E5",
        "agent": "#A8518E",
        "channel": "#F2A900",
    }
    pos: Dict[str, Tuple[float, float]] = {}
    max_rows = max((len(v) for v in by_level.values()), default=1)
    fig_w = max(8.6, 2.55 * n_layers + 1.2)
    fig_h = max(3.4, 1.15 * max_rows + 1.6)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.04)
    x_by_layer = {lv: i for i, lv in enumerate(sorted(by_level))}
    for lv, ids_in in by_level.items():
        x = x_by_layer[lv]
        for j, nid in enumerate(ids_in):
            y = (len(ids_in) - 1) / 2 - j
            pos[nid] = (x, y)

    box_w, box_h = 0.30, 0.38
    for e in edges:
        sources = [s.strip() for s in str(e["source"]).split(",")]
        tgt = e["target"]
        if tgt not in pos:
            continue
        for s in sources:
            if s not in pos:
                continue
            x0, y0 = pos[s]
            x1, y1 = pos[tgt]
            arrow = FancyArrowPatch(
                (x0 + box_w, y0),
                (x1 - box_w, y1),
                arrowstyle="-|>",
                mutation_scale=12,
                linewidth=1.3,
                color="#7B8794",
                connectionstyle="arc3,rad=0.0",
                zorder=1,
            )
            ax.add_patch(arrow)
            lab = str(e.get("label") or "")[:18]
            # Only label long, mostly-horizontal spans; short spans have no room.
            if lab and abs(x1 - x0) >= 1.25 and abs(y1 - y0) <= 0.6:
                ax.text(
                    (x0 + x1) / 2,
                    max(y0, y1) + 0.16,
                    lab,
                    fontsize=7,
                    color="#7B8794",
                    ha="center",
                    va="bottom",
                    style="italic",
                    zorder=2,
                    bbox=dict(
                        boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85
                    ),
                )

    node_meta = {n["id"]: n for n in nodes}
    for nid, (x, y) in pos.items():
        meta = node_meta.get(nid, {})
        color = grp_color.get(str(meta.get("grp", "")).lower(), BRAND_NAVY)
        box = FancyBboxPatch(
            (x - box_w, y - box_h / 2),
            box_w * 2,
            box_h,
            boxstyle="round,pad=0.02,rounding_size=0.06",
            facecolor="white",
            edgecolor=color,
            linewidth=1.8,
            zorder=3,
        )
        ax.add_patch(box)
        ax.plot(
            [x - box_w, x - box_w],
            [y - box_h / 2, y + box_h / 2],
            color=color,
            linewidth=4,
            zorder=4,
            solid_capstyle="butt",
        )
        label_lines = _wrap(meta.get("label", nid), 18)[:3]
        ax.text(
            x,
            y + (0.07 if meta.get("value") else 0),
            "\n".join(label_lines),
            ha="center",
            va="center",
            fontsize=8.2,
            fontweight="bold",
            color=BRAND_NAVY,
            zorder=5,
        )
        if meta.get("value"):
            ax.text(
                x,
                y - 0.12,
                "\n".join(_wrap(str(meta["value"])[:34], 22)[:2]),
                ha="center",
                va="center",
                fontsize=7.0,
                color=color,
                fontweight="bold",
                zorder=5,
            )

    ax.set_xlim(-0.6, n_layers - 0.4)
    ax.set_ylim(-max_rows / 2 - 0.7, max_rows / 2 + 0.7)
    ax.axis("off")
    ax.set_title(
        title, fontsize=13, fontweight="bold", color=BRAND_NAVY, loc="left", pad=8
    )

    legend_items = [
        (grp_color[g], g.capitalize())
        for g in ("financial", "operational", "product", "assumption")
        if any(str(n.get("grp", "")).lower() == g for n in nodes)
    ]
    for i, (c, lab) in enumerate(legend_items):
        ax.scatter([], [], color=c, s=40, label=lab)
    if legend_items:
        ax.legend(
            frameon=False,
            fontsize=8,
            ncol=len(legend_items),
            loc="lower left",
            bbox_to_anchor=(0.0, -0.02),
        )
    import io

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Auto chart specs from instance tables (generic, pack-independent)
# ---------------------------------------------------------------------------


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def auto_chart_specs(
    tables: List[Dict[str, Any]], max_charts: int = 6
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Inspect active-dataset tables and synthesize chart specs.

    Returns (specs, interpretations). Tables with 2..15 rows and numeric
    columns produce one grouped-bar chart each (up to 3 numeric columns);
    a single positive-value numeric column with <=6 rows becomes a donut.
    """
    specs: List[Dict[str, Any]] = []
    notes: List[str] = []
    for tbl in tables:
        rows = tbl.get("rows") or []
        cols = tbl.get("columns") or []
        if not (2 <= len(rows) <= 15):
            continue
        num_cols = [
            c
            for c in cols
            if c.get("data_type") in ("INTEGER", "FLOAT") and not c.get("expression")
        ]
        label_cols = [c for c in cols if c.get("data_type") == "STRING"]
        if not num_cols or not label_cols:
            continue

        # Prefer a human name column; avoid raw id/code keys when names exist.
        def _label_score(c):
            key = (c.get("key") or "").lower()
            lab = (c.get("label") or "").lower()
            if "name" in key or "name" in lab:
                return 0
            if key in ("label", "title") or "label" in lab:
                return 1
            if any(t in key for t in ("id", "code", "key")):
                return 3
            return 2

        label_col = sorted(label_cols, key=_label_score)[0]
        label_key = label_col["key"]
        labels = [str(r.get(label_key, "")) for r in rows]
        chosen = num_cols[:3]
        series = []
        for c in chosen:
            vals = [float(r[c["key"]]) for r in rows if _is_num(r.get(c["key"]))]
            if len(vals) == len(rows):
                series.append(
                    {
                        "name": c.get("label") or c["key"],
                        "values": vals,
                        "fmt": c.get("fmt", "number"),
                    }
                )
        if not series:
            continue
        ctype = "bar"
        only = series[0]
        if len(series) == 1 and len(rows) <= 6 and all(v >= 0 for v in only["values"]):
            ctype = "donut"
        spec = {
            "type": ctype,
            "title": tbl.get("label") or tbl["key"],
            "labels": labels,
            "series": series,
            "fmt": only["fmt"],
            "table_key": tbl["key"],
        }
        specs.append(spec)
        # Deterministic one-line interpretation.
        vals = only["values"]
        hi = max(range(len(vals)), key=lambda i: vals[i])
        lo = min(range(len(vals)), key=lambda i: vals[i])

        def _lbl(i):
            t = (labels[i] or "").strip()
            return t if t else f"item {i + 1}"

        note = (
            f"Highest **{_lbl(hi)}** at {format_value(vals[hi], only['fmt'])}; "
            f"lowest **{_lbl(lo)}** at {format_value(vals[lo], only['fmt'])}."
        )
        if len(series) > 1:
            note += f" Grouped bars compare {', '.join(s['name'] for s in series)}."
        notes.append(note)
        if len(specs) >= max_charts:
            break
    return specs, notes
