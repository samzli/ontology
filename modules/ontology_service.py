"""Data-driven, generic insurance ontology engine.

Everything in this module is loaded from the active ontology pack in SQLite
(packs are built in the admin portal or imported from JSON). The engine
provides:

  * a SAFE expression evaluator (AST whitelist — no eval/exec, no imports)
    used for derived table columns and scalar metric formulas;
  * a deterministic rule engine (regex patterns + sandboxed Jinja templates)
    with Top-3 similar-question suggestions when no rule matches;
  * a generic visual graph, Graph-RAG subgraph retrieval and narrative;
  * a dynamic LLM grounding context built from the pack's data.

There is no hard-coded business fact in this file: the shipped VNB/APE content
lives entirely in the built-in sample pack (``modules/seed.py``).
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from modules.db import active_pack_id, get_conn, jloads

# ===========================================================================
# Safe expression evaluator
# ===========================================================================

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.And,
    ast.Or,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


class EvalError(Exception):
    """Raised when a stored expression is invalid or fails at evaluation."""


def validate_expression(expr: str, allowed_names: Optional[Set[str]] = None) -> None:
    """Parse ``expr`` and reject anything outside the safe grammar.

    Used both at evaluation time and (importantly) when an admin saves a
    formula — strings like ``__import__('os')`` are rejected with 400.
    """
    if not expr or not expr.strip():
        raise EvalError("Empty expression.")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise EvalError(f"Syntax error: {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            kind = type(node).__name__
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                raise EvalError(f"Forbidden construct in expression: {kind}")
            raise EvalError(f"Unsupported expression construct: {kind}")
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name)
            and (allowed_names is None or node.func.id in allowed_names)
        ):
            raise EvalError("Only whitelisted functions may be called.")
        if isinstance(node, ast.Name) and allowed_names is not None:
            if node.id not in allowed_names:
                raise EvalError(f"Unknown name in expression: {node.id}")
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float, str, bool, type(None))
        ):
            raise EvalError("Unsupported literal type.")


def _safe_eval(expr: str, namespace: Dict[str, Any]) -> Any:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise EvalError(f"Unsupported expression construct: {type(node).__name__}")
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name) and node.func.id in namespace
        ):
            raise EvalError("Only whitelisted functions may be called.")
        if isinstance(node, ast.Name) and node.id not in namespace:
            raise EvalError(f"Unknown name in expression: {node.id}")
    code = compile(tree, "<ontology-formula>", "eval")
    return eval(
        code, {"__builtins__": {}}, namespace
    )  # noqa: S307 - AST is whitelisted


# ===========================================================================
# Pack loading
# ===========================================================================


def _rows(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def load_pack(
    conn: sqlite3.Connection, pack_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Load a pack definition with all child collections. None if missing."""
    if pack_id is None:
        pack_id = active_pack_id(conn)
    if pack_id is None:
        return None
    meta = conn.execute(
        "SELECT * FROM ontology_packs WHERE id=?", (pack_id,)
    ).fetchone()
    if meta is None:
        return None
    pack: Dict[str, Any] = {
        "id": meta["id"],
        "slug": meta["slug"],
        "name": meta["name"],
        "description": meta["description"],
        "narrative": meta["narrative"],
        "is_builtin": bool(meta["is_builtin"]),
        "is_active": bool(meta["is_active"]),
    }
    pack["entities"] = [
        dict(r)
        for r in _rows(
            conn, "SELECT * FROM entities WHERE pack_id=? ORDER BY sort, id", (pack_id,)
        )
    ]
    pack["attributes"] = [
        dict(r)
        for r in _rows(
            conn,
            "SELECT * FROM attributes WHERE pack_id=? ORDER BY sort, id",
            (pack_id,),
        )
    ]
    pack["relations"] = [
        dict(r)
        for r in _rows(
            conn,
            "SELECT * FROM relations WHERE pack_id=? ORDER BY sort, id",
            (pack_id,),
        )
    ]
    pack["nodes"] = []
    for r in _rows(
        conn, "SELECT * FROM graph_nodes WHERE pack_id=? ORDER BY sort, id", (pack_id,)
    ):
        d = dict(r)
        d["aliases"] = jloads(r["aliases"], [])
        pack["nodes"].append(d)
    pack["edges"] = [
        dict(r)
        for r in _rows(
            conn,
            "SELECT * FROM graph_edges WHERE pack_id=? ORDER BY sort, id",
            (pack_id,),
        )
    ]
    pack["formulas"] = [
        dict(r)
        for r in _rows(
            conn,
            "SELECT * FROM metric_formulas WHERE pack_id=? ORDER BY sort, id",
            (pack_id,),
        )
    ]
    pack["rules"] = []
    for r in _rows(
        conn,
        "SELECT * FROM question_rules WHERE pack_id=? ORDER BY sort, id",
        (pack_id,),
    ):
        d = dict(r)
        d["patterns"] = jloads(r["patterns"], [])
        d["require_groups"] = jloads(r["require_groups"], [])
        d["guards"] = jloads(r["guards"], [])
        d["evidence_nodes"] = jloads(r["evidence_nodes"], [])
        d["path_steps"] = jloads(r["path_steps"], [])
        pack["rules"].append(d)

    # Declarative analytics spec (roles of tables/columns for deep analysis).
    spec_row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?", (f"analysis:{pack_id}",)
    ).fetchone()
    pack["analysis"] = jloads(spec_row["value"], {}) if spec_row else {}

    tables: List[Dict[str, Any]] = []
    for tr in _rows(
        conn, "SELECT * FROM pack_tables WHERE pack_id=? ORDER BY id", (pack_id,)
    ):
        td = dict(tr)
        td["columns"] = [
            dict(c)
            for c in _rows(
                conn,
                "SELECT * FROM pack_columns WHERE table_id=? ORDER BY sort, id",
                (tr["id"],),
            )
        ]
        dataset_id = tr["active_dataset_id"]
        if dataset_id is None:
            ds = conn.execute(
                "SELECT id FROM datasets WHERE table_id=? ORDER BY id DESC LIMIT 1",
                (tr["id"],),
            ).fetchone()
            dataset_id = ds["id"] if ds else None
        td["dataset_id"] = dataset_id
        td["rows"] = []
        if dataset_id is not None:
            for rr in _rows(
                conn,
                "SELECT data FROM pack_rows WHERE dataset_id=? ORDER BY seq, id",
                (dataset_id,),
            ):
                td["rows"].append(jloads(rr["data"], {}))
        tables.append(td)
    pack["tables"] = tables
    return pack


# ===========================================================================
# Computation — derived columns + metric formulas
# ===========================================================================


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise EvalError("a number is required")
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise EvalError(f"not a number: {value!r}") from e


def compute_pack(pack: Dict[str, Any], strict: bool = True) -> Dict[str, Any]:
    """Evaluate all derived columns and metric formulas.

    Returns ``{tables: {key: [row, ...]}, metrics: {key: value}, functions}``
    where ``functions`` is the whitelisted callable namespace bound to the
    computed tables (used by formulas and Jinja templates).

    When ``strict`` is False (read-only rendering paths such as the
    dashboard graph, narrative and AI grounding), a metric that fails
    because an input table has no rows evaluates to ``None`` instead of
    aborting the whole request; the failures are listed in
    ``metric_errors`` so authoring surfaces can still report them.
    """
    data: Dict[str, List[Dict[str, Any]]] = {}
    for tbl in pack["tables"]:
        rows = [dict(r) for r in tbl["rows"]]
        data[tbl["key"]] = rows

    def rows_of(name: Any) -> List[Dict[str, Any]]:
        # ``where`` / Jinja helpers already return plain row lists.
        if isinstance(name, list):
            return name
        if isinstance(name, dict):
            return name.get("__rows__", [])
        if not isinstance(name, str) or name not in data:
            raise EvalError(f"unknown table: {name!r}")
        return data[name]

    def where(rows_: Any, col: str, value: Any) -> List[Dict[str, Any]]:
        return [r for r in rows_of(rows_) if _eq(r.get(col), value)]

    def lookup(source: Any, key_col: str, key_val: Any, out_col: str) -> Any:
        for r in rows_of(source):
            if _eq(r.get(key_col), key_val):
                return r.get(out_col)
        return None

    def cell(table: str, col: str) -> Any:
        rs = rows_of(table)
        if not rs:
            raise EvalError(f"table {table!r} has no rows")
        return rs[0].get(col)

    def col_values(rows_: Any, col: str) -> List[float]:
        rs = rows_of(rows_)
        if not rs:
            return []
        if col not in rs[0]:
            raise EvalError(f"column not found: {col}")
        return [_num(r.get(col)) for r in rs]

    def s_sum(rows_: Any, col: str) -> float:
        vals = col_values(rows_, col)
        return sum(vals)

    def s_avg(rows_: Any, col: str) -> float:
        vals = col_values(rows_, col)
        return sum(vals) / len(vals) if vals else 0.0

    def s_min(rows_: Any, col: str) -> float:
        return min(col_values(rows_, col))

    def s_max(rows_: Any, col: str) -> float:
        return max(col_values(rows_, col))

    def s_count(rows_: Any) -> int:
        return len(rows_of(rows_))

    def wavg(rows_: Any, val_col: str, weight_col: str) -> float:
        rs = rows_of(rows_)
        if not rs:
            return 0.0
        num = den = 0.0
        for r in rs:
            w = _num(r.get(weight_col))
            num += _num(r.get(val_col)) * w
            den += w
        return num / den if den else 0.0

    def money_k(value: Any) -> str:
        return f"${_num(value):,.0f}K"

    def s_round(value: Any, ndigits: int = 0) -> float:
        return round(_num(value), int(ndigits))

    def s_abs(value: Any) -> float:
        return abs(_num(value))

    functions: Dict[str, Callable] = {
        "rows": rows_of,
        "where": where,
        "lookup": lookup,
        "cell": cell,
        "sum": s_sum,
        "avg": s_avg,
        "min": s_min,
        "max": s_max,
        "count": s_count,
        "wavg": wavg,
        "round": s_round,
        "abs": s_abs,
        "money_k": money_k,
    }

    def eval_for_row(expr: str, row: Dict[str, Any]) -> Any:
        ns = dict(functions)
        ns.update(row)
        return _safe_eval(expr, ns)

    # Fix-point over derived columns (cross-table dependencies allowed).
    derived: List[Tuple[str, str, str]] = []
    for tbl in pack["tables"]:
        for col in tbl["columns"]:
            if col.get("expression"):
                derived.append((tbl["key"], col["key"], col["expression"]))
    pending = list(derived)
    for _ in range(max(1, len(derived) + 1)):
        still: List[Tuple[str, str, str]] = []
        for tkey, ckey, expr in pending:
            ok = True
            for row in data[tkey]:
                try:
                    row[ckey] = eval_for_row(expr, row)
                except EvalError:
                    ok = False
                    break
            if not ok:
                still.append((tkey, ckey, expr))
        if not still:
            break
        if len(still) == len(pending):
            bad = ", ".join(f"{t}.{c}" for t, c, _ in still)
            raise EvalError(f"Cannot resolve derived columns (cycle/bad name): {bad}")
        pending = still

    # Metrics, evaluated in declared order (earlier names are visible).
    metrics: Dict[str, Any] = {}
    metric_errors: List[str] = []
    for f in pack["formulas"]:
        ns = dict(functions)
        ns.update(metrics)
        try:
            metrics[f["key"]] = _safe_eval(f["expression"], ns)
        except (EvalError, TypeError, ValueError, ZeroDivisionError) as e:
            if strict:
                raise EvalError(f"metric {f['key']!r}: {e}") from e
            metric_errors.append(f"{f['key']}: {e}")
            metrics[f["key"]] = None

    return {
        "tables": data,
        "metrics": metrics,
        "functions": functions,
        "metric_errors": metric_errors,
    }


def _eq(a: Any, b: Any) -> bool:
    if a == b:
        return True
    if isinstance(a, (int, float)) and isinstance(b, str):
        try:
            return a == float(b)
        except ValueError:
            return False
    if isinstance(b, (int, float)) and isinstance(a, str):
        try:
            return float(a) == b
        except ValueError:
            return False
    return False


# ===========================================================================
# Jinja rendering (answer templates + narrative)
# ===========================================================================


def _fmt_money(v: Any, digits: int = 2) -> str:
    return f"${_num(v):,.{digits}f}M"


def _fmt_pct(v: Any, digits: int = 1) -> str:
    return f"{_num(v) * 100:.{digits}f}%"


def _make_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        autoescape=False,
    )
    env.filters["money"] = lambda v: _fmt_money(v, 2)
    env.filters["money3"] = lambda v: _fmt_money(v, 3)
    env.filters["pct"] = lambda v: _fmt_pct(v, 1)
    env.filters["pct0"] = lambda v: _fmt_pct(v, 0)
    # Month helpers exposed as globals so rule templates can call them
    # directly. Jinja Sandbox permits globals set via env.globals — unlike
    # ctx variables — because they are registered before rendering.
    env.globals["month_context"] = _month_context
    env.globals["filter_by_month"] = _filter_rows_by_month
    env.globals["month_movement"] = _month_movement
    return env


def _expanded_links(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    for e in edges:
        for src in [s.strip() for s in e["source_key"].split(",") if s.strip()]:
            links.append(
                {
                    "id": e["key"],
                    "source": src,
                    "target": e["target_key"],
                    "type": e["edge_type"],
                    "label": e["label"],
                    "description": e.get("description", ""),
                }
            )
    return links


def _humanize(key: str) -> str:
    """Convert a CamelCase / acronym key to a readable label.

    Keeps consecutive uppercase runs (acronyms like VNB, APE, KPI) together
    while still separating words: ``TotalVNB`` → ``Total VNB``,
    ``BlendedVNBMargin`` → ``Blended VNB Margin``.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return " ".join(s.split())


def template_context(pack: Dict[str, Any], computed: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the full Jinja namespace shared by rules and the narrative."""
    data = computed["tables"]
    fns = dict(computed["functions"])

    def max_by(rows_: Any, col: str) -> Dict[str, Any]:
        rs = fns["rows"](rows_)
        return max(rs, key=lambda r: _num(r.get(col)))

    def min_by(rows_: Any, col: str) -> Dict[str, Any]:
        rs = fns["rows"](rows_)
        return min(rs, key=lambda r: _num(r.get(col)))

    def mix_weight(channel_id: str, product_id: str) -> Any:
        for r in data.get("mix", []):
            if _eq(r.get("channel_id"), channel_id) and _eq(
                r.get("product_id"), product_id
            ):
                return r.get("weight")
        return 0

    ctx: Dict[str, Any] = {
        "m": computed["metrics"],
        "metrics": computed["metrics"],
        "max_by": max_by,
        "min_by": min_by,
        "mw": mix_weight,
        "edge_defs": pack["edges"],
        "links": _expanded_links(pack["edges"]),
        "edge_types": _edge_type_map(pack["edges"]),
        "groups": _group_map(pack["nodes"]),
    }
    ctx.update(fns)
    for tkey, rows in data.items():
        ctx[tkey] = rows
    if "kpi" in data and data["kpi"]:
        ctx["kpi"] = data["kpi"][0]
    else:
        ctx["kpi"] = None

    # Time-series / attribution / scenario / agent analytics (optional).
    try:
        from . import analytics  # local import avoids a hard package cycle

        overview = analytics.overview(data)
        company = overview.get("company") or []
        ctx["ov"] = overview
        ctx["ts"] = company
        ctx["channels_ts"] = overview.get("channels") or []
        ctx["latest"] = company[-1] if company else None
        ctx["attr"] = overview.get("attribution")
        ctx["astats"] = overview.get("agents")

        def _scenario(multiplier: float = 2.0, planned_active: Any = None):
            return analytics.run_scenario(
                data, float(multiplier), int(planned_active) if planned_active else None
            )

        ctx["scenario"] = _scenario
        ctx["scen"] = overview.get("scenario")
        ctx["month_label"] = analytics.month_label

        def _attr_for(question_text: Any):
            """Month-aware MoM attribution: honours the month named in the
            question (or inherited from history); falls back to the latest
            month pair when no month is mentioned."""
            tgt, _prev = _month_context(
                str(question_text or ""), data.get("vnb_monthly") or []
            )
            if tgt:
                out = analytics.mom_attribution(data, tgt)
                if out:
                    return out
            return overview.get("attribution")

        ctx["attr_for"] = _attr_for
    except Exception:  # noqa: BLE001 - analytics are additive/optional
        ctx.update(
            {
                "ov": {},
                "ts": [],
                "channels_ts": [],
                "latest": None,
                "attr": None,
                "astats": None,
                "scen": None,
                "attr_for": None,
            }
        )
    return ctx


_GROUP_LABELS = {
    "financial": "Financial KPI / formula-derived component",
    "operational": "Operational driver",
    "mix": "Product-mix driver",
    "cross": "Cross-cutting dependency",
}
_EDGE_TYPE_LABELS = {
    "STRUCTURAL": "Formula-derived identity relationship",
    "OPERATIONAL": "Driver to component relationship",
    "MIX": "Weighting to blended margin relationship",
    "CROSS_LINK": "Cross-cutting multi-node dependency",
}


def _group_map(nodes: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in nodes:
        out.setdefault(n["grp"], _GROUP_LABELS.get(n["grp"], "Ontology concept node"))
    return out


def _edge_type_map(edges: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for e in edges:
        out.setdefault(
            e["edge_type"], _EDGE_TYPE_LABELS.get(e["edge_type"], e["edge_type"])
        )
    return out


# ===========================================================================
# Rule engine
# ===========================================================================


def _rule_matches(rule: Dict[str, Any], text: str) -> bool:
    if any(re.search(p, text, re.IGNORECASE) for p in rule.get("guards", [])):
        return False
    if rule["patterns"]:
        if not any(re.search(p, text, re.IGNORECASE) for p in rule["patterns"]):
            return False
    for group in rule.get("require_groups", []):
        if not any(re.search(p, text, re.IGNORECASE) for p in group):
            return False
    # A rule with no patterns and no require_groups never matches by itself.
    return bool(rule["patterns"] or rule.get("require_groups"))


def answer_question(
    question: str,
    pack: Optional[Dict[str, Any]] = None,
    computed: Optional[Dict[str, Any]] = None,
    raise_on_template_error: bool = True,
    history: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a deterministic rule answer, or None when no rule matches.

    With ``raise_on_template_error=False`` (live Q&A), a matched rule
    whose template fails (e.g. an input table has no rows yet) is skipped
    so the caller can fall back to AI or suggestions instead of failing
    the whole request.

    ``history`` (oldest-first ``{role, content}`` turns) lets follow-up
    questions such as "How does it compare to the previous month?" inherit
    the month named in an earlier turn — the rules engine itself stays
    stateless.
    """
    if pack is None:
        with get_conn() as conn:
            pack = load_pack(conn)
    if not pack or not question.strip():
        return None
    if computed is None:
        computed = compute_pack(pack, strict=False)
    text = question.lower()
    # Month inheritance: when the current question names no month, borrow the
    # most recent month mentioned by the user earlier in the conversation so
    # data-driven templates (month_context) stay focused on the same period.
    effective_question = question
    if history and not _extract_month_prefixes(question):
        for msg in reversed(history):
            if (msg or {}).get("role") != "user":
                continue
            content = str((msg or {}).get("content") or "")
            if content and _extract_month_prefixes(content):
                effective_question = question + " " + content
                break
    for rule in pack["rules"]:
        if not _rule_matches(rule, text):
            continue
        ctx = template_context(pack, computed)
        # Inject the (month-enriched) user question so templates can look for
        # specific months / segments before rendering (e.g. Q18 Sep vs full).
        ctx["question"] = effective_question
        try:
            body = _make_env().from_string(rule["template"]).render(**ctx).strip()
        except (
            TemplateError,
            EvalError,
            KeyError,
            ValueError,
            AttributeError,
            TypeError,
        ) as e:
            if raise_on_template_error:
                raise EvalError(f"rule {rule['key']!r} template failed: {e}") from e
            continue
        return {
            "rule_key": rule["key"],
            "category": rule["category"],
            "answer": body,
            "nodes": rule["evidence_nodes"],
            "path": (
                {"type": "path", "steps": rule["path_steps"]}
                if rule["path_steps"]
                else None
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Month matcher used by data-driven rule templates (Q18 etc.).
# ---------------------------------------------------------------------------

_MONTH_KEYWORDS: List[Tuple[str, str]] = [
    ("january", "01"),
    ("jan", "01"),
    ("january", "01"),
    ("february", "02"),
    ("feb", "02"),
    ("feburary", "02"),
    ("march", "03"),
    ("mar", "03"),
    ("april", "04"),
    ("apr", "04"),
    ("may", "05"),
    ("june", "06"),
    ("jun", "06"),
    ("july", "07"),
    ("jul", "07"),
    ("august", "08"),
    ("aug", "08"),
    ("september", "09"),
    ("sep", "09"),
    ("sept", "09"),
    ("october", "10"),
    ("oct", "10"),
    ("november", "11"),
    ("nov", "11"),
    ("december", "12"),
    ("dec", "12"),
]
_MONTH_NUM_TO_NAME: Dict[str, str] = {
    "01": "January",
    "02": "February",
    "03": "March",
    "04": "April",
    "05": "May",
    "06": "June",
    "07": "July",
    "08": "August",
    "09": "September",
    "10": "October",
    "11": "November",
    "12": "December",
}


def _extract_month_prefixes(text: str) -> List[str]:
    """Return month prefixes found in *text*, in the ORDER THEY APPEAR.

    Handles Jan/Sep keyword form and ``1月`` / ``12月`` Chinese form.
    When only a month keyword is found (no year) the function tries to
    infer a year from any 4-digit number in the text; otherwise it leaves
    the year blank so callers can match across all years in the data.

    Crucially the scan is left-to-right, not ordered by the month keyword
    table. Otherwise a question such as "VNB in april and how does it
    compare to march?" returns ``['-03', '-04']`` and downstream matchers
    pick March as the target — completely ignoring April, which is
    obviously not what the user asked for.
    """
    lowered = text.lower()
    years = re.findall(r"\b(20\d{2})\b", text)

    # Collect every month hit with its text offset, then sort by offset.
    hits: List[Tuple[int, str]] = []
    for kw, num in _MONTH_KEYWORDS:
        for m in re.finditer(r"\b" + kw + r"\b", lowered):
            hits.append((m.start(), num))
    for m in re.finditer(r"(\d{1,2})\s*月", text):
        hits.append((m.start(), f"{int(m.group(1)):02d}"))
    hits.sort(key=lambda x: x[0])

    month_nums: List[str] = []
    for _, num in hits:
        if num not in month_nums:
            month_nums.append(num)

    if not month_nums:
        return []

    out: List[str] = []
    # If there's an explicit year pair every month with it. Otherwise emit
    # just the month suffix so downstream matchers can cross all years.
    if years:
        for y in years:
            for m in month_nums:
                out.append(f"{y}-{m}")
    else:
        for m in month_nums:
            out.append(f"-{m}")  # year-less sentinel
    return out


def _make_month_matcher(
    question: str,
) -> Callable[[List[Any]], Tuple[Optional[str], Optional[str]]]:
    """Return a helper usable inside rule templates.

    Given a ``list`` of date-ish rows (or raw strings) the helper finds
    months matching the question and returns
    ``(target_prefix_or_None, prev_month_prefix_or_None)``.

    When the question names *two or more* months, the helper picks the
    **latest matching month in the data** as ``target`` so ``prev`` falls
    out to the month immediately before it — which is always meaningful
    even when the chosen month is the first keyword mentioned in the
    question. A single-month question keeps the "first match" behaviour
    so ``prev`` is its natural predecessor.

    Example::

        {% set tgt, prev = month_match(me) %}
        {% if tgt %}
          Sep 2024: … vs Aug 2024: …
        {% else %}
          Full-dataset summary.
        {% endif %}
    """
    targets = _extract_month_prefixes(question)
    pick_last_match = len(targets) >= 2

    def _helper(rows: List[Any]) -> Tuple[Optional[str], Optional[str]]:
        if not rows or not targets:
            return None, None

        # normalise row.report_month or row['report_month'] values
        month_values: List[str] = []
        for r in rows:
            if isinstance(r, dict):
                v = r.get("report_month") or r.get("month") or r.get("period")
            else:
                v = (
                    getattr(r, "report_month", None)
                    or getattr(r, "month", None)
                    or getattr(r, "period", None)
                )
            if v is None:
                continue
            s = str(v).strip()
            # strip time-of-day so 2024-09-01 00:00:00 → 2024-09
            s = re.split(r"[ T]", s, maxsplit=1)[0][:7]
            month_values.append(s)

        distinct = sorted(set(month_values))
        if not distinct:
            return None, None

        # find matching months
        matches: List[str] = []
        for mv in distinct:
            for tgt in targets:
                hit = False
                if tgt.startswith("-"):
                    if mv.endswith(tgt):
                        hit = True
                elif mv.startswith(tgt):
                    hit = True
                if hit:
                    matches.append(mv)
                    break

        if not matches:
            return None, None

        chosen = matches[-1] if pick_last_match else matches[0]
        idx = distinct.index(chosen)
        prev = distinct[idx - 1] if idx > 0 else None
        return chosen, prev

    return _helper


def _filter_rows_by_month(rows: List[Any], prefix: str) -> List[Any]:
    """Return rows whose report_month normalised ``YYYY-MM`` equals *prefix*."""
    if not rows or not prefix:
        return []
    out: List[Any] = []
    for r in rows:
        if isinstance(r, dict):
            v = r.get("report_month") or r.get("month") or r.get("period")
        else:
            v = (
                getattr(r, "report_month", None)
                or getattr(r, "month", None)
                or getattr(r, "period", None)
            )
        if v is None:
            continue
        s = str(v).strip()
        s = re.split(r"[ T]", s, maxsplit=1)[0][:7]
        if s == prefix:
            out.append(r)
    return out


def _month_context(
    question: str, rows: List[Any]
) -> Tuple[Optional[str], Optional[str]]:
    """One-shot helper for rule templates.

    Finds the month requested in *question* within *rows* and returns
    ``(target_prefix, prev_prefix)`` — or ``(None, None)`` when no
    explicit month was mentioned or nothing matches in the data.
    """
    targets = _extract_month_prefixes(question)
    if not targets or not rows:
        return None, None
    pick_last_match = len(targets) >= 2
    # normalise month values
    month_values: List[str] = []
    for r in rows:
        if isinstance(r, dict):
            v = r.get("report_month") or r.get("month") or r.get("period")
        else:
            v = (
                getattr(r, "report_month", None)
                or getattr(r, "month", None)
                or getattr(r, "period", None)
            )
        if v is None:
            continue
        s = str(v).strip()
        s = re.split(r"[ T]", s, maxsplit=1)[0][:7]
        month_values.append(s)
    distinct = sorted(set(month_values))
    if not distinct:
        return None, None

    matches: List[str] = []
    for mv in distinct:
        for tgt in targets:
            hit = False
            if tgt.startswith("-"):
                if mv.endswith(tgt):
                    hit = True
            elif mv.startswith(tgt):
                hit = True
            if hit:
                matches.append(mv)
                break
    if not matches:
        return None, None

    chosen = matches[-1] if pick_last_match else matches[0]

    # Year-less mention ("Sep"): when several years contain that month,
    # prefer the most recent occurrence — latest experience is the usual
    # business intent and keeps answers consistent across rules.
    if all(t.startswith("-") for t in targets):
        later = [mv for mv in distinct if mv.endswith(targets[0])]
        if later and later[-1] != chosen:
            chosen = later[-1]

    idx = distinct.index(chosen)
    prev = distinct[idx - 1] if idx > 0 else None
    return chosen, prev


# ---------------------------------------------------------------------------
# Month-over-month movement helper (Q19/Q20 style rule templates).
# ---------------------------------------------------------------------------

_COHORT_DIMS = ("plangroup", "product", "policy_year", "channel_nm", "channel")


def _row_month_key(r: Any) -> Optional[str]:
    """Normalised ``YYYY-MM`` of a row's month column, or None."""
    if isinstance(r, dict):
        v = r.get("report_month") or r.get("month") or r.get("period")
    else:
        v = (
            getattr(r, "report_month", None)
            or getattr(r, "month", None)
            or getattr(r, "period", None)
        )
    if v is None:
        return None
    s = str(v).strip()
    return re.split(r"[ T]", s, maxsplit=1)[0][:7]


def _stated_rate(r: Dict[str, Any]) -> Optional[float]:
    v = r.get("actual_lapse_rate")
    if v in (None, ""):
        v = r.get("actual_rate")
    return None if v in (None, "") else _num(v)


def _expected_rate(r: Dict[str, Any]) -> Optional[float]:
    v = r.get("expected_lapse_rate")
    if v in (None, ""):
        v = r.get("expected_rate")
    return None if v in (None, "") else _num(v)


def _month_aggregate(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate lapse-style rows for one month.

    Mirrors the Q18 conventions: the actual rate is annualised from
    counts (lapses / exposure × 12) when no stated rate column exists,
    otherwise the stated rates are exposure-weighted; expected rate is a
    plain average; A/E falls back to actual ÷ expected when no explicit
    ``a_e_ratio`` column is present.
    """
    exp = sum(_num(r.get("exposure")) for r in rs)
    lapses = sum(_num(r.get("actual_lapses")) for r in rs)
    stated = [
        (v, _num(r.get("exposure")))
        for r, v in ((r, _stated_rate(r)) for r in rs)
        if v is not None
    ]
    if stated:
        wtot = sum(w for _v, w in stated)
        rate = (
            sum(v * w for v, w in stated) / wtot
            if wtot
            else sum(v for v, _w in stated) / len(stated)
        )
    elif exp and lapses:
        rate = lapses / exp * 12
    else:
        rate = 0.0
    ev = [v for v in (_expected_rate(r) for r in rs) if v is not None]
    expected = sum(ev) / len(ev) if ev else 0.0
    ae_vals = [_num(r["a_e_ratio"]) for r in rs if r.get("a_e_ratio") not in (None, "")]
    ae = (
        sum(ae_vals) / len(ae_vals)
        if ae_vals
        else (rate / expected if expected else None)
    )
    return {
        "rows": len(rs),
        "exposure": exp,
        "lapses": lapses,
        "actual_rate": rate,
        "expected_rate": expected,
        "a_e": ae,
    }


def _month_movement(
    rows: List[Dict[str, Any]], tgt: Optional[str], prev: Optional[str]
) -> Dict[str, Any]:
    """Aggregate two months and rank per-cohort movers.

    When *tgt* is None (no month named in the question or inherited from
    history) the latest two distinct months in *rows* are used so the
    template still answers sensibly.
    """
    if not tgt:
        months = sorted({m for m in (_row_month_key(r) for r in rows) if m})
        if len(months) >= 2:
            tgt, prev = months[-1], months[-2]
        elif months:
            tgt, prev = months[0], None
    cur = _filter_rows_by_month(rows, tgt) if tgt else []
    prv = _filter_rows_by_month(rows, prev) if prev else []

    dims = [d for d in _COHORT_DIMS if any(d in r for r in cur + prv)]

    def _key_of(r: Dict[str, Any]) -> Tuple[str, ...]:
        return tuple(str(r.get(d, "")) for d in dims)

    cur_m: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    prv_m: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for r in cur:
        cur_m.setdefault(_key_of(r), []).append(r)
    for r in prv:
        prv_m.setdefault(_key_of(r), []).append(r)
    movers: List[Dict[str, Any]] = []
    for k in sorted(set(cur_m) | set(prv_m)):
        # Only cohorts present in BOTH months are ranked — a cohort that
        # appears/disappears would show a misleading "0% → x%" delta.
        if not (cur_m.get(k) and prv_m.get(k)):
            continue
        ca = _month_aggregate(cur_m[k])
        pa = _month_aggregate(prv_m[k])
        label = " · ".join(x for x in k if x != "") or "(all)"
        movers.append(
            {
                "label": label,
                "cur_rate": ca["actual_rate"],
                "prev_rate": pa["actual_rate"],
                "delta_ppt": (ca["actual_rate"] - pa["actual_rate"]) * 100,
                "cur_ae": ca["a_e"],
                "prev_ae": pa["a_e"],
            }
        )
    movers.sort(key=lambda m: -abs(m["delta_ppt"]))
    return {
        "tgt": tgt,
        "prev": prev,
        "cur": _month_aggregate(cur),
        "prev_agg": _month_aggregate(prv),
        "movers": movers,
    }


# ===========================================================================
# Similar question suggestions (deterministic, no LLM)
# ===========================================================================

_STOP = {
    "what",
    "which",
    "the",
    "is",
    "are",
    "do",
    "does",
    "how",
    "why",
    "a",
    "an",
    "of",
    "in",
    "on",
    "to",
    "for",
    "and",
    "or",
    "with",
    "has",
    "have",
    "per",
    "at",
    "be",
    "can",
    "i",
    "it",
    "this",
    "that",
    "many",
    "most",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> Set[str]:
    toks = {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1}
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    toks.update(cjk)
    # CJK bigrams help match short Chinese questions.
    toks.update({a + b for a, b in zip(cjk, cjk[1:])})
    return toks


def similar_questions(
    question: str, limit: int = 3, pack_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    q_tokens = _tokens(question)
    with get_conn() as conn:
        if pack_id is None:
            pack_id = active_pack_id(conn)
        if pack_id is None:
            return []
        bank = [
            dict(r)
            for r in conn.execute(
                "SELECT question, category, source FROM question_bank WHERE pack_id=? "
                "ORDER BY (source='canonical') DESC, id",
                (pack_id,),
            ).fetchall()
        ]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    q_lower = question.lower().strip()
    for item in bank:
        bt = _tokens(item["question"])
        overlap = len(q_tokens & bt)
        if overlap == 0:
            continue
        score = float(overlap) / (len(bt) or 1)
        if item["question"].lower() in q_lower or q_lower in item["question"].lower():
            score += 1.0
        scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]["question"]))
    picked = [item for _, item in scored[:limit]]
    if len(picked) < limit:
        have = {p["question"] for p in picked}
        for item in bank:
            if item["question"] not in have:
                picked.append(item)
                have.add(item["question"])
                if len(picked) >= limit:
                    break
    return picked[:limit]


def search_ontology(
    pack: Dict[str, Any], query: str, limit: int = 20
) -> List[Dict[str, Any]]:
    """Full-text search across entities, attributes, relations, formulas, rules.

    Matches key, label, description (and expression/template for formulas/rules).
    Results ranked by field weight (label > key > description).
    """
    q = query.strip().lower()
    if not q:
        return []
    results: List[Tuple[float, Dict[str, Any]]] = []

    def add(kind: str, obj: Dict[str, Any], text: str, weight: float) -> None:
        if q in text.lower():
            results.append(
                (
                    weight,
                    {
                        "kind": kind,
                        "key": obj.get("key", ""),
                        "label": obj.get("label")
                        or obj.get("label_en")
                        or obj.get("key", ""),
                        "snippet": text[:120],
                    },
                )
            )

    for e in pack.get("entities", []):
        add("entity", e, e.get("label_en", ""), 3.0)
        add("entity", e, e.get("key", ""), 2.0)
        add("entity", e, e.get("description", ""), 1.0)
    for a in pack.get("attributes", []):
        add("attribute", a, a.get("label_en", ""), 3.0)
        add("attribute", a, a.get("key", ""), 2.0)
        add("attribute", a, a.get("description", ""), 1.0)
    for r in pack.get("relations", []):
        add("relation", r, r.get("label_en", ""), 3.0)
        add("relation", r, r.get("key", ""), 2.0)
        add("relation", r, r.get("description", ""), 1.0)
    for f in pack.get("formulas", []):
        add("formula", f, f.get("label", ""), 3.0)
        add("formula", f, f.get("key", ""), 2.0)
        add("formula", f, f.get("expression", ""), 1.0)
    for rl in pack.get("rules", []):
        add("rule", rl, rl.get("template", ""), 2.0)
        add("rule", rl, rl.get("key", ""), 2.0)

    # dedupe by (kind, key) keeping highest weight
    seen: Dict[Tuple[str, str], float] = {}
    deduped: List[Tuple[float, Dict[str, Any]]] = []
    for w, r in results:
        key = (r["kind"], r["key"])
        if key not in seen or w > seen[key]:
            seen[key] = w
            deduped.append((w, r))
    deduped.sort(key=lambda x: (-x[0], x[1]["kind"], x[1]["key"]))
    return [r for _, r in deduped[:limit]]


def suggested_questions(pack_id: Optional[int] = None) -> List[str]:
    with get_conn() as conn:
        if pack_id is None:
            pack_id = active_pack_id(conn)
        if pack_id is None:
            return []
        rows = conn.execute(
            "SELECT question FROM question_bank WHERE pack_id=? "
            "ORDER BY (source='canonical') DESC, id",
            (pack_id,),
        ).fetchall()
    return [r["question"] for r in rows]


# ===========================================================================
# Visual graph + Graph-RAG retrieval + narrative
# ===========================================================================


def pack_tables(pack: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Instance rows keyed by table key — the input shape for analytics."""
    return {t["key"]: t.get("rows", []) for t in pack.get("tables", [])}


def library_payload(pack: Dict[str, Any], pid: int) -> Dict[str, Any]:
    """Read-only glossary + data inventory for the user portal library view."""
    tables = []
    for t in pack.get("tables", []):
        rows = t.get("rows", [])
        tables.append(
            {
                "id": t["id"],
                "key": t["key"],
                "label": t.get("label") or t["key"],
                "description": t.get("description", ""),
                "dataset": t.get("dataset_name"),
                "columns": [
                    {
                        "key": c["key"],
                        "label": c.get("label") or c["key"],
                        "data_type": c["data_type"],
                        "derived": bool(c.get("is_derived")),
                    }
                    for c in t.get("columns", [])
                ],
                "row_count": len(rows),
                "preview": rows[:50],
            }
        )
    return {
        "pack": {
            "id": pid,
            "name": pack["name"],
            "description": pack.get("description", ""),
        },
        "entities": [dict(e) for e in pack.get("entities", [])],
        "attributes": [dict(a) for a in pack.get("attributes", [])],
        "relations": [dict(r) for r in pack.get("relations", [])],
        "formulas": [dict(f) for f in pack.get("formulas", [])],
        "tables": tables,
    }


# -- layer/cardinality → graph group/edge type mappings --
_LAYER_TO_GRP = {1: "financial", 2: "financial", 3: "operational", 4: "mix", 5: "cross"}
_CARD_TO_EDGE = {
    "ONE_TO_ONE": "STRUCTURAL",
    "ONE_TO_MANY": "STRUCTURAL",
    "MANY_TO_ONE": "STRUCTURAL",
    "MANY_TO_MANY": "CROSS_LINK",
}


def _auto_generate_graph(pack: Dict[str, Any]) -> None:
    """Derive graph_nodes/graph_edges from entities/relations when empty.

    Mutates *pack* in-place so that ``visual_graph`` has data to render even
    when the pack defines no explicit visual graph layer.
    """
    entities = pack.get("entities", [])
    relations = pack.get("relations", [])

    if not pack.get("nodes") and entities:
        pack["nodes"] = [
            {
                "key": e["key"],
                "label": e.get("label_en") or _humanize(e["key"]),
                "grp": _LAYER_TO_GRP.get(e.get("layer", 3), "operational"),
                "value": "",
                "description": e.get("description", ""),
                "aliases": "[]",
                "is_core": 0,
                "sort": i,
            }
            for i, e in enumerate(entities)
        ]

    if not pack.get("edges") and relations:
        pack["edges"] = [
            {
                "key": r["key"],
                "source_key": r["source_key"],
                "target_key": r["target_key"],
                "edge_type": _CARD_TO_EDGE.get(r.get("cardinality", ""), "STRUCTURAL"),
                "label": r.get("label_en") or r["key"],
                "description": r.get("description", ""),
                "sort": i,
            }
            for i, r in enumerate(relations)
        ]


def visual_graph(pack: Dict[str, Any]) -> Dict[str, Any]:
    _auto_generate_graph(pack)
    node_meta = {n["key"]: n for n in pack["nodes"]}
    ordered_keys = [n["key"] for n in pack["nodes"]]
    links = _expanded_links(pack["edges"])

    present: List[str] = []
    for link in links:
        for key in (link["source"], link["target"]):
            if key not in present:
                present.append(key)
    # include isolated defined nodes too
    for key in ordered_keys:
        if key not in present:
            present.append(key)

    nodes = []
    for key in present:
        meta = node_meta.get(key)
        if meta:
            nodes.append(
                {
                    "id": key,
                    "label": meta.get("label") or _humanize(key),
                    "group": meta["grp"],
                    "value": meta.get("value", ""),
                    "desc": meta.get("description", ""),
                }
            )
        else:
            nodes.append(
                {
                    "id": key,
                    "label": _humanize(key),
                    "group": "financial",
                    "value": "",
                    "desc": "Ontology concept node.",
                }
            )
    return {
        "nodes": nodes,
        "links": links,
        "groups": _group_map(pack["nodes"]),
        "edge_types": _edge_type_map(pack["edges"]),
        "edge_descriptions": {e["key"]: e["description"] for e in pack["edges"]},
    }


def class_graph(pack: Dict[str, Any]) -> Dict[str, Any]:
    """Build a class-level relationship graph from entities + relations.

    Unlike :func:`visual_graph` which uses the ``graph_nodes`` / ``graph_edges``
    tables, this view shows the ontology class structure directly — entities
    become nodes and ``relations`` become labelled edges with cardinality.
    """
    nodes: List[Dict[str, Any]] = []
    for e in pack.get("entities", []):
        nodes.append(
            {
                "id": e["key"],
                "label": e.get("label_en") or _humanize(e["key"]),
                "group": "entity",
                "value": e.get("cardinality", ""),
                "desc": e.get("description", ""),
            }
        )
    links: List[Dict[str, Any]] = []
    for r in pack.get("relations", []):
        links.append(
            {
                "id": r["key"],
                "source": r["source_key"],
                "target": r["target_key"],
                "type": "RELATION",
                "label": (r.get("label_en") or r["key"])
                + (f" ({r.get('cardinality', '')})" if r.get("cardinality") else ""),
                "description": r.get("description", ""),
            }
        )
    return {
        "nodes": nodes,
        "links": links,
        "edge_types": {"RELATION": "Entity relationship"},
        "edge_descriptions": {
            r["key"]: r.get("description", "") for r in pack.get("relations", [])
        },
    }


# ===========================================================================
# Graph studio — user-driven graph building with full provenance
# ===========================================================================


def graph_studio_sources(pack: Dict[str, Any]) -> Dict[str, Any]:
    """List the building blocks a user can pick from in the graph studio."""
    return {
        "pack": {"id": pack["id"], "name": pack["name"]},
        "ontology": {
            "entities": len(pack.get("entities", [])),
            "attributes": len(pack.get("attributes", [])),
            "relations": len(pack.get("relations", [])),
        },
        "tables": [
            {
                "id": t["id"],
                "key": t["key"],
                "label": t.get("label") or t["key"],
                "row_count": len(t.get("rows", [])),
                "columns": [
                    {
                        "key": c["key"],
                        "label": c.get("label") or c["key"],
                        "data_type": c.get("data_type", "STRING"),
                        "derived": bool(c.get("expression")),
                    }
                    for c in t.get("columns", [])
                ],
            }
            for t in pack.get("tables", [])
        ],
    }


def _match_column_to_attribute(
    col_key: str, col_label: str, attributes: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Match a data column to an ontology attribute by key / label text."""
    ck = (col_key or "").strip().lower()
    cl = (col_label or "").strip().lower()
    for a in attributes:
        if ck and ck == a["key"].lower():
            return a
    for a in attributes:
        names = {
            (a.get("label_en") or "").lower(),
            (a.get("label_zh") or "").lower(),
            a["key"].lower().replace("_", " "),
        }
        if cl and cl in names:
            return a
        if ck and ck.replace("_", " ") in names:
            return a
    return None


def _entity_nodes(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ontology entity nodes with provenance (schema view)."""
    return [
        {
            "id": "ent:" + e["key"],
            "label": e.get("label_en") or _humanize(e["key"]),
            "group": "entity",
            "value": "",
            "desc": e.get("description", ""),
            "source": {
                "kind": "Ontology entity",
                "detail": "Entity '"
                + e["key"]
                + "' (layer "
                + str(e.get("layer", "?"))
                + ")",
            },
        }
        for e in entities
    ]


def build_studio_graph(
    pack: Dict[str, Any],
    view: str = "schema",
    table_ids: Optional[List[int]] = None,
    include_unmatched: bool = True,
) -> Dict[str, Any]:
    """Compose one of three compact analysis graphs, with provenance.

    Views:
      * ``schema``   — entities + relations only (the class structure).
      * ``lineage``  — entities + tables, edges show how each table feeds
                       an entity (explicit mapping or inferred from
                       column→attribute matches).
      * ``columns``  — entities + tables + one node per unique column
                       concept. Columns shared by several tables (e.g.
                       "Channel ID" in five tables) are aggregated into
                       ONE node whose provenance lists every source
                       table, so the graph never shows duplicates.
    """
    entities = pack.get("entities", [])
    attributes = pack.get("attributes", [])
    relations = pack.get("relations", [])
    ent_by_key = {e["key"]: e for e in entities}
    wanted = set(table_ids or [])
    tables = [t for t in pack.get("tables", []) if not wanted or t["id"] in wanted]

    nodes: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add_node(node: Dict[str, Any]) -> None:
        if node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)

    # -- Schema ----------------------------------------------------------
    if view == "schema":
        for n in _entity_nodes(entities):
            add_node(n)
        for r in relations:
            s, t = r["source_key"], r["target_key"]
            if s in ent_by_key and t in ent_by_key:
                links.append(
                    {
                        "id": "rel:" + r["key"],
                        "source": "ent:" + s,
                        "target": "ent:" + t,
                        "type": "RELATION",
                        "label": (r.get("label_en") or r["key"])
                        + (
                            f" ({r.get('cardinality', '')})"
                            if r.get("cardinality")
                            else ""
                        ),
                    }
                )
        groups = {"entity": "Ontology entity"}
        edge_types = {"RELATION": "Entity relationship"}

    # -- Data lineage ----------------------------------------------------
    elif view == "lineage":
        for n in _entity_nodes(entities):
            add_node(n)
        for t in tables:
            row_count = len(t.get("rows", []))
            tid = "tbl:" + t["key"]
            add_node(
                {
                    "id": tid,
                    "label": t.get("label") or t["key"],
                    "group": "instance",
                    "value": f"{row_count} rows",
                    "desc": t.get("description", ""),
                    "source": {
                        "kind": "Data table",
                        "detail": "Table '"
                        + t["key"]
                        + "' — "
                        + str(row_count)
                        + " rows in the active dataset",
                    },
                }
            )
            # Owner: explicit entity_key, otherwise inferred from the
            # entity whose attributes match the most columns.
            owner = t.get("entity_key") or ""
            inferred = False
            if owner not in ent_by_key:
                votes: Dict[str, int] = {}
                for c in t.get("columns", []):
                    attr = _match_column_to_attribute(
                        c["key"], c.get("label") or "", attributes
                    )
                    if attr and attr.get("entity_key") in ent_by_key:
                        votes[attr["entity_key"]] = votes.get(attr["entity_key"], 0) + 1
                if votes:
                    owner = max(votes, key=votes.get)
                    inferred = True
            if owner in ent_by_key:
                links.append(
                    {
                        "id": "feed:" + t["key"],
                        "source": tid,
                        "target": "ent:" + owner,
                        "type": "OPERATIONAL",
                        "label": "feeds (inferred)" if inferred else "feeds",
                    }
                )
        groups = {"entity": "Ontology entity", "instance": "Data table"}
        edge_types = {"OPERATIONAL": "Table feeds entity"}

    # -- Column mapping (aggregated, no duplicate column nodes) ----------
    else:
        for n in _entity_nodes(entities):
            add_node(n)
        # concept key -> aggregated info
        concepts: Dict[str, Dict[str, Any]] = {}
        for t in tables:
            tid = "tbl:" + t["key"]
            add_node(
                {
                    "id": tid,
                    "label": t.get("label") or t["key"],
                    "group": "instance",
                    "value": f"{len(t.get('rows', []))} rows",
                    "desc": t.get("description", ""),
                    "source": {
                        "kind": "Data table",
                        "detail": "Table '"
                        + t["key"]
                        + "' — "
                        + str(len(t.get("rows", [])))
                        + " rows in the active dataset",
                    },
                }
            )
            for c in t.get("columns", []):
                attr = _match_column_to_attribute(
                    c["key"], c.get("label") or "", attributes
                )
                if attr:
                    ckey = "attr:" + attr["key"]
                else:
                    if not include_unmatched:
                        continue
                    # Group by the normalised DISPLAY name, so raw-key
                    # variants and abbreviations (e.g. "prod" /
                    # "productivity", both labelled "Productivity") merge.
                    disp = (c.get("label") or _humanize(c["key"])).strip().lower()
                    ckey = "unmatched:" + disp
                info = concepts.setdefault(
                    ckey,
                    {
                        "label": (attr.get("label_en") if attr else None)
                        or c.get("label")
                        or _humanize(c["key"]),
                        "attr": attr,
                        "tables": [],
                        "raw_keys": set(),
                        "types": set(),
                        "derived": False,
                        "expr": "",
                    },
                )
                info["tables"].append((tid, t["key"]))
                info["raw_keys"].add(c["key"])
                info["types"].add(c.get("data_type", "STRING"))
                if c.get("expression"):
                    info["derived"] = True
                    info["expr"] = c["expression"]

        for ckey, info in concepts.items():
            attr = info["attr"]
            node_id = "cc:" + ckey
            tbl_keys = sorted({k for _, k in info["tables"]})
            n_tables = len(tbl_keys)
            types = ", ".join(sorted(info["types"]))
            if attr:
                detail = (
                    "Column concept '"
                    + ckey.split(":", 1)[1]
                    + "' found in "
                    + str(n_tables)
                    + " table(s): "
                    + ", ".join(tbl_keys)
                    + " · matches ontology attribute '"
                    + attr["key"]
                    + "'"
                )
            else:
                raw = ", ".join(sorted(info["raw_keys"]))
                detail = (
                    "Column '"
                    + raw
                    + "' found in "
                    + str(n_tables)
                    + " table(s): "
                    + ", ".join(tbl_keys)
                    + " · no ontology attribute match"
                )
            if info["derived"]:
                detail += " · derived: " + info["expr"]
            add_node(
                {
                    "id": node_id,
                    "label": info["label"],
                    "group": "assumption",
                    "value": f"{n_tables} table(s)",
                    "desc": detail,
                    "source": {"kind": "Data column", "detail": detail},
                }
            )
            # One has-column edge per (table, concept) — unique pairs only.
            pair_seen: Set[Tuple[str, str]] = set()
            for tid, _tkey in info["tables"]:
                pair = (tid, node_id)
                if pair in pair_seen:
                    continue
                pair_seen.add(pair)
                links.append(
                    {
                        "id": "has:" + tid + ":" + ckey,
                        "source": tid,
                        "target": node_id,
                        "type": "STRUCTURAL",
                        "label": types if n_tables == 1 else "has column",
                    }
                )
            if attr and attr.get("entity_key") in ent_by_key:
                links.append(
                    {
                        "id": "map:" + ckey,
                        "source": node_id,
                        "target": "ent:" + attr["entity_key"],
                        "type": "CROSS_LINK",
                        "label": "maps to " + attr["key"],
                    }
                )
        groups = {
            "entity": "Ontology entity",
            "instance": "Data table",
            "assumption": "Data column (aggregated)",
        }
        edge_types = {
            "STRUCTURAL": "Table has column",
            "CROSS_LINK": "Column maps to attribute",
        }

    return {
        "view": view,
        "nodes": nodes,
        "links": links,
        "groups": groups,
        "edge_types": edge_types,
        "sources_used": sorted(
            {n["source"]["kind"] + ": " + n["source"]["detail"] for n in nodes}
        ),
    }


def graph_from_upload(
    pack: Optional[Dict[str, Any]],
    filename: str,
    columns: List[str],
    row_count: int,
    sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an ad-hoc graph for an uploaded file, matched to the ontology.

    Nothing is written to the database — this is an in-memory preview that
    links the file's columns to whatever ontology attributes they match.
    """
    attributes = (pack or {}).get("attributes", [])
    entities = (pack or {}).get("entities", [])
    ent_by_key = {e["key"]: e for e in entities}
    nodes: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    file_id = "file:" + filename
    nodes.append(
        {
            "id": file_id,
            "label": filename,
            "group": "mix",
            "value": f"{row_count} rows",
            "desc": "Uploaded file (preview only — not stored).",
            "source": {
                "kind": "Uploaded file",
                "detail": filename + " — " + str(row_count) + " data rows",
            },
        }
    )
    matched_entities: Set[str] = set()
    for col in columns:
        col_id = "fcol:" + col
        attr = _match_column_to_attribute(col, col, attributes)
        sample_val = ""
        if sample and col in sample and sample[col] is not None:
            sample_val = str(sample[col])[:40]
        prov = "Column '" + col + "' of uploaded file '" + filename + "'"
        add_desc = prov + (
            " · matches ontology attribute '" + attr["key"] + "'"
            if attr
            else " · no ontology attribute match"
        )
        nodes.append(
            {
                "id": col_id,
                "label": col,
                "group": "assumption",
                "value": sample_val,
                "desc": add_desc,
                "source": {"kind": "Uploaded column", "detail": add_desc},
            }
        )
        links.append(
            {
                "id": "fhas:" + col,
                "source": file_id,
                "target": col_id,
                "type": "STRUCTURAL",
                "label": "has column",
            }
        )
        if attr and attr.get("entity_key") in ent_by_key:
            ek = attr["entity_key"]
            ent_node_id = "ent:" + ek
            if ek not in matched_entities:
                matched_entities.add(ek)
                e = ent_by_key[ek]
                nodes.append(
                    {
                        "id": ent_node_id,
                        "label": e.get("label_en") or _humanize(ek),
                        "group": "entity",
                        "value": "",
                        "desc": e.get("description", ""),
                        "source": {
                            "kind": "Ontology entity",
                            "detail": "Entity '" + ek + "' of the active pack",
                        },
                    }
                )
            links.append(
                {
                    "id": "fmap:" + col,
                    "source": col_id,
                    "target": ent_node_id,
                    "type": "CROSS_LINK",
                    "label": "maps to " + attr["key"],
                }
            )
    return {
        "nodes": nodes,
        "links": links,
        "groups": {
            "mix": "Uploaded file",
            "assumption": "File column",
            "entity": "Ontology entity",
        },
        "edge_types": {
            "STRUCTURAL": "File has column",
            "CROSS_LINK": "Column maps to attribute",
        },
        "sources_used": sorted(
            {n["source"]["kind"] + ": " + n["source"]["detail"] for n in nodes}
        ),
    }


# ===========================================================================
# Smart ontology generation from data tables
# ===========================================================================

_STOP_TOKENS = {
    "monthly",
    "master",
    "data",
    "dim",
    "fact",
    "tbl",
    "table",
    "list",
    "detail",
    "details",
    "record",
    "records",
    "sheet",
}


def _key_tokens(key: str) -> List[str]:
    """Lightweight tokenizer for table/column key matching (singularized,
    table-noise removed). Distinct from _tokens() which powers question
    matching and keeps CJK support."""
    toks = re.split(r"[^a-z0-9]+", (key or "").lower())
    out = []
    for t in toks:
        if not t or t in _STOP_TOKENS:
            continue
        if t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out or re.split(r"[^a-z0-9]+", (key or "").lower())


def _infer_data_type(values: List[Any]) -> str:
    n = nums = bools = dates = 0
    for v in values:
        if v is None or v == "":
            continue
        n += 1
        s = str(v).strip()
        if isinstance(v, bool) or s.lower() in ("true", "false", "yes", "no", "y", "n"):
            bools += 1
            continue
        try:
            float(s.replace(",", ""))
            nums += 1
            continue
        except ValueError:
            pass
        if re.match(r"^\d{4}-\d{2}-\d{2}([ T]|$)", s) or re.match(
            r"^\d{1,2}/\d{1,2}/\d{2,4}$", s
        ):
            dates += 1
    if n == 0:
        return "STRING"
    if nums / n >= 0.8:
        return "NUMBER"
    if bools / n >= 0.8:
        return "BOOL"
    if dates / n >= 0.8:
        return "DATE"
    return "STRING"


def infer_ontology(
    pack: Dict[str, Any],
    table_ids: Optional[List[int]] = None,
    name: str = "",
    slug: str = "",
) -> Dict[str, Any]:
    """Derive a full onti-pack/v1 payload from the pack's data tables.

    One entity per table, one attribute per column (data type inferred
    from the actual values), and ONE_TO_MANY relations detected from
    ``*_id`` columns that reference another table. The selected tables,
    columns and active dataset rows are copied into the payload, so
    importing it produces a working ontology that can answer questions
    immediately.
    """
    wanted = set(table_ids or [])
    tables = [t for t in pack.get("tables", []) if not wanted or t["id"] in wanted]
    if not tables:
        raise EvalError("Select at least one data table to build from.")

    entities: List[Dict[str, Any]] = []
    ent_tokens: Dict[str, set] = {}
    for i, t in enumerate(tables):
        ekey = t["key"]
        label = t.get("label") or _humanize(t["key"])
        entities.append(
            {
                "key": ekey,
                "label_en": label,
                "layer": 3,
                "description": "Auto-generated from data table '"
                + t["key"]
                + "' ("
                + str(len(t.get("rows", [])))
                + " rows).",
                "sort": i,
            }
        )
        ent_tokens[ekey] = set(_key_tokens(ekey))

    attributes: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    seen_rel: Set[Tuple[str, str]] = set()
    for t in tables:
        ekey = t["key"]
        rows = t.get("rows", [])
        sample = rows[:200]
        for ci, c in enumerate(t.get("columns", [])):
            ckey = c["key"]
            values = [r.get(ckey) for r in sample if isinstance(r, dict)]
            dtype = c.get("data_type") or _infer_data_type(values)
            label = c.get("label") or _humanize(ckey)
            attributes.append(
                {
                    "entity_key": ekey,
                    "key": ckey,
                    "label_en": label,
                    "data_type": dtype,
                    "unit": c.get("unit", ""),
                    "required": 0,
                    "is_unique": 1 if ckey.endswith("_id") else 0,
                    "description": "Column '" + ckey + "' of table '" + t["key"] + "'.",
                    "sort": ci,
                }
            )
            # FK-style reference: *_id column pointing at another table.
            if ckey.endswith("_id") or ckey in ("id",):
                if ckey == "id":
                    continue
                stem = set(_key_tokens(ckey[:-3]))
                if not stem:
                    continue
                best, best_score = None, 0.0
                for cand, toks in ent_tokens.items():
                    if cand == ekey or not toks:
                        continue
                    overlap = len(stem & toks)
                    if not overlap:
                        continue
                    score = overlap / len(stem)
                    # Tie-break: prefer the shorter key (channels vs
                    # channel_monthly) then the larger token overlap.
                    if score > best_score or (
                        score == best_score and best and len(cand) < len(best)
                    ):
                        best, best_score = cand, score
                if best and (best, ekey) not in seen_rel:
                    seen_rel.add((best, ekey))
                    rkey = best + "_has_" + ekey
                    relations.append(
                        {
                            "key": rkey[:60],
                            "label_en": _humanize(best) + " → " + _humanize(ekey),
                            "cardinality": "ONE_TO_MANY",
                            "source_key": best,
                            "target_key": ekey,
                            "source_attr": ckey,
                            "target_attr": "id",
                            "description": "Inferred from column '"
                            + ckey
                            + "' in table '"
                            + t["key"]
                            + "'.",
                            "sort": len(relations),
                        }
                    )

    # Visual graph derived from the same entities/relations so the
    # impact graph always corresponds to the schema.
    graph_nodes = [
        {
            "key": e["key"],
            "label": e["label_en"],
            "grp": _LAYER_TO_GRP.get(e["layer"], "operational"),
            "value": "",
            "description": e["description"],
            "aliases": "[]",
            "is_core": 0,
            "sort": e["sort"],
        }
        for e in entities
    ]
    graph_edges = [
        {
            "key": r["key"],
            "source_key": r["source_key"],
            "target_key": r["target_key"],
            "edge_type": _CARD_TO_EDGE.get(r["cardinality"], "STRUCTURAL"),
            "label": r["label_en"],
            "description": r["description"],
            "sort": r["sort"],
        }
        for r in relations
    ]

    tables_out: List[Dict[str, Any]] = []
    for t in tables:
        tables_out.append(
            {
                "key": t["key"],
                "label": t.get("label") or _humanize(t["key"]),
                "entity_key": t["key"],
                "description": t.get("description", ""),
                "columns": [
                    {
                        "key": c["key"],
                        "label": c.get("label") or _humanize(c["key"]),
                        "data_type": c.get("data_type", "STRING"),
                        "unit": c.get("unit", ""),
                        "enum_values": c.get("enum_values", ""),
                        "expression": c.get("expression", ""),
                        "fmt": c.get("fmt", "raw"),
                        "decimals": c.get("decimals", 2),
                        "required": 0,
                        "sort": i,
                    }
                    for i, c in enumerate(t.get("columns", []))
                ],
                "datasets": [
                    {
                        "name": t.get("dataset_name") or "Active dataset",
                        "filename": "",
                        "kind": "upload",
                        "rows": t.get("rows", []),
                    }
                ],
            }
        )

    return {
        "format": "onti-pack/v1",
        "slug": slug,
        "name": name or "Auto-generated ontology",
        "description": "Ontology auto-generated from "
        + str(len(tables))
        + " data table(s). Review entities, attributes and relations, "
        + "then ask questions immediately.",
        "entities": entities,
        "attributes": attributes,
        "relations": relations,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "metric_formulas": [],
        "question_rules": [],
        "tables": tables_out,
        "question_bank": [],
    }


def recommended_questions(pack: Dict[str, Any], limit: int = 8) -> List[str]:
    """Smart question suggestions derived from the active ontology.

    Built from entities, numeric attributes and metric formulas; any
    question already present in the canonical question bank is skipped.
    """
    existing = {q.strip().lower() for q in suggested_questions(pack.get("id"))}
    out: List[str] = []

    def add(q: str) -> None:
        if q.lower() not in existing and q not in out:
            out.append(q)

    for f in pack.get("formulas", []):
        label = f.get("label") or _humanize(f.get("key", ""))
        add("What is the " + label + "?")
        add("How is " + label + " calculated?")
    for a in pack.get("attributes", []):
        if a.get("data_type") in ("NUMBER", "INT", "FLOAT"):
            label = a.get("label_en") or _humanize(a.get("key", ""))
            add("What is the total " + label + "?")
    for e in pack.get("entities", []):
        label = (e.get("label_en") or _humanize(e.get("key", ""))).lower()
        add("Show me the " + label + " breakdown.")
        add("Which " + label + " has the highest value?")
    return out[:limit]


def retrieve_subgraph(
    pack: Dict[str, Any],
    question: str = "",
    answer_text: str = "",
    extra_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Alias-driven deterministic retrieval: matched nodes + 1-hop edges."""
    haystack = f" {question} {answer_text} {' '.join(extra_nodes or [])} ".lower()

    matched: List[str] = []
    for node in pack["nodes"]:
        key = node["key"]
        if key.lower() in haystack or any(
            a.lower() in haystack for a in node["aliases"]
        ):
            matched.append(key)

    if not matched:
        matched = [n["key"] for n in pack["nodes"] if n.get("is_core")]

    seed = set(matched)
    keep_nodes = set(matched)
    keep_edges: List[str] = []
    for e in pack["edges"]:
        endpoints = {s.strip() for s in e["source_key"].split(",")} | {e["target_key"]}
        if endpoints & seed:
            if e["key"] not in keep_edges:
                keep_edges.append(e["key"])
            keep_nodes |= endpoints

    node_order = [n["key"] for n in pack["nodes"]]
    return {
        "matched": matched,
        "nodes": [k for k in node_order if k in keep_nodes],
        "edge_ids": keep_edges,
    }


# ===========================================================================
# Graph intelligence — paths, impact analysis, cycle detection, health
# ===========================================================================


def _graph_adjacency(
    pack: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    """Out/in adjacency maps over the visual graph links (auto-generated)."""
    _auto_generate_graph(pack)
    out: Dict[str, List[Dict[str, Any]]] = {}
    inc: Dict[str, List[Dict[str, Any]]] = {}
    for n in pack["nodes"]:
        out.setdefault(n["key"], [])
        inc.setdefault(n["key"], [])
    for e in pack["edges"]:
        link = {
            "id": e["key"],
            "source": e["source_key"],
            "target": e["target_key"],
            "type": e["edge_type"],
            "label": e.get("label", ""),
            "description": e.get("description", ""),
        }
        for s in {x.strip() for x in str(link["source"]).split(",")}:
            if s in out:
                out[s].append(link)
        if link["target"] in inc:
            inc[link["target"]].append(link)
    return out, inc


def shortest_path(pack: Dict[str, Any], source: str, target: str) -> Dict[str, Any]:
    """BFS shortest path between two graph nodes (direction-aware, follows edges)."""
    out, _ = _graph_adjacency(pack)
    keys = {n["key"] for n in pack["nodes"]}
    if source not in keys or target not in keys:
        return {
            "found": False,
            "reason": "Unknown node key.",
            "path": [],
            "edge_ids": [],
        }
    if source == target:
        return {"found": True, "path": [source], "edge_ids": []}

    prev: Dict[str, Tuple[str, str]] = {}  # node -> (prev_node, via_edge_id)
    queue = deque([source])
    seen = {source}
    while queue:
        cur = queue.popleft()
        for link in out.get(cur, []):
            for t in {x.strip() for x in str(link["target"]).split(",")}:
                if t in seen or t not in keys:
                    continue
                seen.add(t)
                prev[t] = (cur, link["id"])
                if t == target:
                    queue.clear()
                    break
                queue.append(t)

    if target not in seen:
        return {
            "found": False,
            "reason": f"No directed path from '{source}' to '{target}'.",
            "path": [],
            "edge_ids": [],
        }
    path, edge_ids = [target], []
    cur = target
    while cur != source:
        p, eid = prev[cur]
        edge_ids.append(eid)
        cur = p
        path.append(cur)
    path.reverse()
    edge_ids.reverse()
    return {"found": True, "path": path, "edge_ids": edge_ids}


def impact_analysis(
    pack: Dict[str, Any], node: str, hops: int = 2, direction: str = "downstream"
) -> Dict[str, Any]:
    """N-hop reachability from/into *node*.

    ``downstream`` follows edge direction (what *node* influences);
    ``upstream`` walks against it (what influences *node*).
    """
    out, inc = _graph_adjacency(pack)
    keys = {n["key"] for n in pack["nodes"]}
    if node not in keys:
        return {
            "found": False,
            "reason": "Unknown node key.",
            "root": node,
            "levels": [],
            "nodes": [],
            "edge_ids": [],
        }

    fwd = direction != "upstream"
    adjacency = out if fwd else inc

    levels: List[List[str]] = [[node]]
    seen = {node}
    edge_ids: List[str] = []
    frontier = [node]
    for _ in range(max(1, min(int(hops or 2), 6))):
        nxt: List[str] = []
        for cur in frontier:
            for link in adjacency.get(cur, []):
                targets = (
                    {x.strip() for x in str(link["target"]).split(",")}
                    if fwd
                    else {x.strip() for x in str(link["source"]).split(",")}
                )
                for t in targets:
                    if t not in keys or t in seen:
                        continue
                    seen.add(t)
                    nxt.append(t)
                    edge_ids.append(link["id"])
        if not nxt:
            break
        levels.append(sorted(nxt))
        frontier = nxt

    order = [k for lvl in levels for k in lvl]
    node_meta = {n["key"]: n for n in pack["nodes"]}
    return {
        "found": True,
        "root": node,
        "direction": "downstream" if fwd else "upstream",
        "hops": len(levels) - 1,
        "levels": levels,
        "nodes": order,
        "edge_ids": list(dict.fromkeys(edge_ids)),
        "labels": {
            k: (node_meta.get(k, {}).get("label") or _humanize(k)) for k in order
        },
    }


def detect_cycles(pack: Dict[str, Any]) -> Dict[str, Any]:
    """Detect directed cycles in the visual graph (DFS with colouring)."""
    out, _ = _graph_adjacency(pack)
    color = {n["key"]: 0 for n in pack["nodes"]}  # 0 white 1 grey 2 black
    stack: List[str] = []
    cycles: List[List[str]] = []

    def dfs(u: str) -> None:
        color[u] = 1
        stack.append(u)
        for link in out.get(u, []):
            for v in {x.strip() for x in str(link["target"]).split(",")}:
                if v not in color:
                    continue
                if color[v] == 1:
                    i = stack.index(v)
                    cycles.append(stack[i:] + [v])
                elif color[v] == 0:
                    dfs(v)
        stack.pop()
        color[u] = 2

    for k in list(color):
        if color[k] == 0:
            dfs(k)
    return {"count": len(cycles), "cycles": cycles[:10]}


def ontology_health(pack: Dict[str, Any]) -> Dict[str, Any]:
    """Structural integrity report for the active pack.

    Returns a scored list of checks — ok / warn / error — covering schema
    completeness, graph connectivity, alias collisions and formula/rule
    quality.  Consumed by ``GET /api/admin/ontology/health``.
    """
    _auto_generate_graph(pack)
    checks: List[Dict[str, Any]] = []

    def add(cid: str, title: str, status: str, detail: str, items: List[str]) -> None:
        checks.append(
            {
                "id": cid,
                "title": title,
                "status": status,
                "detail": detail,
                "items": items[:20],
            }
        )

    entities = pack.get("entities", [])
    attributes = pack.get("attributes", [])
    relations = pack.get("relations", [])
    nodes = pack.get("nodes", [])
    edges = pack.get("edges", [])
    formulas = pack.get("formulas", [])
    rules = pack.get("rules", [])

    # 1. entities without attributes
    ents_with_attr = {a["entity_key"] for a in attributes}
    missing_attr = [e["key"] for e in entities if e["key"] not in ents_with_attr]
    add(
        "entity-attrs",
        "Entities have attributes",
        (
            "error"
            if len(missing_attr) == len(entities) and entities
            else ("warn" if missing_attr else "ok")
        ),
        (
            f"{len(entities) - len(missing_attr)}/{len(entities)} entities carry at least one attribute."
            if entities
            else "No entities defined yet."
        ),
        missing_attr,
    )

    # 2. orphan entities (never referenced by any relation)
    rel_keys = {r["source_key"] for r in relations} | {
        r["target_key"] for r in relations
    }
    orphans = [e["key"] for e in entities if e["key"] not in rel_keys]
    add(
        "entity-links",
        "Entities connected by relations",
        "warn" if orphans else "ok",
        f"{len(orphans)} of {len(entities)} entities are not used in any relation.",
        orphans,
    )

    # 3. alias collisions — plain-language aliases that map to several nodes.
    #    Namespaced tags ("channel:agency") are cross-node by design and are
    #    excluded: the retrieval engine intentionally matches every holder.
    alias_owner: Dict[str, str] = {}
    dupes: List[str] = []
    for n in nodes:
        names = [n["key"].lower().replace("_", " ")] + [
            a.lower() for a in n.get("aliases", [])
        ]
        for a in names:
            if ":" in a:
                continue
            if a in alias_owner and alias_owner[a] != n["key"]:
                dupes.append(f"'{a}' → {alias_owner[a]} & {n['key']}")
            else:
                alias_owner[a] = n["key"]
    add(
        "alias-collisions",
        "Aliases resolve uniquely",
        "warn" if dupes else "ok",
        f"{len(dupes)} plain-language alias(es) map to multiple nodes — "
        "evidence retrieval may over-select.",
        dupes,
    )

    # 4. graph connectivity — isolated nodes + components
    out, inc = _graph_adjacency(pack)
    isolated = [
        n["key"] for n in nodes if not out.get(n["key"]) and not inc.get(n["key"])
    ]
    add(
        "graph-isolated",
        "Graph has no isolated nodes",
        "warn" if isolated else "ok",
        f"{len(isolated)} node(s) have no edges and will float disconnected in the graph.",
        isolated,
    )

    # connected components (undirected)
    seen: Set[str] = set()
    components = 0
    for n in nodes:
        if n["key"] in seen:
            continue
        components += 1
        queue = deque([n["key"]])
        seen.add(n["key"])
        while queue:
            cur = queue.popleft()
            for nb in {l["target"] for l in out.get(cur, [])} | {
                l["source"] for l in inc.get(cur, [])
            }:
                for x in {y.strip() for y in str(nb).split(",")}:
                    if x in out and x not in seen:
                        seen.add(x)
                        queue.append(x)
    add(
        "graph-components",
        "Graph is a single connected web",
        "warn" if components > 1 and len(nodes) > 1 else "ok",
        f"{components} connected component(s) across {len(nodes)} nodes.",
        [],
    )

    # 5. formula quality — expression sanity via sandbox compile check
    bad_formulas: List[str] = []
    for f in formulas:
        expr = f.get("expression", "")
        if not str(expr).strip():
            bad_formulas.append(f"{f['key']}: empty expression")
            continue
        try:
            ast.parse(str(expr), mode="eval")
        except (SyntaxError, ValueError) as e:
            bad_formulas.append(f"{f['key']}: {e}")
    add(
        "formulas-valid",
        "Formulas compile",
        "error" if bad_formulas else "ok",
        f"{len(formulas) - len(bad_formulas)}/{len(formulas)} formulas compile in the sandbox.",
        bad_formulas,
    )

    # 6. rules quality — empty patterns or templates
    bad_rules: List[str] = []
    for r in rules:
        if not r.get("patterns"):
            bad_rules.append(f"{r['key']}: no patterns — will never trigger")
        elif not str(r.get("template", "")).strip():
            bad_rules.append(f"{r['key']}: empty answer template")
    add(
        "rules-valid",
        "Question rules are answerable",
        "warn" if bad_rules else "ok",
        f"{len(rules) - len(bad_rules)}/{len(rules)} rules have patterns and a template.",
        bad_rules,
    )

    # 7. relations have labels
    unlabelled = [r["key"] for r in relations if not (r.get("label_en") or "").strip()]
    add(
        "relations-labelled",
        "Relations carry labels",
        "warn" if unlabelled else "ok",
        f"{len(unlabelled)} relation(s) fall back to their key as label.",
        unlabelled,
    )

    # 8. numeric attributes missing units
    no_unit = [
        f"{a['entity_key']}.{a['key']}"
        for a in attributes
        if a.get("data_type") in ("number", "currency", "decimal", "float", "integer")
        and not (a.get("unit") or "").strip()
    ]
    add(
        "attr-units",
        "Numeric attributes declare units",
        "warn" if no_unit else "ok",
        f"{len(no_unit)} numeric attribute(s) without a unit.",
        no_unit,
    )

    errors = sum(1 for c in checks if c["status"] == "error")
    warns = sum(1 for c in checks if c["status"] == "warn")
    score = "red" if errors else ("amber" if warns else "green")

    stats = {
        "entities": len(entities),
        "attributes": len(attributes),
        "relations": len(relations),
        "nodes": len(nodes),
        "edges": len(edges),
        "formulas": len(formulas),
        "rules": len(rules),
        "components": components if nodes else 0,
    }
    return {
        "score": score,
        "errors": errors,
        "warnings": warns,
        "checks": checks,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Formula impact analysis — what depends on a given formula?
# ---------------------------------------------------------------------------


def _expr_names(expr: str) -> Set[str]:
    """Extract referenced identifiers (names) from a formula expression."""
    names: Set[str] = set()
    try:
        tree = ast.parse(str(expr or ""), mode="eval")
    except (SyntaxError, ValueError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


def formula_impact(pack: Dict[str, Any], formula_key: str) -> Dict[str, Any]:
    """What breaks if I change formula ``formula_key``?

    Returns downstream formulas, graph nodes and question rules that reference
    the key — plus a transitive closure of formulas (since formula B may call
    formula A which calls the target).  Use this in the admin formula editor
    to warn before saving.
    """
    formulas = pack.get("formulas", [])
    fkeys = {f["key"] for f in formulas}
    if formula_key not in fkeys:
        return {
            "found": False,
            "reason": f"Unknown formula '{formula_key}'.",
            "downstream_formulas": [],
            "graph_nodes": [],
            "rules": [],
        }

    # direct dependencies: formula_key -> set of formula keys it calls
    calls: Dict[str, Set[str]] = {
        f["key"]: _expr_names(f.get("expression", "")) & fkeys for f in formulas
    }

    # reverse: who calls formula_key (directly), then transitive closure
    direct_callers = [f["key"] for f in formulas if formula_key in calls[f["key"]]]
    downstream: Set[str] = set()
    queue = list(direct_callers)
    while queue:
        cur = queue.pop()
        if cur in downstream:
            continue
        downstream.add(cur)
        for f in formulas:
            if cur in calls[f["key"]] and f["key"] not in downstream:
                queue.append(f["key"])

    # graph nodes referencing the formula (or any downstream formula) in value
    affected_formulas = {formula_key} | downstream
    nodes: List[Dict[str, str]] = []
    for n in pack.get("nodes", []):
        val = str(n.get("value", ""))
        for fk in affected_formulas:
            if fk in val:
                nodes.append({"key": n["key"], "label": n.get("label", n["key"])})
                break

    # question rules whose template mentions any affected formula
    rules: List[Dict[str, str]] = []
    for r in pack.get("rules", []):
        tmpl = str(r.get("template", ""))
        for fk in affected_formulas:
            if fk in tmpl:
                rules.append({"key": r["key"], "category": r.get("category", "")})
                break

    return {
        "found": True,
        "formula": formula_key,
        "downstream_formulas": sorted(downstream),
        "direct_callers": sorted(direct_callers),
        "graph_nodes": nodes,
        "rules": rules,
        "total_affected": len(downstream) + len(nodes) + len(rules),
    }


def render_narrative(
    pack: Dict[str, Any], computed: Optional[Dict[str, Any]] = None
) -> str:
    """Render the pack narrative template; auto-generate a basic one if empty."""
    if computed is None:
        computed = compute_pack(pack, strict=False)
    graph = visual_graph(pack)
    ctx = template_context(pack, computed)
    ctx["graph"] = graph
    if pack.get("narrative", "").strip():
        try:
            return _make_env().from_string(pack["narrative"]).render(**ctx).strip()
        except (TemplateError, EvalError, KeyError, ValueError) as e:
            return f"## Ontology narrative\n\n_Narrative template error: {e}_"
    # Generic fallback for packs without a curated narrative.
    if not pack["nodes"]:
        return "## Ontology narrative\n\nThis pack does not define a graph yet."
    lines = [
        f"## {pack['name']} — relationship model",
        "",
        f"The graph contains **{len(graph['nodes'])} nodes** and "
        f"**{len(graph['links'])} directed links** across "
        f"{len(graph['edge_types'])} edge type(s).",
        "",
    ]
    for e in pack["edges"]:
        lines.append(
            f"- **{e['key']}** ({e['edge_type']}) "
            f"{e['source_key']} → {e['target_key']}: {e['description']}"
        )
    return "\n".join(lines)


# ===========================================================================
# Formal schema + dynamic LLM grounding
# ===========================================================================


def formal_schema(pack: Dict[str, Any]) -> Dict[str, Any]:
    attrs_by_entity: Dict[str, List[Dict[str, Any]]] = {}
    for a in pack["attributes"]:
        attrs_by_entity.setdefault(a["entity_key"], []).append(a)
    entities = []
    for ent in pack["entities"]:
        d = dict(ent)
        d["attributes"] = attrs_by_entity.get(ent["key"], [])
        entities.append(d)
    return {
        "pack": {
            "id": pack["id"],
            "name": pack["name"],
            "slug": pack["slug"],
            "description": pack["description"],
        },
        "entities": entities,
        "relations": pack["relations"],
        "stats": {
            "entities": len(entities),
            "attributes": len(pack["attributes"]),
            "relations": len(pack["relations"]),
        },
    }


def _metric_display(key: str, value: Any, formulas: List[Dict[str, Any]]) -> str:
    if value is None:
        return "n/a (input data missing)"
    f = next((x for x in formulas if x["key"] == key), None)
    if f is None:
        return str(value)
    if f["fmt"] == "percent":
        return _fmt_pct(value, 1)
    if f["fmt"] == "currency_m":
        return _fmt_money(value, 2)
    if isinstance(value, float):
        return f"{value:.{f.get('decimals', 2)}f}"
    return str(value)


def _cell_display(value: Any, col: Dict[str, Any]) -> str:
    if value is None:
        return ""
    if col.get("fmt") == "percent" and isinstance(value, (int, float)):
        return _fmt_pct(value, 1)
    if col.get("fmt") == "currency_m" and isinstance(value, (int, float)):
        return _fmt_money(value, 2)
    if col.get("fmt") == "currency_k" and isinstance(value, (int, float)):
        return f"${value:,.0f}K"
    return str(value)


# Hints that a column carries a month / period value. Month coverage is the
# single most-asked grounding question ("what months do we have?"), so these
# columns get special treatment: full distinct list + per-month aggregates.
_MONTH_HINTS = ("month", "period", "date")

# Columns whose distinct values are listed in full in the LLM context. Beyond
# this cap only the count is given — the profile must stay compact even when
# a table holds tens of thousands of rows.
_DISTINCT_LIST_CAP = 48

# A month-like column with at most this many distinct values also gets a
# per-month aggregate table (sum / avg of every numeric column per month).
_MONTH_GROUP_CAP = 36

# Numeric columns aggregated per month, at most (keeps the block narrow).
_MONTH_MEASURE_CAP = 8

# Sample rows rendered verbatim per table — format illustration only; all
# figures the model needs come from the profiles and aggregates above.
_SAMPLE_ROWS = 3


def _is_month_like(col: Dict[str, Any]) -> bool:
    k = f"{col.get('key', '')} {col.get('label', '')}".lower()
    return any(h in k for h in _MONTH_HINTS)


def _profile_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt_profile_num(f: float) -> str:
    if abs(f - round(f)) < 1e-9 and abs(f) < 1e15:
        return f"{int(round(f)):,}"
    return f"{f:,.6g}"


def _sorted_distinct(rows: List[Dict[str, Any]], key: str) -> List[str]:
    """Distinct non-empty values, numerically sorted when they all parse."""
    seen: Dict[str, None] = {}
    for r in rows:
        v = r.get(key)
        if v is None or str(v).strip() == "":
            continue
        seen.setdefault(str(v).strip(), None)
    vals = list(seen)
    try:
        return sorted(vals, key=float)
    except ValueError:
        return sorted(vals)


def _month_key(v: Any) -> str:
    """Normalise a period value for grouping/display: `2023-01-01 00:00:00`,
    `2023-01-01` and `2023-01` all collapse to `2023-01`; anything else is
    passed through unchanged."""
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-\d{1,2}", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def _month_buckets(
    rows: List[Dict[str, Any]], key: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Group rows by normalised month key in one pass."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        v = r.get(key)
        if v is None or str(v).strip() == "":
            continue
        buckets.setdefault(_month_key(v), []).append(r)
    return buckets


def _column_profile_line(
    col: Dict[str, Any], rows: List[Dict[str, Any]]
) -> Tuple[str, bool]:
    """One profile line for a column; second value = is this the month column."""
    key = col["key"]
    dtype = (col.get("data_type") or "STRING").upper()
    nums = [x for x in (_profile_number(r.get(key)) for r in rows) if x is not None]
    is_num_col = dtype in ("INTEGER", "FLOAT") or (
        nums and len(nums) >= len(rows) * 0.9
    )

    if is_num_col and nums:
        total, avg = sum(nums), sum(nums) / len(nums)
        if col.get("fmt") == "percent":
            parts = [
                f"avg {_fmt_pct(avg, 2)}",
                f"min {_fmt_pct(min(nums), 2)}",
                f"max {_fmt_pct(max(nums), 2)}",
            ]
        else:
            parts = [
                f"sum {_fmt_profile_num(total)}",
                f"avg {_fmt_profile_num(avg)}",
                f"min {_fmt_profile_num(min(nums))}",
                f"max {_fmt_profile_num(max(nums))}",
            ]
        return f"- {col['label']} (`{key}`, {dtype}): " + " · ".join(parts), False

    vals = _sorted_distinct(rows, key)
    if not vals:
        return f"- {col['label']} (`{key}`, {dtype}): no values", False
    if _is_month_like(col):
        # '2023-01-01 00:00:00' style values read badly and waste tokens —
        # collapse them to YYYY-MM before listing.
        vals = sorted({_month_key(v) for v in vals})
        if len(vals) <= _DISTINCT_LIST_CAP:
            return (
                f"- {col['label']} (`{key}`, {dtype}): {len(vals)} distinct values — "
                + ", ".join(vals),
                len(vals) <= _MONTH_GROUP_CAP,
            )
        return (
            f"- {col['label']} (`{key}`, {dtype}): {len(vals)} distinct values",
            False,
        )
    if len(vals) <= _DISTINCT_LIST_CAP:
        return (
            f"- {col['label']} (`{key}`, {dtype}): {len(vals)} distinct values — "
            + ", ".join(vals),
            False,
        )
    return f"- {col['label']} (`{key}`, {dtype}): {len(vals)} distinct values", False


def _table_context_block(
    tbl: Dict[str, Any], rows: List[Dict[str, Any]], sample_rows: int
) -> List[str]:
    """Profile-first description of one table: complete coverage, tiny size.

    Raw rows are NOT dumped wholesale — the model gets (a) a per-column
    profile with full distinct lists for low-cardinality fields, (b) per-month
    aggregates when a month column exists, and (c) a couple of sample rows.
    This answers 'what months/segments exist' over the FULL dataset (the old
    row-dump truncated at N rows and misled the model) while keeping the
    prompt an order of magnitude smaller, so AI answers are faster.
    """
    cols = tbl["columns"]
    lines = [f"## {tbl['label']} (table `{tbl['key']}`, {len(rows)} rows)"]

    lines.append("Column profile (computed over ALL rows):")
    month_col: Optional[Dict[str, Any]] = None
    for c in cols:
        line, is_month = _column_profile_line(c, rows)
        lines.append(line)
        if is_month and month_col is None:
            month_col = c

    if month_col is not None:
        buckets = _month_buckets(rows, month_col["key"])
        months = sorted(buckets)
        measures = []
        for c in cols:
            if c["key"] == month_col["key"]:
                continue
            dtype = (c.get("data_type") or "").upper()
            nums = [
                x
                for x in (_profile_number(r.get(c["key"])) for r in rows)
                if x is not None
            ]
            if nums and (dtype in ("INTEGER", "FLOAT") or len(nums) >= len(rows) * 0.9):
                measures.append((c, "sum" if dtype == "INTEGER" else "avg"))
            if len(measures) >= _MONTH_MEASURE_CAP:
                break
        if measures:
            head = (
                "| "
                + month_col["label"]
                + " | "
                + " | ".join(f"{c['label']} ({agg})" for c, agg in measures)
                + " |"
            )
            lines += [
                f"Aggregates by {month_col['label']} "
                f"({len(months)} groups, computed over all {len(rows)} rows):",
                head,
                "|" + "---|" * (len(measures) + 1),
            ]
            for m in months:
                cells = []
                for c, agg in measures:
                    nums = [
                        x
                        for x in (_profile_number(r.get(c["key"])) for r in buckets[m])
                        if x is not None
                    ]
                    if not nums:
                        cells.append("—")
                        continue
                    v = sum(nums) if agg == "sum" else sum(nums) / len(nums)
                    if c.get("fmt") == "percent":
                        cells.append(_fmt_pct(v, 2))
                    else:
                        cells.append(_fmt_profile_num(v))
                lines.append("| " + m + " | " + " | ".join(cells) + " |")

    sample = rows[:sample_rows]
    if sample:
        lines += [
            f"Sample rows (first {len(sample)} of {len(rows)}; figures above are "
            "the authoritative aggregates):",
            "| " + " | ".join(c["label"] for c in cols) + " |",
            "|" + "---|" * len(cols),
        ]
        for row in sample:
            lines.append(
                "| "
                + " | ".join(
                    _cell_display(row.get(c["key"]), c).replace("\n", " ") for c in cols
                )
                + " |"
            )
    lines.append("")
    return lines


def build_llm_context(pack: Dict[str, Any], sample_rows: int = _SAMPLE_ROWS) -> str:
    """Render a deterministic grounding block from the active pack's data.

    Every table is summarised over its FULL active dataset (column profiles,
    complete distinct month lists, per-month aggregates) instead of dumping a
    truncated row sample, so answers cover all months/segments and the prompt
    stays small enough for fast model responses.
    """
    computed = compute_pack(pack, strict=False)
    lines: List[str] = [f"# {pack['name']} — knowledge base", ""]
    if pack.get("description"):
        lines += [pack["description"], ""]

    if pack["entities"]:
        lines.append("## Entities and attributes")
        for ent in pack["entities"]:
            lines.append(f"### {ent['label_en']} (`{ent['key']}`)")
            if ent.get("description"):
                lines.append(ent["description"])
            for a in [x for x in pack["attributes"] if x["entity_key"] == ent["key"]]:
                unit = f" {a['unit']}" if a["unit"] else ""
                enum = f"; allowed: {a['enum_values']}" if a["enum_values"] else ""
                lines.append(
                    f"- {a['label_en']} (`{a['key']}`, {a['data_type']}{unit}){enum}"
                    + (f": {a['description']}" if a.get("description") else "")
                )
            lines.append("")

    if pack["relations"]:
        lines.append("## Relations")
        for r in pack["relations"]:
            lines.append(
                f"- `{r['key']}` {r['label_en']} ({r['cardinality']}): "
                f"{r['source_key']} → {r['target_key']}"
                + (f" — {r['description']}" if r.get("description") else "")
            )
        lines.append("")

    for tbl in pack["tables"]:
        rows = computed["tables"].get(tbl["key"], [])
        if not rows:
            lines.append(f"## {tbl['label']} (table `{tbl['key']}`, no active data)")
            lines.append("")
            continue
        lines.extend(_table_context_block(tbl, rows, sample_rows))

    if pack["formulas"]:
        lines.append("## Computed metrics")
        for f in pack["formulas"]:
            lines.append(
                f"- {f['label']} (`{f['key']}`) = `{f['expression']}` = "
                f"{_metric_display(f['key'], computed['metrics'].get(f['key']), pack['formulas'])}"
            )
        lines.append("")

    if pack["edges"]:
        lines.append(f"## Ontology graph — {len(pack['edges'])} edge definitions")
        for e in pack["edges"]:
            lines.append(
                f"- [{e['key']}] {e['edge_type']}: {e['source_key']} -> "
                f"{e['target_key']} ({e['label']}): {e['description']}"
            )
        lines.append("")

    return "\n".join(lines).strip()


def build_table_profiles(pack: Dict[str, Any], sample_rows: int = 3) -> str:
    """Data-only grounding block — no ontology layer.

    Same column/date-profile logic as ``build_llm_context`` but deliberately
    excludes entities, relations, formulas and the ontology graph. Used by
    the "without ontology" side of the Ask comparison so the model can still
    reason over the uploaded data without seeing any of the pack's explicit
    structure or metric definitions.
    """
    computed = compute_pack(pack, strict=False)
    lines: List[str] = [
        f"# {pack['name']} — **uploaded data only (no ontology)**",
        "",
        "The following tables contain data this company has uploaded. Use them",
        "as-is; do NOT infer entities, relationships, metrics or business rules",
        "from column names — treat every field name literally.",
        "",
    ]
    tables_with_data = 0
    for tbl in pack["tables"]:
        rows = computed["tables"].get(tbl["key"], [])
        if not rows:
            continue
        tables_with_data += 1
        lines.extend(_table_context_block(tbl, rows, sample_rows))

    if tables_with_data == 0:
        lines.append("No active data tables have rows yet.")

    return "\n".join(lines).strip()


def formula_namespace_names(
    pack: Dict[str, Any], include_metrics: bool = True, table: Optional[str] = None
) -> Set[str]:
    """All names a formula/derived-column expression is allowed to reference."""
    names = {
        "rows",
        "where",
        "lookup",
        "cell",
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "wavg",
        "round",
        "abs",
        "money_k",
    }
    if include_metrics:
        names.update(f["key"] for f in pack["formulas"])
    if table is not None:
        for tbl in pack["tables"]:
            for c in tbl["columns"]:
                if tbl["key"] == table or c.get("expression"):
                    names.add(c["key"])
    return names
