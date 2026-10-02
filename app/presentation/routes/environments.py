"""Browser (HTMX) pages for environments. The JSON API lives in api_environments.py; these
return HTML fragments and reuse the same use cases, so the two never drift."""
import logging
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
from app.presentation.pagination import (
    InvalidCursorError, decode_cursor, encode_cursor, filter_params,
)
from app.presentation.reconcile import (
    InvalidReconcileRequestError, environment_row_version, has_newer, parse_reconcile_request,
    reconcile_poller_context,
)
from app.presentation.routes.api_environments import (
    _blueprint_repo, _derived_status, _env_repo, _namespace_repo, _order_use_case, _release_use_case,
    _update_name_use_case,
)
from app.application.use_cases._permissions import can_manage
from app.presentation.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


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
        query = {"cursor": next_cursor, **filter_params(filter, show_released, label)}
        load_more_url = f"/environments/rows?{urlencode(query)}"
    return {
        "active_filter": filter,
        "show_released": show_released,
        "label_filter": label,
        "load_more_url": load_more_url,
    }


async def _list_section_context(
    session, current_user, *, filter: str, show_released: bool, label: str | None,
):
    """Template context of the environments list section: the first page under the given filters.

    Shared by the page and the list-section fragment (#494), so both render the same section.
    """
    # Always the first page — a bookmarked or pushed URL opens at the top (#467).
    environments, next_cursor = await _list_for(
        session, current_user, filter=filter, show_released=show_released, label=label,
    )
    return {
        "environments": environments,
        "current_user": current_user,
        **_list_context(filter, show_released, label, next_cursor),
        **new_rows_context(filter=filter, show_released=show_released, label=label),
        **reconcile_poller_context("/environments/reconcile", filter, show_released, label),
    }


def new_rows_context(*, filter, show_released, label, show: bool = False) -> dict:
    """Template context of the environments section's newer-rows indicator (#497)."""
    return {
        "new_rows_id": "environments-new-rows", "section_id": "environments-section", "colspan": 6,
        "noun": "environments", "show_new_rows": show,
        "new_rows_url": f"/environments/list?{urlencode(filter_params(filter, show_released, label))}",
    }


def _child_limit(request: Request) -> int:
    """The effective environment child limit computed at startup (#497 D3a)."""
    return getattr(request.app.state, "environment_child_limit", settings.ENVIRONMENT_MAX_CHILDREN)


@router.get("/environments", response_class=HTMLResponse)
async def environments_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    list_context = await _list_section_context(
        session, current_user, filter=filter, show_released=show_released, label=label,
    )
    blueprints = await _blueprint_repo.list_active(session)
    available_namespaces = await _namespace_repo.list_available(session)
    held_namespaces = await _namespace_repo.list_held_standalone_by_user(session, str(current_user.id))
    return templates.TemplateResponse(
        request, "environments.html",
        {
            "blueprints": blueprints,
            "available_namespaces": available_namespaces,
            "held_namespaces": held_namespaces,
            "active_nav": "environment",
            **list_context,
        },
    )


@router.get("/environments/list", response_class=HTMLResponse, include_in_schema=False)
async def environment_list_section(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """The list section alone, for a filter change (#494): no order form, no catalog reads.

    HX-Push-Url records the page URL for these filters, never this fragment's own URL. It is
    query-only, so the browser keeps the page's path and any reverse-proxy subpath prefix.
    """
    list_context = await _list_section_context(
        session, current_user, filter=filter, show_released=show_released, label=label,
    )
    return templates.TemplateResponse(
        request, "partials/environment_list_section.html", list_context,
        headers={"HX-Push-Url": f"?{urlencode(filter_params(filter, show_released, label))}"},
    )


@router.get("/environments/reconcile", response_class=HTMLResponse, include_in_schema=False)
async def environment_reconcile(
    request: Request,
    r: list[str] = Query(default=[]),
    newest: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Page row reconciliation for the environments list (#497) — see bookings._render_reconcile.

    Children come from the bounded child read (at most the effective child limit + 1 each). An
    environment over the limit breaks an invariant the write paths enforce, so only a direct
    database edit produces one: it fails closed with a "reload required" row and nothing more is
    read for it, so the request stays within its statement and child bounds.
    """
    try:
        req = parse_reconcile_request(r, newest, max_ids=settings.RECONCILE_MAX_IDS)
    except InvalidReconcileRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user_id = None if filter == "all" else str(current_user.id)
    items, over_limit = await _env_repo.list_items_by_ids(
        session, list(req.versions), user_id=user_id, child_limit=_child_limit(request),
    )
    probe = await _env_repo.newest_key(
        session, user_id=user_id, label=label, include_released=show_released,
    )
    for env in over_limit:
        logger.error(
            "environment %s has more children than the effective child limit (%d); "
            "not reconciled — the child-limit invariant was broken outside the application",
            env.id, _child_limit(request),
        )
    visible = {e.id for e in items} | {e.id for e in over_limit}
    return templates.TemplateResponse(request, "partials/environment_reconcile.html", {
        "current_user": current_user,
        "changed": [e for e in map(_annotate, items) if environment_row_version(e) != req.versions[e.id]],
        "over_limit": over_limit,
        "removed": [row_id for row_id in req.versions if row_id not in visible],
        **new_rows_context(filter=filter, show_released=show_released, label=label,
                           show=has_newer(probe, req.newest)),
    })


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
