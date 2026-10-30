"""HTTP routes for the pages area."""

from __future__ import annotations


from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from modules import auth
from modules import config as cfg

router = APIRouter()

# Jinja2 environment for the HTML pages served by this router.
templates = Jinja2Templates(directory=str(cfg.BASE_DIR / "templates"))


@router.get("/")
async def root():
    """Redirect the site root to the portal application.

    Returns:
        RedirectResponse: Redirect to ``/app``.
    """
    return RedirectResponse("/app")


@router.get("/login")
async def login_page(request: Request):
    """Render the user sign-in page, or bounce authenticated users to the app.

    Args:
        request (Request): Incoming request (used to read the session cookie).

    Returns:
        RedirectResponse | TemplateResponse: Redirect to ``/app`` for signed-in
        users, otherwise the rendered ``login.html`` page.
    """
    user = auth.optional_user(request)
    if user:
        return RedirectResponse("/app")
    return templates.TemplateResponse(
        request, "login.html", {"system_name": cfg.SYSTEM_NAME, "version": cfg.VERSION}
    )


@router.get("/app")
async def app_page(request: Request):
    """Render the main portal page for an authenticated user.

    Args:
        request (Request): Incoming request (used to read the session cookie).

    Returns:
        RedirectResponse | TemplateResponse: Redirect to ``/login`` when not
        signed in, otherwise the rendered ``app.html`` page.
    """
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


@router.get("/admin/login")
async def admin_login_page(request: Request):
    """Dedicated sign-in page for the administrator console (separate entry).

    Args:
        request (Request): Incoming request (used to read the session cookie).

    Returns:
        RedirectResponse | TemplateResponse: Redirect to ``/admin`` for a
        signed-in administrator, otherwise the rendered ``admin_login.html``.
    """
    user = auth.optional_user(request)
    if user and user.get("role") == "admin":
        return RedirectResponse("/admin")
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"system_name": cfg.SYSTEM_NAME, "version": cfg.VERSION},
    )


@router.get("/admin")
async def admin_page(request: Request):
    """Render the administrator console for an authenticated admin.

    Args:
        request (Request): Incoming request (used to read the session cookie).

    Returns:
        RedirectResponse | TemplateResponse: Redirect to ``/admin/login`` for
        anonymous callers, ``/app?forbidden=1`` for non-admins, otherwise the
        rendered ``admin.html`` page.
    """
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


@router.get("/health")
async def health():
    """Liveness probe for monitoring and container health checks.

    Returns:
        dict: ``{"status": "ok", "service": <system name>, "version": <version>}``.
    """
    return {"status": "ok", "service": cfg.SYSTEM_NAME, "version": cfg.VERSION}
