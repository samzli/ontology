"""HTTP routes for the my models area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules import auth
from modules import llm_client as llm
from modules.db import get_conn
from modules.db import set_setting

from routes.common import _parse_model_pref
from routes.common import _user_model_pref

router = APIRouter()


class ModelSelectIn(BaseModel):
    """Request body for selecting the caller's model.

    ``config_id`` is ``"sys:<id>"`` (admin-registered) or ``"my:<id>"``
    (caller's own Model centre entry); ``null`` falls back to the system
    default.

    Attributes:
        config_id (Optional[str]): Prefixed model id, or ``None`` to reset.
    """

    # "sys:<id>" (admin-registered) or "my:<id>" (caller's own Model centre
    # entry); null → fall back to the system default.
    config_id: Optional[str] = None


@router.get("/api/models")
async def list_user_models(user: Dict[str, Any] = Depends(auth.current_user)):
    """Models the caller may use.

    Returns admin-registered (enabled) models plus their own Model centre
    entries — safe fields only — along with the current selection.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"models": [{"id": "sys:1"|"my:3", "name", "model",
        "is_default", "mine"}], "selected": str | None}``.
    """
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


@router.post("/api/models/select")
async def select_user_model(
    data: ModelSelectIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Remember the caller's model choice (per-user, reversible any time).

    Args:
        data (ModelSelectIn): Selected ``sys:``/``my:`` model id, or ``None`` to
            reset to the system default.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"selected": str | None, "name", "model"}``;
        422 JSON for an unparseable id, 404 JSON when the model is unavailable.
    """
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


class UserModelIn(BaseModel):
    """Request body for the caller's own model configuration.

    Attributes:
        name (str): Friendly name shown in the Model centre.
        base_url (str): OpenAI-compatible endpoint base URL.
        api_key (str): Credential; empty on update keeps the stored key.
        model (str): Provider model identifier.
        temperature (float): Sampling temperature (default 0.3).
        max_tokens (int): Response token cap (default 1024).
        timeout (float): Request timeout in seconds (default 90).
        verify_ssl (bool): Whether TLS certificates are verified.
        write_env (bool): Whether to persist the key into the local env file.
    """

    name: str
    base_url: str
    api_key: str = ""
    model: str
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: float = 90.0
    verify_ssl: bool = True
    write_env: bool = True


@router.get("/api/my/models")
async def my_models_list(user: Dict[str, Any] = Depends(auth.current_user)):
    """List the caller's own model configurations.

    Args:
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict: ``{"models": [...], "selected": str | None}`` (keys masked).
    """
    with get_conn() as conn:
        return {
            "models": llm.list_user_configs(conn, user["id"]),
            "selected": _user_model_pref(conn, user["id"]),
        }


@router.post("/api/my/models")
async def my_models_create(
    data: UserModelIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Save a new model configuration owned by the caller.

    Args:
        data (UserModelIn): Endpoint, credential and generation settings.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"id": int}``; error JSON with the model
        configuration's status code when the settings are rejected.
    """
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


@router.put("/api/my/models/{mid}")
async def my_models_update(
    mid: int,
    data: UserModelIn,
    user: Dict[str, Any] = Depends(auth.current_user),
):
    """Update one of the caller's model configurations.

    An empty ``api_key`` keeps the stored credential unchanged.

    Args:
        mid (int): Configuration id.
        data (UserModelIn): Replacement settings.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true}``; error JSON with the model
        configuration's status code when the update is rejected.
    """
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


@router.delete("/api/my/models/{mid}")
async def my_models_delete(mid: int, user: Dict[str, Any] = Depends(auth.current_user)):
    """Delete one of the caller's model configurations.

    When the deleted model was selected, the selection is reset.

    Args:
        mid (int): Configuration id.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": true}``; error JSON with the model
        configuration's status code when deletion is rejected.
    """
    with get_conn() as conn:
        try:
            llm.delete_user_config(conn, user["id"], mid)
        except llm.ModelConfigError as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.message})
        if _parse_model_pref(_user_model_pref(conn, user["id"])) == ("my", mid):
            set_setting(conn, f"user_model:{user['id']}", "")
    return {"ok": True}


class MyModelTestIn(BaseModel):
    """Request body for probing a user model configuration.

    Supply ``config_id`` (``"my:N"`` for a saved model) or the individual
    connection fields for an unsaved one.

    Attributes:
        config_id (Optional[str]): Saved model reference.
        name (Optional[str]): Display name for an unsaved configuration.
        base_url (Optional[str]): Endpoint to probe.
        api_key (Optional[str]): Credential for the probe.
        model (Optional[str]): Model identifier to probe.
        verify_ssl (Optional[bool]): TLS verification override.
    """

    config_id: Optional[str] = None  # "my:N" for a saved user model
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    verify_ssl: Optional[bool] = None


@router.post("/api/my/models/test")
async def my_models_test(
    data: MyModelTestIn, user: Dict[str, Any] = Depends(auth.current_user)
):
    """Probe a saved or unsaved user model configuration.

    Always answers 200 with the verdict in ``ok``; ``hint`` carries the next
    step when the connection fails.

    Args:
        data (MyModelTestIn): Saved model reference or ad-hoc settings.
        user (Dict[str, Any]): Injected by the ``current_user`` dependency.

    Returns:
        dict | JSONResponse: ``{"ok": bool, "status": str, "hint": str, ...}``;
        404 JSON when the saved model is missing, 422 JSON for a malformed id.
    """
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
