from urllib.parse import urlencode
from uuid import UUID

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases._permissions import can_manage, can_view_credentials
from app.application.use_cases._roles import resolve_config_roles
from app.config import settings
from app.domain.entities import User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError, BookingNotFoundError, NamespaceUnavailableError, BookingPermissionError,
    QuotaExceededError, RoleNotFoundError, StaticVMUnavailableError,
)
from app.domain.validation import validate_var_names
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation import deps
from app.presentation.middleware.correlation_id import get_request_id
from app.presentation.routes._queue import attach_queue_positions
from app.presentation.pagination import (
    InvalidCursorError, decode_cursor, encode_cursor, filter_params,
)
from app.presentation.reconcile import (
    InvalidReconcileRequestError, has_newer, parse_reconcile_request, reconcile_poller_context,
    row_version,
)
from app.presentation.templating import templates

router = APIRouter()

# Shared singletons from the composition root. Names kept so existing patches still target them.
_repo = deps.booking_repo
_image_repo = deps.image_repo
_hw_config_repo = deps.hw_config_repo
_namespace_repo = deps.namespace_repo
_role_repo = deps.role_repo
_static_vm_repo = deps.static_vm_repo
_dispatcher = deps.dispatcher
_use_case = deps.create_booking_uc
_extend_use_case = deps.extend_booking_uc
_update_label_use_case = deps.update_booking_label_uc
_release_use_case = deps.release_booking_uc
_book_namespace_use_case = deps.book_namespace_uc
_reserve_static_vm_use_case = deps.reserve_static_vm_uc


def _parse_vars_yaml(raw: str) -> dict:
    """Parse the booking form's Ansible-variables YAML textarea.

    Only checked for blankness with .strip() — the raw text itself is parsed unmodified, since
    stripping it would truncate the trailing newline of a multi-line block-scalar value when
    it's the last field in the textarea (#349). Raises ValueError on bad YAML, a non-mapping,
    or an invalid variable name.
    """
    raw = raw or ""
    if not raw.strip():
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"vars must be valid YAML: {exc}")
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("vars must be a YAML mapping (key: value pairs)")
    validate_var_names(parsed)
    return parsed

# Resource types listed on each booking page.
_VM_PAGE_TYPES = [ResourceType.VM.value, ResourceType.STATIC_VM.value]
_NAMESPACE_PAGE_TYPES = [ResourceType.NAMESPACE.value]


async def _list_page(
    session, current_user, *, resource_types, page_path, filter, show_released, label, after=None,
):
    """One keyset page of a bookings list (#479) → template context for its rows and Load more.

    Shared by the page (first page) and the Load more fragment (later pages), so both apply the
    same visibility and filters. Queue positions are read for the page's rows only, in one statement.
    """
    page = await _repo.list_page(
        session,
        user_id=None if filter == "all" else str(current_user.id),
        resource_types=resource_types, label=label, include_released=show_released,
        limit=settings.BOOKINGS_PAGE_SIZE, scan_size=settings.BOOKINGS_LABEL_SCAN_SIZE, after=after,
    )
    await attach_queue_positions(session, _repo, page.items)
    # The Load more URL echoes the filters in effect, so every page matches the first one.
    load_more_url = None
    if page.next_cursor:
        query = {"cursor": encode_cursor(page.next_cursor), **filter_params(filter, show_released, label)}
        load_more_url = f"{page_path}/rows?{urlencode(query)}"
    return {
        "bookings": page.items,
        "current_user": current_user,
        "active_filter": filter,
        "show_released": show_released,
        "label_filter": label,
        "load_more_url": load_more_url,
        # A short page with a next page only happens when a label scan ran out (#485): say that
        # the next step searches older bookings, rather than promising more rows.
        "searches_older": (
            page.next_cursor is not None and len(page.items) < settings.BOOKINGS_PAGE_SIZE
        ),
    }


async def _list_section_context(
    session, current_user, *, booking_type, page_path, filter, show_released, label,
):
    """Template context of a bookings list section: the first page under the given filters.

    Shared by the page and the list-section fragment (#494), so both render the same section.
    """
    # The VM page lists both provisioned and static VMs; other pages list their one type.
    # Always the first page — a bookmarked or pushed URL opens at the top (#479).
    list_context = await _list_page(
        session, current_user,
        resource_types=_VM_PAGE_TYPES if booking_type == "VM" else _NAMESPACE_PAGE_TYPES,
        page_path=page_path, filter=filter, show_released=show_released, label=label,
    )
    return {
        "booking_type": booking_type, "page_path": page_path, **list_context,
        **new_rows_context(page_path=page_path, filter=filter, show_released=show_released,
                           label=label),
        **reconcile_poller_context(f"{page_path}/reconcile", filter, show_released, label),
    }


async def _render_bookings_page(
    request, session, current_user, *, booking_type, page_path, active_nav, filter, show_released,
    label=None,
):
    list_context = await _list_section_context(
        session, current_user, booking_type=booking_type, page_path=page_path,
        filter=filter, show_released=show_released, label=label,
    )
    vm_images = await _image_repo.list_active(session)
    hw_configs = await _hw_config_repo.list_active(session)
    available_namespaces = await _namespace_repo.list_available(session)
    available_static_vms = await _static_vm_repo.list_available(session)
    # F-6 (#377): the role picker is admin-only — non-admins get an empty list so the
    # template's `{% if roles %}` gate hides it. The vars textarea (#377 follow-up) is gated
    # independently in the template on `current_user.role`, so it stays visible to admins
    # even when the role catalog itself is empty.
    roles = await _role_repo.list_active(session) if current_user.role == "admin" else []
    return templates.TemplateResponse(
        request, "index.html",
        {
            "vm_images": vm_images,
            "hw_configs": hw_configs,
            "available_namespaces": available_namespaces,
            "available_static_vms": available_static_vms,
            "roles": roles,
            "active_nav": active_nav,
            **list_context,
        },
    )


async def _render_list_section(
    request, session, current_user, *, booking_type, page_path, filter, show_released, label,
):
    """The list section alone, for a filter change (#494): no form, no catalog reads.

    HX-Push-Url records the page URL for these filters, never this fragment's own URL, so reload,
    bookmarks and Back/Forward always reach the full page. It is query-only, so the browser keeps
    the page's path and any reverse-proxy subpath prefix, which a header escapes (nginx rewrites
    only bodies and Location).
    """
    list_context = await _list_section_context(
        session, current_user, booking_type=booking_type, page_path=page_path,
        filter=filter, show_released=show_released, label=label,
    )
    return templates.TemplateResponse(
        request, "partials/booking_list_section.html", list_context,
        headers={"HX-Push-Url": f"?{urlencode(filter_params(filter, show_released, label))}"},
    )


async def _render_rows_page(
    request, session, current_user, *, resource_types, page_path, cursor, filter, show_released,
    label,
):
    """The next page of rows for "Load more" (#479): rows + the next control, appended in place."""
    try:
        after = decode_cursor(cursor)
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    list_context = await _list_page(
        session, current_user, resource_types=resource_types, page_path=page_path,
        filter=filter, show_released=show_released, label=label, after=after,
    )
    return templates.TemplateResponse(request, "partials/booking_rows_page.html", list_context)


def new_rows_context(*, page_path: str, filter, show_released, label, show: bool = False) -> dict:
    """Template context of a bookings section's newer-rows indicator (#497)."""
    return {
        "new_rows_id": "bookings-new-rows", "section_id": "bookings-section", "colspan": 9,
        "noun": "bookings", "show_new_rows": show,
        "new_rows_url": f"{page_path}/list?{urlencode(filter_params(filter, show_released, label))}",
    }


async def _render_reconcile(
    request, session, current_user, *, resource_types, page_path, rows, newest, filter,
    show_released, label,
):
    """Page row reconciliation for a bookings list (#497): out-of-band updates of the requested
    rows that changed, removals for those no longer visible here, and the newer-rows indicator.

    The request is validated before anything is read. Then a fixed number of statements, whatever
    the batch size: one scoped batch read (authorization is the query — the page's kinds and, for
    Mine, the owner/creator rule), at most one queue-rank read, and one keys-only newest probe.
    """
    try:
        req = parse_reconcile_request(rows, newest, max_ids=settings.RECONCILE_MAX_IDS)
    except InvalidReconcileRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user_id = None if filter == "all" else str(current_user.id)
    items = await _repo.list_items_by_ids(
        session, list(req.versions), user_id=user_id, resource_types=resource_types,
    )
    await attach_queue_positions(session, _repo, items)
    probe = await _repo.newest_key(
        session, user_id=user_id, resource_types=resource_types, label=label,
        include_released=show_released, scan_size=settings.BOOKINGS_LABEL_SCAN_SIZE,
    )
    visible = {b.id for b in items}
    return templates.TemplateResponse(request, "partials/booking_reconcile.html", {
        "current_user": current_user,
        "changed": [b for b in items if row_version(b) != req.versions[b.id]],
        "removed": [row_id for row_id in req.versions if row_id not in visible],
        **new_rows_context(page_path=page_path, filter=filter, show_released=show_released,
                           label=label, show=has_newer(probe, req.newest)),
    })


@router.get("/", response_class=HTMLResponse)
@router.get("/book/vm", response_class=HTMLResponse)
async def vm_bookings_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_bookings_page(
        request, session, current_user,
        booking_type="VM", page_path="/book/vm", active_nav="vm",
        filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/vm/list", response_class=HTMLResponse, include_in_schema=False)
async def vm_booking_list_section(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_list_section(
        request, session, current_user, booking_type="VM", page_path="/book/vm",
        filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/vm/rows", response_class=HTMLResponse, include_in_schema=False)
async def vm_booking_rows_page(
    request: Request,
    cursor: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_rows_page(
        request, session, current_user, resource_types=_VM_PAGE_TYPES, page_path="/book/vm",
        cursor=cursor, filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/vm/reconcile", response_class=HTMLResponse, include_in_schema=False)
async def vm_booking_reconcile(
    request: Request,
    r: list[str] = Query(default=[]),
    newest: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_reconcile(
        request, session, current_user, resource_types=_VM_PAGE_TYPES, page_path="/book/vm",
        rows=r, newest=newest, filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/namespace", response_class=HTMLResponse)
async def namespace_bookings_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_bookings_page(
        request, session, current_user,
        booking_type="NAMESPACE", page_path="/book/namespace", active_nav="namespace",
        filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/namespace/list", response_class=HTMLResponse, include_in_schema=False)
async def namespace_booking_list_section(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_list_section(
        request, session, current_user, booking_type="NAMESPACE", page_path="/book/namespace",
        filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/namespace/rows", response_class=HTMLResponse, include_in_schema=False)
async def namespace_booking_rows_page(
    request: Request,
    cursor: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_rows_page(
        request, session, current_user, resource_types=_NAMESPACE_PAGE_TYPES,
        page_path="/book/namespace",
        cursor=cursor, filter=filter, show_released=show_released, label=label,
    )


@router.get("/book/namespace/reconcile", response_class=HTMLResponse, include_in_schema=False)
async def namespace_booking_reconcile(
    request: Request,
    r: list[str] = Query(default=[]),
    newest: str | None = None,
    filter: str = "mine",
    show_released: bool = False,
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_reconcile(
        request, session, current_user, resource_types=_NAMESPACE_PAGE_TYPES,
        page_path="/book/namespace",
        rows=r, newest=newest, filter=filter, show_released=show_released, label=label,
    )


async def _render_form_error(request, session, current_user, booking_type="VM", **errors):
    """Re-render the booking form with an error banner (HTMX swaps the form area)."""
    ctx = {
        "vm_images": await _image_repo.list_active(session),
        "hw_configs": await _hw_config_repo.list_active(session),
        "available_namespaces": await _namespace_repo.list_available(session),
        "available_static_vms": await _static_vm_repo.list_available(session),
        # F-6 (#377) + follow-up: admin-only picker/vars — see _render_bookings_page.
        "roles": await _role_repo.list_active(session) if current_user.role == "admin" else [],
        "current_user": current_user,
        "booking_type": booking_type,
    }
    ctx.update(errors)
    return templates.TemplateResponse(
        request, "partials/booking_form.html", ctx,
        headers={"HX-Retarget": "#booking-form-area", "HX-Reswap": "outerHTML"},
    )


@router.post("/bookings", response_class=HTMLResponse)
async def create_booking(
    request: Request,
    ttl_minutes: int = Form(...),
    resource_type: str = Form("VM"),
    label: str = Form(""),
    image_id: UUID | None = Form(None),
    hw_config_id: UUID | None = Form(None),
    namespace_id: UUID | None = Form(None),
    static_vm_id: UUID | None = Form(None),
    roles: list[str] = Form([]),
    vars_yaml: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # ── Namespace booking — reserve from the pool (pick-specific or any), else queue ──
    if resource_type == ResourceType.NAMESPACE.value:
        try:
            booking = await _book_namespace_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), namespace_id=namespace_id,
                label=label.strip() or None,
            )
        except NamespaceUnavailableError as exc:
            return await _render_form_error(
                request, session, current_user, booking_type="NAMESPACE", namespace_error=str(exc)
            )

    # ── Static VM booking — reserve from the pool, no provisioning ──
    elif resource_type == ResourceType.STATIC_VM.value:
        try:
            booking = await _reserve_static_vm_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), static_vm_id=static_vm_id,
                label=label.strip() or None,
            )
        except StaticVMUnavailableError as exc:
            return await _render_form_error(
                request, session, current_user, static_vm_error=str(exc)
            )

    # ── VM booking — existing provisioning flow ──
    else:
        if image_id is None or hw_config_id is None:
            return await _render_form_error(
                request, session, current_user, quota_error="Select an image and hardware config",
            )
        # F-6 (#377) + follow-up: defense in depth — the roles/vars picker is hidden from
        # non-admins client-side, but ignore any `roles`/`vars_yaml` form data submitted anyway
        # (e.g. a direct POST bypassing the UI).
        if current_user.role != "admin":
            roles = []
            vars_yaml = ""
        try:
            config_roles = await resolve_config_roles(session, _role_repo, roles)
        except RoleNotFoundError as exc:
            return await _render_form_error(request, session, current_user, role_error=str(exc))
        try:
            extra_vars = _parse_vars_yaml(vars_yaml)
        except ValueError as exc:
            return await _render_form_error(
                request, session, current_user, vars_error=str(exc), vars_yaml=vars_yaml,
            )
        try:
            booking = await _use_case.execute(
                session, ttl_minutes, image_id, hw_config_id,
                user_id=str(current_user.id), label=label.strip() or None,
                config_roles=config_roles, extra_vars=extra_vars,
                request_id=get_request_id(),
            )
        except QuotaExceededError as exc:
            return await _render_form_error(request, session, current_user, quota_error=str(exc))

    booking.owner_username = current_user.username
    await attach_queue_positions(session, _repo, [booking])
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}, status_code=201
    )


@router.get("/bookings/{booking_id}/row", response_class=HTMLResponse)
async def booking_row(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")

    await attach_queue_positions(session, _repo, [booking])
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.get("/bookings/{booking_id}/credentials", response_class=HTMLResponse)
async def booking_credentials(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """A booking's credentials, revealed on explicit request (#478) — never embedded in a row.

    Owner or admin only (not the creating dispatcher). Authorization is checked before status, so a
    refused caller cannot tell a READY booking from any other.
    """
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_view_credentials(owner_id=booking.user_id, user=current_user):
        raise HTTPException(status_code=403, detail="Not allowed to view these credentials")
    if booking.status != BookingStatus.READY:
        raise HTTPException(status_code=409, detail="Credentials are available only while the booking is READY")

    return templates.TemplateResponse(
        request, "partials/booking_credentials.html", {"booking": booking},
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/bookings/{booking_id}", status_code=202, response_class=HTMLResponse)
async def release_booking(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _release_use_case.execute(
            session, booking_id, current_user, request_id=get_request_id(),
        )
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}, status_code=202
    )


@router.put("/bookings/{booking_id}/extend", response_class=HTMLResponse)
async def extend_booking(
    booking_id: UUID,
    request: Request,
    extend_minutes: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _extend_use_case.execute(session, booking_id, extend_minutes, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.patch("/bookings/{booking_id}/label", response_class=HTMLResponse)
async def update_booking_label(
    booking_id: UUID,
    request: Request,
    label: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _update_label_use_case.execute(session, booking_id, label, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # A label can be edited while QUEUED: keep the row's position (#495).
    await attach_queue_positions(session, _repo, [booking])
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.get("/bookings/{booking_id}/audit", response_class=HTMLResponse)
async def booking_audit_page(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Human-readable audit timeline for a booking (linked from a failed booking row)."""
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")

    entries = await _repo.list_audit(session, booking_id)
    return templates.TemplateResponse(
        request, "audit_log.html",
        {"booking": booking, "entries": entries, "current_user": current_user},
    )


@router.get("/bookings/{booking_id}/log", response_class=HTMLResponse)
async def booking_log_page(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Full accumulated Terraform/Ansible provisioning (and teardown) log for a booking (#378)."""
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")

    return templates.TemplateResponse(
        request, "booking_log.html",
        {"booking": booking, "current_user": current_user},
    )
