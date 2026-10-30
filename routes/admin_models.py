"""HTTP routes for the admin models area."""

from __future__ import annotations


from typing import Any
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel

from modules import auth
from modules import llm_client as llm
from modules.db import get_conn

router = APIRouter()


class ModelIn(BaseModel):
    """Request body for creating or updating a system model configuration.

    Attributes:
        name (str): Friendly name shown in the UI.
        base_url (str): OpenAI-compatible endpoint base URL.
        api_key (str): Credential; may be empty for local endpoints.
        model (str): Provider model identifier.
        temperature (float): Sampling temperature (default 0.3).
        max_tokens (int): Response token cap (default 1024).
        timeout (float): Request timeout in seconds (default 90).
        verify_ssl (bool): Whether TLS certificates are verified.
        enabled (bool): Whether users may select this model.
    """

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
    """Request body for testing a model configuration.

    Supply ``config_id`` to test a saved configuration, or the individual
    fields to test an unsaved one.

    Attributes:
        config_id (Optional[int]): Saved model id to test.
        name (Optional[str]): Display name for an unsaved configuration.
        base_url (Optional[str]): Endpoint to probe.
        api_key (Optional[str]): Credential for the probe.
        model (Optional[str]): Model identifier to probe.
        verify_ssl (Optional[bool]): TLS verification override.
    """

    config_id: Optional[int] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    verify_ssl: Optional[bool] = None


@router.get("/api/admin/models")
async def admin_models(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """List all registered system model configurations.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"models": [{"id", "name", "model", "is_default", ...}]}``.
    """
    with get_conn() as conn:
        return {"models": llm.list_configs(conn)}


@router.post("/api/admin/models")
async def admin_model_create(
    data: ModelIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Register a new system model configuration.

    Args:
        data (ModelIn): Endpoint, credential and generation settings.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"id": int}`` of the new configuration.
    """
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


@router.put("/api/admin/models/{mid}")
async def admin_model_update(
    mid: int, data: ModelIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Update an existing system model configuration.

    Args:
        mid (int): Configuration id.
        data (ModelIn): Replacement settings.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        llm.update_config(conn, admin["username"], mid, data.model_dump())
    return {"ok": True}


@router.delete("/api/admin/models/{mid}")
async def admin_model_delete(
    mid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Delete a system model configuration.

    Args:
        mid (int): Configuration id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        llm.delete_config(conn, admin["username"], mid)
    return {"ok": True}


@router.post("/api/admin/models/{mid}/default")
async def admin_model_default(
    mid: int, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Mark a system model configuration as the default.

    Args:
        mid (int): Configuration id.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": true}``.
    """
    with get_conn() as conn:
        llm.set_default(conn, admin["username"], mid)
    return {"ok": True}


@router.post("/api/admin/models/test")
async def admin_model_test(
    data: ModelTestIn, admin: Dict[str, Any] = Depends(auth.require_admin)
):
    """Probe a saved or unsaved model configuration.

    Always answers 200 with the verdict in ``ok``/``status`` so a negative
    result is not mistaken for a transport error.

    Args:
        data (ModelTestIn): Saved config id or ad-hoc connection settings.
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"ok": bool, "status": str, "hint": str, ...}`` from the probe.
    """
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


@router.get("/api/admin/user-models")
async def admin_user_models(admin: Dict[str, Any] = Depends(auth.require_admin)):
    """Oversight: every user-created model (keys masked).

    Lets support help users debug their own endpoints.

    Args:
        admin (Dict[str, Any]): Injected by the ``require_admin`` dependency.

    Returns:
        dict: ``{"models": [{"id", "name", "model", "base_url", ...}]}``.
    """
    with get_conn() as conn:
        return {"models": llm.list_all_user_configs(conn)}
