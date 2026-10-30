"""HTTP routes for the admin users area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel

from modules import admin_service as adm
from modules import auth
from modules import report_builder as rb
from modules.db import get_conn
from modules.db import get_setting
from modules.db import set_setting

from routes.common import IdIn
from routes.common import _parse_model_pref
from routes.common import _user_model_pref
from routes.common import user_pack_id

router = APIRouter()


class UserIn(BaseModel):
    """Request body for creating a user account.

    Attributes:
        username (str): Unique login name.
        password (str): Initial password.
        display_name (str): Optional human-readable name.
        role (str): ``user`` (default) or ``admin``.
    """

    username: str
    password: str
    display_name: str = ""
    role: str = "user"


@router.get("/api/admin/user-settings")
async def admin_user_settings(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Oversight: per-user portal settings.

    Shows the scenario pack and the model each user currently works with, so
    support sees exactly what the user sees without asking.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"settings": [{"user_id", "username", "pack_id", "pack_name",
        "is_pack_default", "model_name", "model_id", ...}]}``.
    """
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


@router.get("/api/admin/users")
async def admin_users(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """List every user account.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"users": [{"id", "username", "role", "enabled", ...}, ...]}``.
    """
    with get_conn() as conn:
        return {"users": auth.list_users(conn)}


@router.post("/api/admin/users")
async def admin_user_create(
    data: UserIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Create a user account.

    Args:
        data (UserIn): Username, password, display name and role.
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"id": int}`` of the new account.
    """
    with get_conn() as conn:
        uid = auth.create_user(
            conn, data.username, data.password, data.display_name, data.role
        )
    return {"id": uid}


@router.post("/api/admin/users/{uid}/role")
async def admin_user_role(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Change a user's role.

    Args:
        uid (int): Target user id.
        body (IdIn): Body whose ``value`` is the new role (``user``/``admin``).
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        auth.set_user_role(conn, admin_user, uid, str(body.value))
    return {"ok": True}


@router.post("/api/admin/users/{uid}/enabled")
async def admin_user_enabled(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Enable or disable a user account.

    Args:
        uid (int): Target user id.
        body (IdIn): Body whose ``value`` is truthy to enable the account.
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        auth.set_user_enabled(conn, admin_user, uid, bool(body.value))
    return {"ok": True}


@router.post("/api/admin/users/{uid}/reset-password")
async def admin_user_pw(
    uid: int, body: IdIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Reset a user's password to an administrator-supplied value.

    Args:
        uid (int): Target user id.
        body (IdIn): Body whose ``value`` is the new password.
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        auth.reset_password(conn, admin_user, uid, str(body.value))
    return {"ok": True}


class SettingsIn(BaseModel):
    """Request body for updating platform settings.

    Every field is optional: only the ones supplied are written.

    Attributes:
        allow_registration (Optional[bool]): Whether self-registration is open.
        ai_auto_propose (Optional[bool]): Whether uploads auto-propose AI
            ontology additions.
    """

    allow_registration: Optional[bool] = None
    ai_auto_propose: Optional[bool] = None


@router.get("/api/admin/settings")
async def admin_settings(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Read the current platform settings.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"allow_registration": bool, "ai_auto_propose": bool}``.
    """
    with get_conn() as conn:
        return {
            "allow_registration": get_setting(conn, "allow_registration", "1") == "1",
            "ai_auto_propose": get_setting(conn, "ai_auto_propose", "1") == "1",
        }


@router.put("/api/admin/settings")
async def admin_settings_put(
    data: SettingsIn, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Update platform-level settings; omitted fields are left unchanged.

    Args:
        data (SettingsIn): Optional new values for each setting.
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        if data.allow_registration is not None:
            set_setting(
                conn, "allow_registration", "1" if data.allow_registration else "0"
            )
        if data.ai_auto_propose is not None:
            set_setting(conn, "ai_auto_propose", "1" if data.ai_auto_propose else "0")
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit(
    limit: int = 100, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Recent audit-trail events.

    Args:
        limit (int): Maximum number of events (default 100).
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"events": [{"id", "actor", "action", "detail", ...}]}``.
    """
    with get_conn() as conn:
        return {"events": adm.list_audit(conn, limit)}


@router.get("/api/admin/reports")
async def admin_all_reports(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Every report in the system, across all owners.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"reports": [{"uid", "title", "owner", "created_at", ...}]}``.
    """
    with get_conn() as conn:
        return {"reports": rb.list_reports(conn, admin)}


@router.delete("/api/admin/reports/{uid}")
async def admin_report_delete(
    uid: str, admin_user: Dict[str, Any] = Depends(auth.require_admin)
):
    """Delete any report as an administrator (audited).

    Args:
        uid (str): Report uid.
        admin_user (Dict[str, Any]): Injected by the ``require_admin``
            dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        rb.delete_report(conn, uid, admin_user, admin_user["username"])
    return {"ok": True}
