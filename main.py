"""Onti — generic insurance ontology agent platform. FastAPI entry point.

Two portals share one API surface:

  * User portal  (/app)  — ontology Q&A (rules engine + any configured
    OpenAI-compatible LLM), impact-chain provenance, analytics reports.
  * Admin portal (/admin) — ontology packs, data & question imports, question
    bank, knowledge graph, model centre, reports, users and audit trail.

Authentication is cookie/session based (see modules.auth). Nothing about the
business ontology or the language model is hard-coded: the active ontology
pack and the default model configuration are both read from the database.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from common.logger import AppLogger
from modules import admin_service as adm
from modules import analytics
from modules import auth
from modules import config as cfg
from modules import importer as imp
from modules import induction
from modules import llm_client as llm
from modules import ontology_service as svc
from modules import qa_service as qa
from modules import report_builder as rb
from modules import db as db_mod
from modules.db import audit, get_conn, get_setting, set_setting

BASE_DIR = cfg.BASE_DIR
logger = AppLogger("onti")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent: on a fresh checkout this creates the schema and seeds the
    # built-in sample pack so the first-run admin wizard works out of the box.
    db_mod.init_db()
    yield


app = FastAPI(title=cfg.SYSTEM_NAME, version=cfg.VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------------------
# Error normalization — service-layer exceptions become clean JSON responses.
# ---------------------------------------------------------------------------


def _err(exc: Exception, status: int) -> JSONResponse:
    detail = getattr(exc, "message", None) or getattr(exc, "args", [str(exc)])[0]
    return JSONResponse(status_code=status, content={"error": detail})


@app.exception_handler(auth.AuthError)
async def _auth_error(request: Request, exc: auth.AuthError):
    return _err(exc, exc.status_code)


@app.exception_handler(adm.AdminValueError)
async def _admin_value_error(request: Request, exc: adm.AdminValueError):
    body: Dict[str, Any] = {"error": exc.args[0] if exc.args else "Invalid request."}
    if getattr(exc, "conflicts", None):
        body["conflicts"] = exc.conflicts
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(adm.AdminConflict)
async def _admin_conflict(request: Request, exc: adm.AdminConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.args[0] if exc.args else "Conflict.",
            "conflicts": getattr(exc, "conflicts", []),
        },
    )


@app.exception_handler(llm.ModelConfigError)
async def _model_error(request: Request, exc: llm.ModelConfigError):
    return _err(exc, exc.status_code)


@app.exception_handler(imp.UploadError)
async def _upload_error(request: Request, exc: imp.UploadError):
    return _err(exc, exc.status_code)


@app.exception_handler(rb.ReportError)
async def _report_error(request: Request, exc: rb.ReportError):
    return _err(exc, exc.status_code)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = ""

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        stripped = v.strip()
        if not re.match(r"^[A-Za-z0-9_.\-]{3,64}$", stripped):
            raise ValueError(
                "Username must be 3-64 characters: letters, digits, '_', '.', '-'."
            )
        return stripped

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < 8 or len(v) > 256:
            raise ValueError("Password must be 8-256 characters.")
        return v


class AskIn(BaseModel):
    question: str
    mode: str = "auto"  # auto | rules | ai
    history: Optional[List[Dict[str, str]]] = None


class ReportIn(BaseModel):
    title: str = ""
    scope: str = "session"
    sections: Optional[List[str]] = None
    include_ai: bool = False
    qa_items: List[Dict[str, Any]] = []
    use_ontology: bool = True


class PackIn(BaseModel):
    slug: str
    name: str
    description: str = ""


class DuplicateIn(BaseModel):
    slug: str
    name: str


class ElementIn(BaseModel):
    pack_id: int
    payload: Dict[str, Any]
    expected_version: Optional[int] = None


class AnalyzeIn(BaseModel):
    pack_id: int


class ImportPackIn(BaseModel):
    payload: Dict[str, Any]
    slug: Optional[str] = None
    activate: bool = False


class QuestionIn(BaseModel):
    pack_id: int
    question: str
    category: str = ""
    source: str = "user"


class ModelIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: float = 90
    verify_ssl: bool = True
    enabled: bool = True


class ModelTestIn(BaseModel):
    config_id: Optional[int] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    verify_ssl: Optional[bool] = None


class UserIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"


class IdIn(BaseModel):
    value: Any


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return RedirectResponse("/app")


@app.get("/login")
async def login_page(request: Request):
    user = auth.optional_user(request)
    if user:
        return RedirectResponse("/app")
    return templates.TemplateResponse(
        request, "login.html", {"system_name": cfg.SYSTEM_NAME, "version": cfg.VERSION}
    )


@app.get("/app")
async def app_page(request: Request):
    user = auth.optional_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "system_name": cfg.SYSTEM_NAME,
            "version": cfg.VERSION,
            "user": user,
            "is_admin": user.get("role") == "admin",
        },
    )


@app.get("/admin/login")
async def admin_login_page(request: Request):
    """Dedicated sign-in page for the administrator console (separate entry)."""
    user = auth.optional_user(request)
    if user and user.get("role") == "admin":
        return RedirectResponse("/admin")
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"system_name": cfg.SYSTEM_NAME, "version": cfg.VERSION},
    )


@app.get("/admin")
async def admin_page(request: Request):
    user = auth.optional_user(request)
    if not user:
        return RedirectResponse("/admin/login")
    if user.get("role") != "admin":
        return RedirectResponse("/app?forbidden=1")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"system_name": cfg.SYSTEM_NAME, "version": cfg.VERSION, "user": user},
    )


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------


@app.get("/api/auth/status")
async def auth_status():
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        return {
            "first_run": count == 0,
            "allow_registration": get_setting(conn, "allow_registration", "1") == "1",
        }


@app.post("/api/auth/register")
async def register(data: LoginIn):
    with get_conn() as conn:
        user = auth.register_account(
            conn, data.username.strip(), data.password, data.display_name.strip(), ""
        )
        result = auth.authenticate(conn, data.username.strip(), data.password)
        resp = JSONResponse({"user": user})
        resp.set_cookie(
            auth.COOKIE_NAME,
            result["token"],
            httponly=True,
            samesite="lax",
            max_age=cfg.SESSION_DAYS * 86400,
        )
        return resp


@app.post("/api/auth/login")
async def login(request: Request, data: LoginIn):
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    with get_conn() as conn:
        result = auth.authenticate(
            conn, data.username.strip(), data.password, ip=ip, user_agent=ua
        )
    resp = JSONResponse({"user": result["user"]})
    resp.set_cookie(
        auth.COOKIE_NAME,
        result["token"],
        httponly=True,
        samesite="lax",
        max_age=cfg.SESSION_DAYS * 86400,
    )
    return resp


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    with get_conn() as conn:
        auth.logout(conn, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
async def me(user: Dict[str, Any] = Depends(auth.current_user)):
    return {
        "user": {
            k: user[k]
            for k in ("id", "username", "display_name", "role", "enabled", "created_at")
            if k in user
        }
    }


# ---------------------------------------------------------------------------
# User portal API — read
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "service": cfg.SYSTEM_NAME, "version": cfg.VERSION}


def user_pack_id(conn, user: Dict[str, Any]) -> Optional[int]:
    """Resolve the pack this user is working in: their personal selection
    (persisted via the portal scenario dropdown) wins; otherwise the
    globally active pack is used."""
    raw = get_setting(conn, f"user_pack:{user['id']}", "")
    if raw:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            pid = None
        if (
            pid is not None
            and conn.execute(
                "SELECT 1 FROM ontology_packs WHERE id=?", (pid,)
            ).fetchone()
        ):
            return pid
    return adm.active_pack_id(conn)


def resolve_pack(conn, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convenience: the user's effective pack (None when nothing exists)."""
    pid = user_pack_id(conn, user)
    return svc.load_pack(conn, pid) if pid is not None else None


def pack_data_sources(conn, pid: Optional[int]) -> List[Dict[str, Any]]:
    """Dataset provenance records for the pack (what data it was built on)."""
    if pid is None:
        return []
    return [
        dict(r)
        for r in conn.execute(
            "SELECT name, filename, kind, row_count, created_at "
            "FROM datasets WHERE pack_id=? ORDER BY id",
            (pid,),
        ).fetchall()
    ]


def _auto_pack_report(
    conn,
    user: Dict[str, Any],
    pack: Dict[str, Any],
    title: str,
) -> Dict[str, Any]:
    """Build the default analytics report for a pack: recommended questions
    are answered by the rules engine, then rendered into MD/HTML/PDF. The
    title states the scenario (pack) and the data it is based on."""
    computed = svc.compute_pack(pack, strict=False)
    questions = svc.recommended_questions(pack, 8)
    if len(questions) < 6:
        for r in conn.execute(
            "SELECT question FROM question_bank WHERE pack_id=? ORDER BY id",
            (pack["id"],),
        ).fetchall():
            if r["question"] not in questions:
                questions.append(r["question"])
            if len(questions) >= 8:
                break
    qa_items = []
    for q in questions:
        try:
            res = svc.answer_question(q, pack, computed, raise_on_template_error=False)
        except Exception:
            res = None
        if res:
            qa_items.append(
                {
                    "question": q,
                    "answer": res["answer"],
                    "source": "rules",
                    "matched": True,
                    "latency_ms": 0,
                    "graph": res.get("nodes", []),
                }
            )
        else:
            qa_items.append(
                {
                    "question": q,
                    "answer": "_No rule matched — ask this in AI mode for a "
                    "model answer grounded in the same knowledge base._",
                    "source": "rules",
                    "matched": False,
                    "latency_ms": 0,
                }
            )
    sources = pack_data_sources(conn, pack["id"])
    data_note = (
        ", ".join(s["filename"] or s["name"] for s in sources[:4]) or "seed data"
    )
    if len(sources) > 4:
        data_note += f" (+{len(sources) - 4} more)"
    return rb.build_report(
        conn,
        owner_id=user["id"],
        title=f"{title} | Scenario: {pack['name']} | Data: {data_note}",
        scope="pack",
        sections=[
            "summary",
            "qa",
            "evidence",
            "kpi",
            "charts",
            "ontology",
            "analytics",
            "appendix",
        ],
        include_ai=False,
        qa_items=qa_items,
        pack_id=pack["id"],
        actor=user["username"],
    )


def _ensure_auto_report(
    conn,
    user: Dict[str, Any],
    pack: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Guarantee a full analytics report exists for this user + pack: the
    first dashboard visit generates one automatically (rules engine, no AI
    cost); later visits reuse it. Returns {uid, title} or None."""
    flag = f"auto_report:{user['id']}:{pack['id']}"
    uid = get_setting(conn, flag)
    if not uid:
        try:
            rep = _auto_pack_report(conn, user, pack, "Full Analytics Report")
            uid = rep.get("uid")
            if uid:
                set_setting(conn, flag, uid)
        except Exception:
            return None
    if not uid:
        return None
    row = conn.execute(
        "SELECT title, created_at FROM reports WHERE uid=?", (uid,)
    ).fetchone()
    if row is None:
        return None
    return {
        "uid": uid,
        "title": row["title"],
        "created_at": row["created_at"],
    }


@app.get("/api/packs")
async def list_packs(user: Dict[str, Any] = Depends(auth.current_user)):
    """Scenario picker data: every ontology pack with its provenance, so the
    user can switch the working scenario from the portal header."""
    with get_conn() as conn:
        chosen = user_pack_id(conn, user)
        active = adm.active_pack_id(conn)
        out = []
        for r in conn.execute(
            "SELECT p.id, p.name, p.description, p.origin, p.created_at, "
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
                    or ("Built-in sample pack" if r["id"] == 6 else ""),
                    "created_at": r["created_at"],
                    "entities": r["entities"],
                    "datasets": r["datasets"],
                    "is_active": r["id"] == active,
                    "selected": r["id"] == chosen,
                }
            )
    return {"packs": out, "selected_id": chosen}


class MyPackIn(BaseModel):
    pack_id: Optional[int] = None


@app.post("/api/my/pack")
async def choose_pack(
    data: MyPackIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Persist the user's scenario selection (survives reloads); pass
    pack_id=null to fall back to the globally active pack."""
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


@app.get("/api/dashboard")
async def dashboard_data(user: Dict[str, Any] = Depends(auth.current_user)):
    """Everything the Dashboard needs in one call: scenario banner (which
    pack, from what data), computed KPIs and the monthly timeseries."""
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


@app.get("/api/config")
async def public_config(user: Optional[Dict[str, Any]] = Depends(auth.optional_user)):
    with get_conn() as conn:
        # Resolve the effective pack: user's personal pick wins when logged in,
        # otherwise fall back to the globally active admin pack.
        if user is not None:
            pid = user_pack_id(conn, user)
        else:
            pid = adm.active_pack_id(conn)
        pack = adm.get_pack(conn, pid) if pid else None
        models = llm.list_configs(conn)
        default = next((m for m in models if m.get("is_default")), None)
    return {
        "system_name": cfg.SYSTEM_NAME,
        "version": cfg.VERSION,
        "modes": ["auto", "rules", "ai"],
        "pack": {
            "id": pid,
            "name": pack["name"] if pack else None,
            "description": pack.get("description", "") if pack else "",
        },
        "model": {
            "configured": bool(default),
            "name": default.get("name") if default else None,
            "model": default.get("model") if default else None,
        },
    }


@app.get("/api/questions")
async def questions(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        items = svc.suggested_questions(pid)
    return {"questions": items}


@app.get("/api/graph")
async def graph(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"graph": {"nodes": [], "links": []}, "narrative": ""}
        pack = svc.load_pack(conn, pid)
    visual = svc.visual_graph(pack)
    return {"graph": visual, "narrative": svc.render_narrative(pack)}


@app.get("/api/class-graph")
async def class_graph(user: Dict[str, Any] = Depends(auth.current_user)):
    """Class-level relationship graph (entities + relations, not graph_nodes/edges)."""
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"graph": {"nodes": [], "links": []}}
        pack = svc.load_pack(conn, pid)
    return {"graph": svc.class_graph(pack)}


@app.get("/api/graph/path")
async def graph_path(
    source: str,
    target: str,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Shortest directed path between two graph nodes (BFS)."""
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


@app.get("/api/graph/impact")
async def graph_impact(
    node: str,
    hops: int = 2,
    direction: str = "downstream",
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """N-hop downstream / upstream impact analysis from a node."""
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


@app.get("/api/graph/cycles")
async def graph_cycles(user: Dict[str, Any] = Depends(auth.current_user)):
    """Directed cycles in the impact graph (should normally be zero)."""
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"count": 0, "cycles": []}
        pack = svc.load_pack(conn, pid)
    return svc.detect_cycles(pack)


# ---------------------------------------------------------------------------
# Analytics — time series, MoM driver attribution, scenario, agent health
# ---------------------------------------------------------------------------


@app.get("/api/analytics/timeseries")
async def api_timeseries(user: Dict[str, Any] = Depends(auth.current_user)):
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


@app.get("/api/analytics/attribution")
async def api_attribution(
    month: Optional[str] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"attribution": None}
    return {"attribution": analytics.mom_attribution(svc.pack_tables(pack), month)}


@app.get("/api/analytics/scenario")
async def api_scenario(
    multiplier: float = 2.0,
    planned_active: Optional[int] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
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


@app.get("/api/analytics/agent-stats")
async def api_agent_stats(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid is not None else None
    if not pack:
        return {"agents": None}
    return {"agents": analytics.agent_stats(svc.pack_tables(pack))}


@app.get("/api/library")
async def library(user: Dict[str, Any] = Depends(auth.current_user)):
    """Read-only glossary + data inventory for the user portal."""
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"pack": None, "entities": [], "relations": [], "tables": []}
        pack = svc.load_pack(conn, pid)
    return svc.library_payload(pack, pid)


@app.get("/api/data/rows")
async def user_data_rows(
    table_id: int,
    limit: int = 50,
    offset: int = 0,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Paginated rows of a table in the user's active pack — powers the data
    centre's full-dataset browser (the library endpoint only carries a small
    preview of each table)."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        t = conn.execute(
            "SELECT id FROM pack_tables WHERE id=? AND pack_id=?", (table_id, pid)
        ).fetchone()
        if t is None:
            return JSONResponse(
                status_code=404,
                content={"error": "Table not found in your active pack."},
            )
        columns = adm.table_columns(conn, table_id)
        result = adm.table_rows(conn, table_id, None, limit, offset)
    return {
        "columns": columns,
        "rows": result["rows"],
        "total": result["total"],
        "dataset_id": result["dataset_id"],
    }


# ---------------------------------------------------------------------------
# Q&A — rules engine first, configured LLM as the AI fallback (SSE + plain)
# ---------------------------------------------------------------------------


def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _log_llm_call(**kwargs: Any) -> None:
    """Persist an LLM call record (callback used by the QA pipeline)."""
    with get_conn() as conn:
        llm.log_call(conn, **kwargs)


@app.post("/api/ask-stream")
async def ask_stream(data: AskIn, user: Dict[str, Any] = Depends(auth.current_user)):
    question = data.question.strip()
    mode = data.mode if data.mode in ("auto", "rules", "ai") else "auto"

    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid else None
        model_cfg = _effective_model_cfg(conn, user["id"])
        # Long/short-term memory: recent Q&A of this user against this pack
        # (server-side, survives page reloads) + their saved questions.
        mem_rows = (
            conn.execute(
                "SELECT question, answer FROM ask_history WHERE user_id=? AND pack_id=? "
                "AND answer != '' ORDER BY id DESC LIMIT 4",
                (user["id"], pid),
            ).fetchall()
            if pid is not None
            else []
        )
    if not pack:

        async def _empty():
            yield _sse(
                {
                    "type": "error",
                    "error": "No active ontology pack. "
                    "Ask an administrator to create or activate one.",
                }
            )

        return StreamingResponse(_empty(), media_type="text/event-stream")

    computed = svc.compute_pack(pack, strict=False)

    # Server memory → conversation history (oldest first, after the
    # client-supplied session turns).
    memory: List[Dict[str, str]] = []
    for r in reversed(mem_rows):
        memory.append({"role": "user", "content": r["question"]})
        memory.append({"role": "assistant", "content": (r["answer"] or "")[:600]})
    history = list(data.history or [])
    if memory:
        history = history + memory

    async def events():
        info = {"source": None, "matched": None, "tokens": 0}
        answer_excerpt = ""
        async for ev in qa.stream_answer(
            question,
            mode,
            pack,
            computed,
            model_cfg,
            history=history or None,
            user_id=user["id"],
            logger=logger,
            log_call=_log_llm_call,
        ):
            if ev.get("type") == "done":
                info["source"] = ev.get("source")
                info["matched"] = ev.get("matched")
                info["tokens"] = int(ev.get("tokens") or 0)
            elif ev.get("type") == "token":
                answer_excerpt += ev.get("content", "")
            yield _sse(ev)
        # Persist the turn so it can be recalled later (memory + history UI).
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO ask_history(user_id, pack_id, question, answer, "
                    "source, matched, tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        user["id"],
                        pid,
                        question[:500],
                        answer_excerpt[:800],
                        info["source"] or mode,
                        0 if info["matched"] is False else 1,
                        info["tokens"],
                        adm.now(),
                    ),
                )
        except Exception:
            pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ask/compare")
async def ask_compare(data: AskIn, user: Dict[str, Any] = Depends(auth.current_user)):
    """Answer one question twice — with the ontology and without it — so a
    demo can show, side by side, what the ontology grounding adds to both the
    answer and the impact chain."""
    question = data.question.strip()
    if not question:
        return JSONResponse(status_code=422, content={"error": "Question is required."})
    mode = data.mode if data.mode in ("auto", "rules", "ai") else "auto"

    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        pack = svc.load_pack(conn, pid) if pid else None
        model_cfg = _effective_model_cfg(conn, user["id"])
        mem_rows = (
            conn.execute(
                "SELECT question, answer FROM ask_history WHERE user_id=? AND pack_id=? "
                "AND answer != '' ORDER BY id DESC LIMIT 4",
                (user["id"], pid),
            ).fetchall()
            if pid is not None
            else []
        )
    if not pack:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No active ontology pack — ask an administrator "
                "to create or activate one."
            },
        )

    # Same conversation memory as /api/ask-stream so follow-up questions
    # ("…compared to the previous month?") resolve in Compare mode too.
    memory: List[Dict[str, str]] = []
    for r in reversed(mem_rows):
        memory.append({"role": "user", "content": r["question"]})
        memory.append({"role": "assistant", "content": (r["answer"] or "")[:600]})
    history = list(data.history or [])
    if memory:
        history = history + memory

    computed = svc.compute_pack(pack, strict=False)
    result = await asyncio.to_thread(
        qa.compare_answers,
        question,
        mode,
        pack,
        computed,
        model_cfg,
        user_id=user["id"],
        history=history or None,
        logger=logger,
        log_call=_log_llm_call,
    )
    # Keep the grounded side in the user's history so later questions retain
    # context; the non-ontology answer is deliberately not stored.
    grounded = result["with_ontology"]
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ask_history(user_id, pack_id, question, answer, "
                "source, matched, tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    pid,
                    question[:500],
                    (grounded.get("answer") or "")[:800],
                    grounded.get("source") or mode,
                    1 if grounded.get("source") in ("rules", "ai") else 0,
                    0,
                    adm.now(),
                ),
            )
    except Exception:
        pass
    return result


@app.get("/api/ask/history")
async def ask_history(user: Dict[str, Any] = Depends(auth.current_user)):
    """The caller's recent asks against the active pack (cross-session)."""
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        if pid is None:
            return {"history": [], "saved": []}
        rows = conn.execute(
            "SELECT id, question, source, matched, created_at FROM ask_history "
            "WHERE user_id=? AND pack_id=? ORDER BY id DESC LIMIT 30",
            (user["id"], pid),
        ).fetchall()
        saved = conn.execute(
            "SELECT question FROM user_questions WHERE user_id=? "
            "AND (pack_id IS ? OR pack_id=?) ORDER BY id DESC LIMIT 20",
            (user["id"], pid, pid),
        ).fetchall()
    return {
        "history": [dict(r) for r in rows],
        "saved": [r["question"] for r in saved],
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@app.post("/api/reports")
async def generate_report(
    data: ReportIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    title = data.title.strip() or "Analytics Report"
    sections = data.sections
    if not data.use_ontology:
        # No-ontology mode: the report still renders, but as text only —
        # every section that needs the ontology (graph, evidence chains,
        # charts, analytics) is dropped rather than rendered empty.
        drop = {"evidence", "charts", "analytics", "ontology"}
        sections = [s for s in (sections or []) if s not in drop]
        # Never hand build_report an empty list: it would fall back to the
        # full default section set and re-introduce ontology visuals.
        sections = sections or ["summary"]
        title += " — No ontology (text-only)"
    with get_conn() as conn:
        rec = rb.build_report(
            conn,
            owner_id=user["id"],
            title=title,
            scope=data.scope,
            sections=sections,
            include_ai=data.include_ai,
            qa_items=data.qa_items,
            pack_id=None,
            actor=user["username"],
            text_only=not data.use_ontology,
        )
    return {"report": rec}


class SampleReportIn(BaseModel):
    title: str = ""
    include_ai: bool = False


@app.post("/api/reports/sample")
async def sample_report(
    data: SampleReportIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """One-click demo report: recommended questions are answered by the
    deterministic rules engine (no waiting on AI), then rendered into a
    full MD/HTML/PDF report with charts, ontology graph and analytics."""
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if not pack:
            return JSONResponse(status_code=400, content={"error": "No active pack."})
        rec = _auto_pack_report(
            conn, user, pack, data.title.strip() or "Sample Analytics Report"
        )
    return {"report": rec}


@app.get("/api/reports")
async def my_reports(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        return {"reports": rb.list_reports(conn, user)}


@app.get("/api/reports/{uid}/download/{kind}")
async def download_report(
    uid: str, kind: str, user: Dict[str, Any] = Depends(auth.current_user)
):
    if kind not in ("md", "html", "pdf"):
        return JSONResponse(status_code=404, content={"error": "Unknown format."})
    with get_conn() as conn:
        path = rb.report_file(conn, uid, kind, user)
    media = {"md": "text/markdown", "html": "text/html", "pdf": "application/pdf"}[kind]
    return FileResponse(path, media_type=media, filename=f"{uid}.{kind}")


@app.get("/api/reports/{uid}/view")
async def view_report(uid: str, user: Dict[str, Any] = Depends(auth.current_user)):
    """Open the generated HTML report inline in the browser."""
    with get_conn() as conn:
        path = rb.report_file(conn, uid, "html", user)
    return FileResponse(path, media_type="text/html")


@app.delete("/api/reports/{uid}")
async def remove_report(uid: str, user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        rb.delete_report(conn, uid, user, user["username"])
    return {"ok": True}


# ===========================================================================
# ADMIN API
# ===========================================================================


@app.get("/api/admin/stats")
async def admin_stats(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return adm.dashboard_stats(conn)


# ---- packs ---------------------------------------------------------------


@app.get("/api/admin/packs")
async def admin_packs(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return {"packs": adm.list_packs(conn), "active_id": adm.active_pack_id(conn)}


@app.post("/api/admin/packs")
async def admin_pack_create(
    data: PackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        pid = adm.create_pack(
            conn,
            admin["username"],
            slug=data.slug,
            name=data.name,
            description=data.description,
        )
        return {"id": pid}


@app.post("/api/admin/packs/{pid}/activate")
async def admin_pack_activate(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.activate_pack(conn, admin["username"], pid)
    return {"ok": True}


@app.post("/api/admin/packs/{pid}/duplicate")
async def admin_pack_duplicate(
    pid: int, data: DuplicateIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        new_id = adm.duplicate_pack(
            conn, admin["username"], pid, slug=data.slug, name=data.name
        )
    return {"id": new_id}


@app.post("/api/admin/packs/{pid}/reset")
async def admin_pack_reset(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        pack = adm.get_pack(conn, pid)
        adm.reset_builtin_pack(conn, admin["username"], pack["slug"])
    return {"ok": True}


@app.delete("/api/admin/packs/{pid}")
async def admin_pack_delete(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.delete_pack(conn, admin["username"], pid)
    return {"ok": True}


@app.get("/api/admin/packs/{pid}/export")
async def admin_pack_export(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return adm.export_pack(conn, pid)


@app.post("/api/admin/packs/import")
async def admin_pack_import(
    data: ImportPackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    try:
        with get_conn() as conn:
            pid = adm.import_pack(
                conn,
                admin["username"],
                data.payload,
                slug_override=data.slug,
                activate=data.activate,
            )
            conn.execute(
                "UPDATE ontology_packs SET origin=? WHERE id=?",
                (f"Imported from JSON file by {admin['username']}", pid),
            )
    except (adm.AdminValueError, adm.AdminConflict) as e:
        return _err(e, e.status_code)
    except sqlite3.IntegrityError as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Pack data conflicts with existing records: {e}. "
                "Entity/attribute/relation keys must be unique within the pack."
            },
        )
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Invalid pack file structure: {e}. Please check the JSON format."
            },
        )
    return {"id": pid}


@app.post("/api/admin/packs/import/preview")
async def admin_pack_import_preview(
    data: ImportPackIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Dry-run import — validate and summarise a pack file without writing."""
    try:
        with get_conn() as conn:
            return adm.preview_import(conn, data.payload, slug_override=data.slug)
    except (adm.AdminValueError, adm.AdminConflict) as e:
        return _err(e, e.status_code)
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Invalid pack file structure: {e}. Please check the JSON format."
            },
        )


@app.get("/api/admin/packs/{pid}/versions")
async def admin_pack_versions(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return {"versions": adm.list_versions(conn, pid)}


@app.post("/api/admin/packs/{pid}/versions")
async def admin_pack_snapshot(
    pid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        vid = adm.snapshot_pack(conn, admin["username"], pid, label="manual")
    return {"id": vid}


@app.post("/api/admin/packs/{pid}/versions/{version_id}/restore")
async def admin_pack_restore(
    pid: int, version_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.restore_version(conn, admin["username"], pid, version_id)
    return {"ok": True}


# ---- ontology elements ----------------------------------------------------


_ONTO_KINDS = {
    "entity": (adm.upsert_entity, adm.delete_entity),
    "relation": (adm.upsert_relation, adm.delete_relation),
    "attribute": (adm.upsert_attribute, adm.delete_attribute),
    "node": (adm.upsert_node, adm.delete_node),
    "edge": (adm.upsert_edge, adm.delete_edge),
    "formula": (adm.upsert_formula, adm.delete_formula),
    "rule": (adm.upsert_rule, adm.delete_rule),
}


@app.get("/api/admin/ontology")
async def admin_ontology(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        pack = svc.load_pack(conn, pack_id)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    return {
        "pack": {k: pack.get(k) for k in ("id", "slug", "name", "description")},
        "entities": pack.get("entities", []),
        "attributes": pack.get("attributes", []),
        "relations": pack.get("relations", []),
        "nodes": pack.get("nodes", []),
        "edges": pack.get("edges", []),
        "formulas": pack.get("formulas", []),
        "rules": pack.get("rules", []),
    }


@app.get("/api/admin/ontology/health")
async def admin_ontology_health(
    pack_id: Optional[int] = None,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Structural integrity report (orphan nodes, alias collisions, connectivity…)."""
    with get_conn() as conn:
        pid = pack_id if pack_id is not None else adm.active_pack_id(conn)
        if pid is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        pack = svc.load_pack(conn, pid)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    health = svc.ontology_health(pack)
    health["pack_id"] = pack["id"]
    health["pack_name"] = pack["name"]
    return health


class SyncGraphIn(BaseModel):
    pack_id: int


@app.post("/api/admin/ontology/sync-graph")
async def admin_sync_graph(
    data: SyncGraphIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Rebuild the curated impact graph (graph_nodes/edges) from the
    entities/relations, so both graph layers correspond."""
    with get_conn() as conn:
        try:
            counts = adm.sync_visual_graph(conn, admin["username"], data.pack_id)
            adm.bump_pack_version(conn, admin["username"], data.pack_id)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, e.status_code)
    return {"ok": True, **counts}


@app.get("/api/admin/visual-graph")
async def admin_visual_graph(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Impact graph (what users see on the dashboard) for any pack."""
    with get_conn() as conn:
        pack = svc.load_pack(conn, pack_id)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "Pack not found."})
    return {"graph": svc.visual_graph(pack)}


@app.get("/api/formulas/{formula_key}/impact")
async def formula_impact_endpoint(
    formula_key: str, user: Dict[str, Any] = Depends(auth.current_user)
):
    """What depends on a formula — downstream formulas, graph nodes, rules."""
    with get_conn() as conn:
        pack = svc.load_pack(conn)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    return svc.formula_impact(pack, formula_key)


@app.get("/api/search")
async def ontology_search(
    q: str,
    pack_id: Optional[int] = None,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Full-text search across the active ontology's entities, attributes,
    relations, formulas and rules (snippets + kind + key)."""
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
    """Human-readable analysis card for the top search match: what it is,
    which tables back it, how it connects, which formulas use it."""
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
                    + ", ".join((a.get("label_en") or a["key"]) for a in attrs[:8])
                    + ("…" if len(attrs) > 8 else ""),
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
            for r in rels[:6]:
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
                    ", ".join((m.get("label") or m["key"]) for m in formulas[:6]),
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
                        for e in ins[:6]
                    ),
                ]
            )
        if outs:
            facts.append(
                [
                    "Drives",
                    "; ".join(
                        f"{e['target_key']} ({e.get('label') or e.get('edge_type', '')})"
                        for e in outs[:6]
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


@app.get("/api/search/insight")
async def ontology_search_insight(
    q: str,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Ontology search + instant understanding: matched chips, the affected
    graph neighbourhood (matched nodes + 1 hop) and a generated text
    analysis for the top match."""
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


# ---------------------------------------------------------------------------
# Graph studio — user-driven graph building with provenance
# ---------------------------------------------------------------------------


class GraphBuildIn(BaseModel):
    view: str = "schema"  # schema | lineage | columns
    table_ids: Optional[List[int]] = None
    include_unmatched: bool = True


@app.get("/api/graph-studio/sources")
async def graph_studio_sources(user: Dict[str, Any] = Depends(auth.current_user)):
    """Sources the user can pick from: ontology schema + data tables, each
    table carrying its dataset provenance (which upload/file, when)."""
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        ds = {}
        if pack is not None:
            pid = pack.get("id")
            if pid is not None:
                for r in conn.execute(
                    "SELECT table_id, name, filename, kind, row_count, created_at "
                    "FROM datasets WHERE pack_id=?",
                    (pid,),
                ).fetchall():
                    ds.setdefault(r["table_id"], []).append(dict(r))
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    out = svc.graph_studio_sources(pack)
    for t in out.get("tables", []):
        rows = ds.get(t["id"]) or []
        if rows:
            d = rows[-1]
            t["dataset_name"] = d["name"]
            t["dataset_file"] = d["filename"]
            t["dataset_kind"] = d["kind"]
            t["dataset_updated"] = d["created_at"]
        else:
            t["dataset_name"] = ""
            t["dataset_file"] = ""
            t["dataset_kind"] = ""
            t["dataset_updated"] = ""
    return out


def _ai_enrich_ontology(
    conn,
    pack: Dict[str, Any],
    payload: Dict[str, Any],
    table_ids: List[int],
) -> List[str]:
    """Optional AI pass over a freshly inferred ontology: fills entity /
    attribute descriptions and proposes starter questions. Best-effort —
    returns [] when no model is configured or anything goes wrong."""
    try:
        model_cfg = llm.get_default_client_config(conn)
    except Exception:
        model_cfg = None
    if model_cfg is None:
        return []
    tables = [t for t in pack.get("tables", []) if t.get("id") in set(table_ids)]
    lines = []
    for t in tables:
        cols = ", ".join(
            f"{c['key']}({c.get('data_type', 'STRING')})"
            for c in (t.get("columns") or [])[:25]
        )
        sample = (t.get("rows") or [{}])[0] if t.get("rows") else {}
        sample_s = json.dumps(sample, ensure_ascii=False, default=str)[:400]
        lines.append(f"Table {t['key']} (columns: {cols}); sample row: {sample_s}")
    ents = "\n".join(
        f"- {e['key']}: {e.get('description', '')}" for e in payload.get("entities", [])
    )
    prompt = (
        "You are helping to document a freshly generated insurance ontology. "
        "Below are the source tables, the derived entities and their current "
        "auto-generated descriptions.\n\nSOURCE TABLES:\n"
        + "\n".join(lines)
        + "\n\nENTITIES:\n"
        + ents
        + '\n\nReturn STRICT JSON only (no markdown fence): {"entities": '
        '{"<entity_key>": "one-sentence business description"}, "attributes": '
        '{"<entity_key>.<attribute_key>": "short description"}, "questions": '
        "[5 example business questions answerable from these tables]}"
    )
    try:
        res = llm.ConfiguredLLM(model_cfg).answer(
            "You document insurance ontologies. Reply with STRICT JSON only.",
            prompt,
        )
        text = (res.get("response") or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        obj = json.loads(text)
        ent_desc = obj.get("entities") or {}
        attr_desc = obj.get("attributes") or {}
        for e in payload.get("entities", []):
            d = ent_desc.get(e["key"])
            if d and not (e.get("description") or "").strip():
                e["description"] = str(d)
        for a in payload.get("attributes", []):
            d = attr_desc.get(f"{a.get('entity_key')}.{a.get('key')}")
            if d and not (a.get("description") or "").strip():
                a["description"] = str(d)
        return [str(q) for q in (obj.get("questions") or [])][:8]
    except Exception:
        return []


@app.post("/api/graph-studio/build")
async def graph_studio_build(
    data: GraphBuildIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Compose one of the three compact analysis graphs from the sources
    the user picked. Every node carries a provenance record."""
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    view = data.view if data.view in ("schema", "lineage", "columns") else "schema"
    return svc.build_studio_graph(
        pack,
        view=view,
        table_ids=data.table_ids,
        include_unmatched=data.include_unmatched,
    )


@app.post("/api/graph-studio/upload")
async def graph_studio_upload(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Build an ad-hoc graph from an uploaded CSV/XLSX data file or an
    ontology JSON file — in memory only, nothing is written to the database.
    Columns are matched against the active ontology so the graph shows how
    the new content connects to existing concepts (with provenance)."""
    filename = file.filename or "upload"
    content = await file.read()
    with get_conn() as conn:
        pack = resolve_pack(conn, user)

    if filename.lower().endswith(".json"):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise imp.UploadError(f"Invalid JSON file: {e}", 422)
        if not isinstance(payload, dict) or not (
            payload.get("entities") or payload.get("classes")
        ):
            raise imp.UploadError(
                "JSON must be an ontology definition (with entities) — "
                "BP2 or onti-pack/v1 format.",
                422,
            )
        norm = adm._normalize_pack_payload(payload)
        virtual_pack = {
            "id": 0,
            "entities": norm.get("entities", []),
            "attributes": norm.get("attributes", []),
            "relations": norm.get("relations", []),
            "tables": [],
        }
        graph = svc.build_studio_graph(virtual_pack, view="schema")
        for n in graph["nodes"]:
            n["source"]["kind"] = "Uploaded ontology"
            n["source"]["detail"] = (
                "From uploaded file '" + filename + "': " + n["source"]["detail"]
            )
        graph["sources_used"] = sorted(
            {n["source"]["kind"] + ": " + n["source"]["detail"] for n in graph["nodes"]}
        )
        with get_conn() as conn:
            _record_studio(
                conn,
                user,
                pack.get("id") if pack else None,
                pack.get("name", "") if pack else "",
                "ontology-upload",
                f"Ontology graph from {filename}",
                f"Uploaded ontology JSON '{filename}' previewed against "
                f"{len(graph.get('nodes', []))} nodes",
            )
        return graph

    df = imp.parse_data_file(filename, content)
    sample = df.iloc[0].to_dict() if len(df) else {}
    graph = svc.graph_from_upload(
        pack, filename, [str(c) for c in df.columns], len(df), sample
    )
    with get_conn() as conn:
        _record_studio(
            conn,
            user,
            pack.get("id") if pack else None,
            pack.get("name", "") if pack else "",
            "data-upload",
            f"Graph from {filename}",
            f"CSV/XLSX '{filename}' — {len(df)} rows x {len(df.columns)} columns "
            "previewed against the active ontology",
        )
    return graph


class InferOntoIn(BaseModel):
    table_ids: List[int]
    name: str = ""
    slug: str = ""
    ai_enrich: bool = False


class CreateOntoIn(BaseModel):
    table_ids: List[int]
    name: str
    slug: str = ""
    ai_enrich: bool = False


@app.post("/api/graph-studio/infer")
async def graph_studio_infer(
    data: InferOntoIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Preview the ontology that would be generated from the picked
    tables — entities, attributes, inferred relations and counts. Nothing
    is written."""
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
    if pack is None:
        return JSONResponse(status_code=404, content={"error": "No active pack."})
    ai_questions: List[str] = []
    try:
        payload = svc.infer_ontology(pack, data.table_ids, data.name, data.slug)
        if data.ai_enrich:
            with get_conn() as conn:
                ai_questions = _ai_enrich_ontology(conn, pack, payload, data.table_ids)
        with get_conn() as conn:
            _record_studio(
                conn,
                user,
                pack.get("id"),
                pack.get("name", ""),
                "preview",
                data.name.strip() or f"Preview ({len(data.table_ids)} tables)",
                f"Ontology preview from {len(data.table_ids)} table(s): "
                f"{len(payload['entities'])} entities, "
                f"{len(payload['relations'])} relations"
                + (" + AI enrichment" if data.ai_enrich else ""),
            )
    except svc.EvalError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    preview_pack = {
        "entities": payload["entities"],
        "attributes": payload["attributes"],
        "relations": payload["relations"],
        "tables": [],
    }
    graph = svc.build_studio_graph(preview_pack, view="schema")
    return {
        "counts": {
            "entities": len(payload["entities"]),
            "attributes": len(payload["attributes"]),
            "relations": len(payload["relations"]),
        },
        "entities": [
            {"key": e["key"], "label": e["label_en"], "description": e["description"]}
            for e in payload["entities"]
        ],
        "relations": [
            {
                "key": r["key"],
                "label": r["label_en"],
                "cardinality": r["cardinality"],
                "source": r["source_key"],
                "target": r["target_key"],
                "description": r["description"],
            }
            for r in payload["relations"]
        ],
        "ai_questions": ai_questions,
        "graph": graph,
    }


@app.post("/api/graph-studio/create-ontology")
async def graph_studio_create(
    data: CreateOntoIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Generate an ontology from the picked knowledge-base tables, import
    it as a new pack (with the data copied) and activate it — the user can
    immediately ask questions and build reports on it. A default analytics
    report is generated automatically and the action is written to the
    Graph studio generation history."""
    name = data.name.strip()
    if not name:
        return JSONResponse(status_code=422, content={"error": "Name is required."})
    with get_conn() as conn:
        pack = resolve_pack(conn, user)
        if pack is None:
            return JSONResponse(status_code=404, content={"error": "No active pack."})
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "ontology"
        slug = base
        i = 2
        while conn.execute(
            "SELECT 1 FROM ontology_packs WHERE slug=?", (slug,)
        ).fetchone():
            slug = f"{base}-{i}"[:49]
            i += 1
        try:
            payload = svc.infer_ontology(pack, data.table_ids, name, slug)
            if data.ai_enrich:
                _ai_enrich_ontology(conn, pack, payload, data.table_ids)
            pid = adm.import_pack(conn, user["username"], payload, activate=True)
            # Provenance: record how this ontology was created and from what.
            # Table ids refer to the SOURCE pack — resolve names there.
            src_names = [
                r["label"] or r["key"]
                for r in conn.execute(
                    "SELECT key, label FROM pack_tables WHERE pack_id=? AND id IN "
                    f"({','.join('?' * len(data.table_ids))})",
                    (pack["id"], *data.table_ids),
                ).fetchall()
            ]
            origin = (
                f"Generated in Graph studio by {user['username']} from "
                f"{len(data.table_ids)} table(s): "
                + (", ".join(src_names) or "unknown")
                + (" + AI enrichment" if data.ai_enrich else "")
            )
            conn.execute("UPDATE ontology_packs SET origin=? WHERE id=?", (origin, pid))
            _record_studio(
                conn,
                user,
                pid,
                name,
                "ontology",
                f"{name} created from {len(data.table_ids)} table(s)",
                origin,
            )
            audit(
                conn,
                user["username"],
                "pack.infer_create",
                f"pack={pid} tables={data.table_ids}",
            )
            # The new pack is now active for this user: generate its default
            # report automatically (MD + HTML + PDF) so there is always a
            # consumable output right after creation.
            new_pack = svc.load_pack(conn, pid)
            report = None
            if new_pack is not None:
                try:
                    report = _auto_pack_report(conn, user, new_pack, f"{name} Report")
                    _record_studio(
                        conn,
                        user,
                        pid,
                        name,
                        "report",
                        f"Auto report for {name}",
                        report.get("uid", ""),
                    )
                except Exception:  # noqa: BLE001 - report must not block creation
                    report = None
        except svc.EvalError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"pack_id": pid, "slug": slug, "name": name, "report": report}


def _record_studio(
    conn,
    user: Dict[str, Any],
    pack_id: Optional[int],
    pack_name: str,
    action: str,
    title: str,
    detail: str,
) -> None:
    """Append an entry to the user's Graph studio generation history."""
    conn.execute(
        "INSERT INTO studio_history(user_id, username, pack_id, pack_name, "
        "action, title, detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            user["id"],
            user["username"],
            pack_id,
            pack_name,
            action,
            title[:300],
            (detail or "")[:900],
            db_mod.now(),
        ),
    )


@app.get("/api/graph-studio/history")
async def graph_studio_history(user: Dict[str, Any] = Depends(auth.current_user)):
    """The caller's generation history: graphs built from uploads, ontology
    previews/creations and auto-generated reports — newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, pack_id, pack_name, action, title, detail, created_at "
            "FROM studio_history WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}


@app.get("/api/recommended-questions")
async def recommended_questions(user: Dict[str, Any] = Depends(auth.current_user)):
    """Smart question suggestions derived from the active ontology."""
    with get_conn() as conn:
        pack = svc.load_pack(conn)
    if pack is None:
        return {"questions": []}
    return {"questions": svc.recommended_questions(pack)}


@app.get("/api/my-questions")
async def my_questions(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        rows = conn.execute(
            "SELECT id, question, created_at FROM user_questions "
            "WHERE user_id=? AND (pack_id IS ? OR pack_id=?) ORDER BY id DESC",
            (user["id"], pid, pid),
        ).fetchall()
    return {"questions": [dict(r) for r in rows]}


class MyQuestionIn(BaseModel):
    question: str


@app.post("/api/my-questions")
async def add_my_question(
    data: MyQuestionIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    q = data.question.strip()
    if not q:
        return JSONResponse(status_code=422, content={"error": "Empty question."})
    with get_conn() as conn:
        pid = user_pack_id(conn, user)
        dup = conn.execute(
            "SELECT id FROM user_questions WHERE user_id=? AND question=?",
            (user["id"], q),
        ).fetchone()
        if dup:
            return {"id": dup["id"], "deduplicated": True}
        cur = conn.execute(
            "INSERT INTO user_questions(user_id, pack_id, question, created_at) "
            "VALUES(?,?,?,?)",
            (user["id"], pid, q, adm.now()),
        )
        return {"id": int(cur.lastrowid)}


@app.delete("/api/my-questions/{qid}")
async def delete_my_question(
    qid: int, user: Dict[str, Any] = Depends(auth.current_user)
):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_questions WHERE id=? AND user_id=?",
            (qid, user["id"]),
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# User-side model selection — pick any enabled model for your own answers
# ---------------------------------------------------------------------------


class ModelSelectIn(BaseModel):
    # "sys:<id>" (admin-registered) or "my:<id>" (caller's own Model centre
    # entry); null → fall back to the system default.
    config_id: Optional[str] = None


def _user_model_pref(conn, user_id: int) -> Optional[str]:
    raw = get_setting(conn, f"user_model:{user_id}", "")
    return raw or None


def _parse_model_pref(pref: Optional[str]) -> Optional[Tuple[str, int]]:
    """'sys:12' → ('sys', 12); 'my:3' → ('my', 3); plain '12' → ('sys', 12)."""
    if not pref:
        return None
    s = str(pref).strip()
    try:
        if ":" in s:
            kind, mid = s.split(":", 1)
            if kind in ("sys", "my"):
                return kind, int(mid)
        return "sys", int(s)
    except (TypeError, ValueError):
        return None


def _effective_model_cfg(conn, user_id: int) -> Optional[Dict[str, Any]]:
    """The model that answers this user: their own pick (system or personal)
    if still available, otherwise the system default."""
    parsed = _parse_model_pref(_user_model_pref(conn, user_id))
    if parsed is not None:
        kind, mid = parsed
        cfg = (
            llm.get_user_client_config_by_id(conn, user_id, mid)
            if kind == "my"
            else llm.get_client_config_by_id(conn, mid)
        )
        if cfg is not None:
            return cfg
    return llm.get_default_client_config(conn)


@app.get("/api/models")
async def list_user_models(user: Dict[str, Any] = Depends(auth.current_user)):
    """Models the caller may use — admin-registered (enabled) plus their own
    Model centre entries — safe fields only, plus the current selection."""
    with get_conn() as conn:
        configs = []
        for m in llm.list_configs(conn):
            if not m["enabled"]:
                continue
            configs.append(
                {
                    "id": f"sys:{m['id']}",
                    "name": m["name"],
                    "model": m["model"],
                    "is_default": m["is_default"],
                    "mine": False,
                }
            )
        for m in llm.list_user_configs(conn, user["id"]):
            configs.append(
                {
                    "id": f"my:{m['id']}",
                    "name": m["name"],
                    "model": m["model"],
                    "is_default": False,
                    "mine": True,
                }
            )
        selected = _user_model_pref(conn, user["id"])
        if selected is not None and not any(c["id"] == selected for c in configs):
            selected = None
    return {"models": configs, "selected": selected}


@app.post("/api/models/select")
async def select_user_model(
    data: ModelSelectIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Remember the caller's model choice (per-user, reversible any time)."""
    with get_conn() as conn:
        if data.config_id is None:
            set_setting(conn, f"user_model:{user['id']}", "")
            return {"selected": None}
        parsed = _parse_model_pref(data.config_id)
        if parsed is None:
            return JSONResponse(status_code=422, content={"error": "Invalid model id."})
        kind, mid = parsed
        if kind == "my":
            cfg = llm.get_user_client_config_by_id(conn, user["id"], mid)
        else:
            cfg = llm.get_client_config_by_id(conn, mid)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"error": "Model not found or unavailable."},
            )
        stored = f"{kind}:{mid}"
        set_setting(conn, f"user_model:{user['id']}", stored)
        return {"selected": stored, "name": cfg["name"], "model": cfg["model"]}


# ---- user Model centre -----------------------------------------------------


class UserModelIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: float = 90.0
    verify_ssl: bool = True
    write_env: bool = True


@app.get("/api/my/models")
async def my_models_list(user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        return {
            "models": llm.list_user_configs(conn, user["id"]),
            "selected": _user_model_pref(conn, user["id"]),
        }


@app.post("/api/my/models")
async def my_models_create(
    data: UserModelIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    with get_conn() as conn:
        try:
            mid = llm.create_user_config(
                conn,
                user["id"],
                user["username"],
                name=data.name,
                base_url=data.base_url,
                api_key=data.api_key,
                model=data.model,
                temperature=data.temperature,
                max_tokens=data.max_tokens,
                timeout=data.timeout,
                verify_ssl=data.verify_ssl,
                write_env=data.write_env,
            )
        except llm.ModelConfigError as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.message})
    return {"id": mid}


@app.put("/api/my/models/{mid}")
async def my_models_update(
    mid: int,
    data: UserModelIn,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    with get_conn() as conn:
        try:
            llm.update_user_config(
                conn,
                user["id"],
                mid,
                {
                    "name": data.name,
                    "base_url": data.base_url,
                    "api_key": data.api_key or "__unchanged__",
                    "model": data.model,
                    "temperature": data.temperature,
                    "max_tokens": data.max_tokens,
                    "timeout": data.timeout,
                    "verify_ssl": data.verify_ssl,
                },
            )
        except llm.ModelConfigError as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.message})
    return {"ok": True}


@app.delete("/api/my/models/{mid}")
async def my_models_delete(mid: int, user: Dict[str, Any] = Depends(auth.current_user)):
    with get_conn() as conn:
        try:
            llm.delete_user_config(conn, user["id"], mid)
        except llm.ModelConfigError as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.message})
        if _parse_model_pref(_user_model_pref(conn, user["id"])) == ("my", mid):
            set_setting(conn, f"user_model:{user['id']}", "")
    return {"ok": True}


class MyModelTestIn(BaseModel):
    config_id: Optional[str] = None  # "my:N" for a saved user model
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    verify_ssl: Optional[bool] = None


@app.post("/api/my/models/test")
async def my_models_test(
    data: MyModelTestIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    with get_conn() as conn:
        if data.config_id and str(data.config_id).startswith("my:"):
            try:
                mid = int(str(data.config_id).split(":", 1)[1])
            except ValueError:
                mid = -1
            ucfg = llm.get_user_client_config_by_id(conn, user["id"], mid)
            if ucfg is None:
                return JSONResponse(
                    status_code=404, content={"error": "Model not found."}
                )
            cfg_dict = {
                "name": ucfg["name"],
                "base_url": ucfg["base_url"],
                "api_key": ucfg["api_key"],
                "model": ucfg["model"],
                "temperature": ucfg.get("temperature", 0.3),
                "max_tokens": ucfg.get("max_tokens", 1024),
                "timeout": ucfg.get("timeout", 90.0),
                "verify_ssl": bool(
                    ucfg.get("verify_ssl", True)
                    if data.verify_ssl is None
                    else data.verify_ssl
                ),
            }
        else:
            cid: Optional[int] = None
            if data.config_id:
                try:
                    cid = int(str(data.config_id))
                except ValueError:
                    return JSONResponse(
                        status_code=422, content={"error": "Invalid model id."}
                    )
            cfg_dict = llm.resolve_test_config(
                conn,
                config_id=cid,
                name=data.name,
                base_url=data.base_url,
                api_key=data.api_key,
                model=data.model,
                verify_ssl=data.verify_ssl,
            )
    result = await llm.ConfiguredLLM(cfg_dict).test_connection()
    # Same contract as the admin endpoint: a finished test always answers 200,
    # with ``ok`` carrying the verdict and ``hint`` the next step.
    return {"ok": result.get("status") == "connected", **result}


@app.post("/api/admin/elements/{kind}")
async def admin_upsert(
    kind: str, data: ElementIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    if kind not in _ONTO_KINDS:
        return JSONResponse(status_code=404, content={"error": "Unknown element kind."})
    with get_conn() as conn:
        try:
            if data.expected_version is not None:
                adm.check_pack_version(conn, data.pack_id, data.expected_version)
            _ONTO_KINDS[kind][0](conn, admin["username"], data.pack_id, data.payload)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"ok": True}


@app.delete("/api/admin/elements/{kind}")
async def admin_delete(
    kind: str,
    pack_id: int,
    key: str,
    entity_key: str = "",
    expected_version: Optional[int] = None,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    if kind not in _ONTO_KINDS:
        return JSONResponse(status_code=404, content={"error": "Unknown element kind."})
    with get_conn() as conn:
        try:
            if expected_version is not None:
                adm.check_pack_version(conn, pack_id, expected_version)
            if kind == "attribute":
                _ONTO_KINDS[kind][1](conn, admin["username"], pack_id, entity_key, key)
            else:
                _ONTO_KINDS[kind][1](conn, admin["username"], pack_id, key)
        except (adm.AdminValueError, adm.AdminConflict) as e:
            return _err(e, getattr(e, "status_code", 400))
    return {"ok": True}


# ---- data centre ----------------------------------------------------------


@app.get("/api/admin/data/tables")
async def admin_tables(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return {"tables": adm.list_tables(conn, pack_id)}


@app.get("/api/admin/data/datasets")
async def admin_datasets(
    table_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return {"datasets": adm.list_datasets(conn, table_id)}


@app.post("/api/admin/data/datasets/{did}/activate")
async def admin_dataset_activate(
    did: int, body: IdIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.set_active_dataset(conn, admin["username"], int(body.value), did)
    return {"ok": True}


@app.delete("/api/admin/data/datasets/{did}")
async def admin_dataset_delete(
    did: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.delete_dataset(conn, admin["username"], did)
    return {"ok": True}


@app.get("/api/admin/data/rows")
async def admin_rows(
    table_id: int,
    dataset_id: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    with get_conn() as conn:
        columns = adm.table_columns(conn, table_id)
        result = adm.table_rows(conn, table_id, dataset_id, limit, offset)
    return {
        "columns": columns,
        "rows": result["rows"],
        "total": result["total"],
        "dataset_id": result["dataset_id"],
    }


@app.post("/api/admin/data/upload")
async def admin_upload(
    background_tasks: BackgroundTasks,
    pack_id: int = Form(...),
    table_id: int = Form(...),
    dataset_name: str = Form(""),
    commit: bool = Form(False),
    sheet: str = Form(""),
    mapping: str = Form(""),
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    content = await file.read()
    imp.validate_upload(file.filename or "", len(content))
    mapping_override = imp.parse_mapping(mapping)
    if not commit:
        _, report = imp.preview_upload(
            table_id, file.filename, content, sheet, mapping_override
        )
        return report
    with get_conn() as conn:
        result = imp.commit_upload(
            conn,
            admin["username"],
            table_id,
            dataset_name,
            file.filename,
            content,
            sheet,
            mapping_override,
        )
    # AI ontology induction runs after the response so a slow model never
    # blocks the upload; failures are isolated inside the task.
    background_tasks.add_task(
        _run_induction_bg,
        pack_id,
        table_id,
        result.get("dataset_id"),
        admin["username"],
    )
    return result


@app.post("/api/admin/data/batch-upload")
async def admin_batch_upload(
    background_tasks: BackgroundTasks,
    pack_id: int = Form(...),
    commit: bool = Form(False),
    plan: str = Form(""),
    files: List[UploadFile] = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Many files / every sheet of a workbook / a whole folder in one request.

    Preview (``commit=false``) returns a per-item routing + mapping report and
    writes nothing. Commit writes every valid item as its own dataset and
    creates any missing tables, each with a matching ontology entity and
    attributes. ``plan`` is an optional JSON list of per-item overrides
    ``[{file,item,target,mapping,skip}]``.
    """
    payload = []
    for f in files:
        content = await f.read()
        payload.append({"filename": f.filename or "", "content": content})
    if not payload:
        raise imp.UploadError("No files were uploaded.")
    actor = admin["username"]
    with get_conn() as conn:
        if not commit:
            return imp.analyse_batch(conn, pack_id, payload, plan)
        result = imp.commit_batch(conn, actor, pack_id, payload, plan)
    # Keep parity with the single-file flow: enrich each new dataset in the
    # background so a slow model never blocks the response.
    for item in result["items"]:
        background_tasks.add_task(
            _run_induction_bg, pack_id, item["table_id"], item.get("dataset_id"), actor
        )
    return result


def _run_induction_bg(pack_id: int, table_id: int, dataset_id, actor: str) -> None:
    try:
        with get_conn() as conn:
            induction.induce_from_table(
                conn, pack_id, table_id, dataset_id=dataset_id, actor=actor
            )
    except Exception:  # noqa: BLE001 — background task must never raise
        logger.exception("Background ontology induction failed")


# ---- AI ontology proposals ------------------------------------------------


@app.get("/api/admin/proposals")
async def admin_proposals(
    pack_id: int,
    status: str = "pending",
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    with get_conn() as conn:
        return {
            "proposals": induction.list_proposals(conn, pack_id, status),
            "pending": induction.pending_count(conn, pack_id),
        }


@app.post("/api/admin/proposals/{proposal_id}/approve")
async def admin_proposal_approve(
    proposal_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return induction.approve_proposal(conn, admin["username"], proposal_id)


@app.post("/api/admin/proposals/{proposal_id}/reject")
async def admin_proposal_reject(
    proposal_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return induction.reject_proposal(conn, admin["username"], proposal_id)


@app.post("/api/admin/packs/{pack_id}/proposals/approve-all")
async def admin_proposals_approve_all(
    pack_id: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return induction.approve_all(conn, admin["username"], pack_id)


@app.post("/api/admin/data/tables/{table_id}/analyze")
async def admin_table_analyze(
    table_id: int,
    data: AnalyzeIn,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    """Manually ask the AI to propose ontology additions for one table."""
    with get_conn() as conn:
        return induction.induce_from_table(
            conn, data.pack_id, table_id, actor=admin["username"]
        )


# ---- question bank --------------------------------------------------------


@app.get("/api/admin/questions")
async def admin_questions(
    pack_id: int,
    search: str = "",
    category: str = "",
    limit: int = 500,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    with get_conn() as conn:
        return {"questions": adm.list_questions(conn, pack_id, search, category, limit)}


@app.post("/api/admin/questions")
async def admin_question_add(
    data: QuestionIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        created = adm.add_question(
            conn,
            data.pack_id,
            data.question,
            data.category,
            data.source,
            admin["username"],
        )
    if not created:
        return JSONResponse(
            status_code=409,
            content={"error": "This question already exists in the bank."},
        )
    return {"ok": True}


@app.delete("/api/admin/questions/{qid}")
async def admin_question_delete(
    qid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        adm.delete_question(conn, admin["username"], qid)
    return {"ok": True}


@app.post("/api/admin/questions/import")
async def admin_question_import(
    pack_id: int = Form(...),
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    content = await file.read()
    imp.validate_upload(file.filename or "", len(content))
    items = imp.parse_question_file(file.filename, content)
    with get_conn() as conn:
        summary = adm.import_questions(conn, admin["username"], pack_id, items)
    return summary


# ---- knowledge graph ------------------------------------------------------


@app.get("/api/admin/instance-graph")
async def admin_instance_graph(
    pack_id: int,
    search: str = "",
    entity_filter: str = "",
    node_limit: int = 300,
    admin: Dict[str, Any] = Depends(auth.require_admin),
):
    with get_conn() as conn:
        return adm.instance_graph(
            conn,
            pack_id,
            node_limit=node_limit,
            entity_filter=entity_filter or None,
            search=search or None,
        )


# ---- model centre ---------------------------------------------------------


@app.get("/api/admin/models")
async def admin_models(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return {"models": llm.list_configs(conn)}


@app.post("/api/admin/models")
async def admin_model_create(
    data: ModelIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        mid = llm.create_config(
            conn,
            admin["username"],
            name=data.name,
            base_url=data.base_url,
            api_key=data.api_key,
            model=data.model,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
            timeout=data.timeout,
            verify_ssl=data.verify_ssl,
            enabled=data.enabled,
        )
    return {"id": mid}


@app.put("/api/admin/models/{mid}")
async def admin_model_update(
    mid: int, data: ModelIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        llm.update_config(conn, admin["username"], mid, data.model_dump())
    return {"ok": True}


@app.delete("/api/admin/models/{mid}")
async def admin_model_delete(
    mid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        llm.delete_config(conn, admin["username"], mid)
    return {"ok": True}


@app.post("/api/admin/models/{mid}/default")
async def admin_model_default(
    mid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        llm.set_default(conn, admin["username"], mid)
    return {"ok": True}


@app.post("/api/admin/models/test")
async def admin_model_test(
    data: ModelTestIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        cfg_dict = llm.resolve_test_config(
            conn,
            config_id=data.config_id,
            name=data.name,
            base_url=data.base_url,
            api_key=data.api_key,
            model=data.model,
            verify_ssl=data.verify_ssl,
        )
    result = await llm.ConfiguredLLM(cfg_dict).test_connection()
    # A completed test is a *successful request* even when the verdict is
    # "cannot connect": the payload was valid, the answer is simply negative.
    # Encoding a negative verdict as HTTP 400 made every failed test look like
    # a client error in the server log and pushed the UI through its exception
    # path, where the actionable hint is easy to lose.
    return {"ok": result.get("status") == "connected", **result}


@app.get("/api/admin/user-models")
async def admin_user_models(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Oversight: every user-created model (keys masked) so support can
    help users debug their own endpoints."""
    with get_conn() as conn:
        return {"models": llm.list_all_user_configs(conn)}


@app.get("/api/admin/user-settings")
async def admin_user_settings(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Oversight: per-user portal settings — the scenario pack and the model
    each user currently works with — so support sees exactly what the user
    sees without asking."""
    with get_conn() as conn:
        users = conn.execute(
            "SELECT id, username, display_name, role, last_login FROM users "
            "ORDER BY id"
        ).fetchall()
        out = []
        for u in users:
            pack_id = user_pack_id(conn, {"id": u["id"]})
            pack = (
                conn.execute(
                    "SELECT name FROM ontology_packs WHERE id=?", (pack_id,)
                ).fetchone()
                if pack_id is not None
                else None
            )
            parsed = _parse_model_pref(_user_model_pref(conn, u["id"]))
            model = None
            if parsed is not None:
                kind, mid = parsed
                table = "model_configs" if kind == "sys" else "user_model_configs"
                model = conn.execute(
                    f"SELECT name, model FROM {table} WHERE id=?", (mid,)
                ).fetchone()
            out.append(
                {
                    "user_id": u["id"],
                    "username": u["username"],
                    "display_name": u["display_name"],
                    "role": u["role"],
                    "last_login": u["last_login"],
                    "pack_id": pack_id,
                    "pack_name": pack["name"] if pack else None,
                    "is_pack_default": pack_id == adm.active_pack_id(conn),
                    "model_name": model["name"] if model else None,
                    "model_id": model["model"] if model else None,
                }
            )
    return {"settings": out}


# ---- users ----------------------------------------------------------------


@app.get("/api/admin/users")
async def admin_users(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return {"users": auth.list_users(conn)}


@app.post("/api/admin/users")
async def admin_user_create(
    data: UserIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        uid = auth.create_user(
            conn, data.username, data.password, data.display_name, data.role
        )
    return {"id": uid}


@app.post("/api/admin/users/{uid}/role")
async def admin_user_role(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        auth.set_user_role(conn, admin_user, uid, str(body.value))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/enabled")
async def admin_user_enabled(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        auth.set_user_enabled(conn, admin_user, uid, bool(body.value))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reset-password")
async def admin_user_pw(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        auth.reset_password(conn, admin_user, uid, str(body.value))
    return {"ok": True}


# ---- settings, audit, report centre --------------------------------------


class SettingsIn(BaseModel):
    allow_registration: Optional[bool] = None
    ai_auto_propose: Optional[bool] = None


@app.get("/api/admin/settings")
async def admin_settings(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return {
            "allow_registration": get_setting(conn, "allow_registration", "1") == "1",
            "ai_auto_propose": get_setting(conn, "ai_auto_propose", "1") == "1",
        }


@app.put("/api/admin/settings")
async def admin_settings_put(
    data: SettingsIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        if data.allow_registration is not None:
            set_setting(
                conn, "allow_registration", "1" if data.allow_registration else "0"
            )
        if data.ai_auto_propose is not None:
            set_setting(conn, "ai_auto_propose", "1" if data.ai_auto_propose else "0")
    return {"ok": True}


@app.get("/api/admin/audit")
async def admin_audit(
    limit: int = 100, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        return {"events": adm.list_audit(conn, limit)}


@app.get("/api/admin/reports")
async def admin_all_reports(admin: Dict[str, Any] = Depends(auth.require_admin)):
    with get_conn() as conn:
        return {"reports": rb.list_reports(conn, admin)}


@app.delete("/api/admin/reports/{uid}")
async def admin_report_delete(
    uid: str, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    with get_conn() as conn:
        rb.delete_report(conn, uid, admin_user, admin_user["username"])
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=cfg.HOST, port=cfg.PORT, reload=False)
