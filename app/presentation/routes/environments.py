"""Browser (HTMX) pages for environments. The JSON API lives in api_environments.py; these
return HTML fragments and reuse the same use cases, so the two never drift."""
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.domain.exceptions import (
    BlueprintNotFoundError, BookingPermissionError, EnvironmentItemError,
    NamespaceUnavailableError, QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation.routes.api_environments import (
    _blueprint_repo, _derived_status, _env_repo, _namespace_repo, _order_use_case, _release_use_case,
)
from app.application.use_cases._permissions import can_manage
from app.application.use_cases.release_environment import EnvironmentNotFoundError
from app.presentation.templating import templates

router = APIRouter()


def _annotate(env):
    """Attach the derived aggregate status so templates can read env.derived_status."""
    env.derived_status = _derived_status(env)
    return env


async def _list_for(session, current_user):
    if current_user.role == "admin":
        envs = await _env_repo.list_all(session)
    else:
        envs = await _env_repo.list_by_user(session, str(current_user.id))
    return [_annotate(e) for e in envs]


@router.get("/environments", response_class=HTMLResponse)
async def environments_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    environments = await _list_for(session, current_user)
    blueprints = await _blueprint_repo.list_active(session)
    available_namespaces = await _namespace_repo.list_available(session)
    held_namespaces = await _namespace_repo.list_held_standalone_by_user(session, str(current_user.id))
    shared_namespaces = await _namespace_repo.list_shared_standalone_namespaces(session, current_user.id)
    return templates.TemplateResponse(
        request, "environments.html",
        {
            "environments": environments,
            "blueprints": blueprints,
            "available_namespaces": available_namespaces,
            "held_namespaces": held_namespaces,
            "shared_namespaces": shared_namespaces,
            "current_user": current_user,
            "active_nav": "environment",
        },
    )


def _order_error(
    request, current_user, blueprints, available_namespaces, held_namespaces, shared_namespaces,
    message: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/environment_order_form.html",
        {
            "blueprints": blueprints,
            "available_namespaces": available_namespaces,
            "held_namespaces": held_namespaces,
            "shared_namespaces": shared_namespaces,
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
        )
    except (BlueprintNotFoundError, EnvironmentItemError,
            QuotaExceededError, NamespaceUnavailableError, StaticVMUnavailableError) as exc:
        blueprints = await _blueprint_repo.list_active(session)
        available_namespaces = await _namespace_repo.list_available(session)
        held_namespaces = await _namespace_repo.list_held_standalone_by_user(session, str(current_user.id))
        shared_namespaces = await _namespace_repo.list_shared_standalone_namespaces(session, current_user.id)
        return _order_error(request, current_user, blueprints, available_namespaces, held_namespaces, shared_namespaces, str(exc))

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
    except ValueError:
        raise HTTPException(status_code=404, detail="Environment not found")
    if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the environment owner")
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user},
    )


@router.delete("/environments/{environment_id}", response_class=HTMLResponse)
async def release_environment(
    environment_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _release_use_case.execute(session, environment_id, current_user)
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return templates.TemplateResponse(
        request, "partials/environment_row.html",
        {"environment": _annotate(env), "current_user": current_user}, status_code=202,
    )
