"""Browser (HTMX) pages for environments. The JSON API lives in api_environments.py; these
return HTML fragments and reuse the same use cases, so the two never drift."""
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import User
from app.domain.exceptions import (
    BlueprintNotFoundError, BookingPermissionError, EnvironmentError, EnvironmentItemError,
    EnvironmentNotFoundError, NamespaceUnavailableError, NotFoundError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation.middleware.correlation_id import get_request_id
from app.presentation.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.presentation.routes.api_environments import (
    _blueprint_repo, _derived_status, _env_repo, _namespace_repo, _order_use_case, _release_use_case,
    _update_name_use_case,
)
from app.application.use_cases._permissions import can_manage
from app.presentation.templating import templates

router = APIRouter()


def _annotate(env):
    """Attach the derived aggregate status so templates can read env.derived_status."""
    env.derived_status = _derived_status(env)
    return env


async def _list_for(
    session, current_user, *, filter: str = "mine", show_released: bool = False, label=None,
    after=None,
):
    """One keyset page of the environments list (#467) → (annotated envs, encoded next cursor).

    Fully released environments are excluded in SQL, before their children load (#466), and
    children are loaded only for the page's environments.
    """
    page = await _env_repo.list_page(
        session,
        user_id=None if filter == "all" else str(current_user.id),
        label=label, include_released=show_released,
        limit=settings.ENVIRONMENTS_PAGE_SIZE, after=after,
    )
    next_cursor = encode_cursor(page.next_cursor) if page.next_cursor else None
    return [_annotate(e) for e in page.items], next_cursor


def _list_context(filter: str, show_released: bool, label: str | None, next_cursor: str | None):
    """Template context shared by the page and the Load more fragment.

    The Load more URL echoes the filters in effect, so every page matches the first one (#467).
    """
    load_more_url = None
    if next_cursor:
        query = {"cursor": next_cursor, "filter": filter}
        if show_released:
            query["show_released"] = "1"
        if label:
            query["label"] = label
        load_more_url = f"/environments/rows?{urlencode(query)}"
    return {
        "active_filter": filter,
        "show_released": show_released,
        "label_filter": label,
        "load_more_url": load_more_url,
    }


@router.get("/environments", response_class=HTMLResponse)
async def environments_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # Always the first page — a bookmarked or pushed URL opens at the top (#467).
    environments, next_cursor = await _list_for(
        session, current_user, filter=filter, show_released=show_released, label=label,
    )
    blueprints = await _blueprint_repo.list_active(session)
    available_namespaces = await _namespace_repo.list_available(session)
    held_namespaces = await _namespace_repo.list_held_standalone_by_user(session, str(current_user.id))
    return templates.TemplateResponse(
        request, "environments.html",
        {
            "environments": environments,
            "blueprints": blueprints,
            "available_namespaces": available_namespaces,
            "held_namespaces": held_namespaces,
            "current_user": current_user,
            "active_nav": "environment",
            **_list_context(filter, show_released, label, next_cursor),
        },
    )


@router.get("/environments/rows", response_class=HTMLResponse, include_in_schema=False)
async def environment_rows_page(
    request: Request,
    cursor: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """The next page of rows for "Load more" (#467): rows + the next control, appended in place."""
    try:
        after = decode_cursor(cursor)
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    environments, next_cursor = await _list_for(
        session, current_user, filter=filter, show_released=show_released, label=label, after=after,
    )
    return templates.TemplateResponse(
        request, "partials/environment_rows_page.html",
        {
            "environments": environments,
            "current_user": current_user,
            **_list_context(filter, show_released, label, next_cursor),
        },
    )


def _order_error(
    request, current_user, blueprints, available_namespaces, held_namespaces, message: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/environment_order_form.html",
        {
            "blueprints": blueprints,
            "available_namespaces": available_namespaces,
            "held_namespaces": held_namespaces,
            "current_user": current_user,
            "order_error": message,
        },
        headers={"HX-Retarget": "#environment-order-form", "HX-Reswap": "outerHTML"},
    )


@router.post("/environments", response_class=HTMLResponse)
async def order_environment(
    request: Request,
    blueprint_name: str = Form(...),
    ttl_minutes: int = Form(...),
    namespace_id: UUID | None = Form(None),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _order_use_case.execute(
            session, blueprint_name, ttl_minutes, user_id=str(current_user.id),
            namespace_id=namespace_id,
            request_id=get_request_id(),
        )
    except (BlueprintNotFoundError, EnvironmentItemError,
            QuotaExceededError, NamespaceUnavailableError, StaticVMUnavailableError) as exc:
        blueprints = await _blueprint_repo.list_active(session)
        available_namespaces = await _namespace_repo.list_available(session)
        held_namespaces = await _namespace_repo.list_held_standalone_by_user(session, str(current_user.id))
        return _order_error(request, current_user, blueprints, available_namespaces, held_namespaces, str(exc))

    env.owner_username = current_user.username
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user}, status_code=201,
    )


@router.get("/environments/{environment_id}/row", response_class=HTMLResponse)
async def environment_row(
    environment_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _env_repo.get(session, environment_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the environment owner")
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user},
    )


@router.patch("/environments/{environment_id}/name", response_class=HTMLResponse)
async def update_environment_name(
    environment_id: UUID,
    request: Request,
    name: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _update_name_use_case.execute(session, environment_id, name, current_user)
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except EnvironmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user},
    )


_DISPATCH_ROLES = {"dispatcher", "admin"}


@router.delete("/environments/{environment_id}", response_class=HTMLResponse)
async def release_environment(
    environment_id: UUID,
    request: Request,
    on_behalf_of: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    force = False
    if on_behalf_of is not None:
        if current_user.role not in _DISPATCH_ROLES:
            raise HTTPException(status_code=403, detail="Only a dispatcher may act on behalf of another user")
        try:
            env = await _env_repo.get(session, environment_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="Environment not found")
        if env.owner_username != on_behalf_of:
            raise HTTPException(status_code=403, detail=f"Environment is not owned by '{on_behalf_of}'")
        force = True
    try:
        env = await _release_use_case.execute(
            session, environment_id, current_user, force=force, request_id=get_request_id(),
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user}, status_code=202,
    )
