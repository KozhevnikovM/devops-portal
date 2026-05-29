import json
import secrets
from uuid import UUID
from zoneinfo import available_timezones

import bcrypt
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.infrastructure.auth import require_admin, require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.user_repo import UserRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository
from app.domain.entities import User
from app.presentation.templating import templates

router = APIRouter()

_quota_repo = QuotaRepository()

_user_repo = UserRepository()


def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


# ── Login / Logout ────────────────────────────────────────────────────────────

@router.get("/auth/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/auth/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
):
    user = await _user_repo.get_by_username(session, username)
    if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid username or password"},
            status_code=401,
        )

    session_id = secrets.token_hex(32)
    r = _get_redis()
    await r.setex(
        f"session:{session_id}",
        settings.SESSION_TTL,
        json.dumps({"user_id": str(user.id), "username": user.username, "role": user.role}),
    )
    await r.aclose()

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        "session_id", session_id,
        max_age=settings.SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        r = _get_redis()
        await r.delete(f"session:{session_id}")
        await r.aclose()

    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie("session_id")
    return response


# ── User management ───────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool


@router.get("/api/users", response_model=list[UserResponse])
async def list_users(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    users = await _user_repo.list_all(session)
    return [UserResponse(id=u.id, username=u.username, role=u.role, is_active=u.is_active) for u in users]


@router.post("/api/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = await _user_repo.create(session, body.username, pw_hash, body.role)
    return UserResponse(id=user.id, username=user.username, role=user.role, is_active=user.is_active)


# ── API key management ────────────────────────────────────────────────────────

class APIKeyCreate(BaseModel):
    description: str | None = None


class APIKeyResponse(BaseModel):
    id: UUID
    description: str | None
    is_active: bool


@router.post("/api/users/{user_id}/api-keys")
async def create_api_key(
    user_id: UUID,
    body: APIKeyCreate,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if str(current_user.id) != str(user_id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    raw_key, api_key = await _user_repo.create_api_key(session, user_id, body.description)
    return JSONResponse(
        {"id": str(api_key.id), "key": raw_key, "description": api_key.description},
        status_code=201,
    )


@router.delete("/api/users/{user_id}/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    user_id: UUID,
    key_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if str(current_user.id) != str(user_id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    revoked = await _user_repo.revoke_api_key(session, user_id, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found")


# ── Admin UI ─────────────────────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    users = await _user_repo.list_all(session)
    return templates.TemplateResponse(
        request, "admin/users.html",
        {"users": users, "current_user": current_user},
    )


@router.post("/admin/users", response_class=HTMLResponse)
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        await _user_repo.create(session, username, pw_hash, role)
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Username "{username}" is already taken.</span>'
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#user-create-error", "HX-Reswap": "innerHTML"},
        )
    users = await _user_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/user_table.html",
        {"users": users, "current_user": current_user},
    )


@router.delete("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_delete_user(
    request: Request,
    user_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    if str(current_user.id) == str(user_id):
        raise HTTPException(status_code=409, detail="Cannot delete your own account")

    all_users = await _user_repo.list_all(session)
    target = next((u for u in all_users if str(u.id) == str(user_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    admins = [u for u in all_users if u.role == "admin"]
    if target.role == "admin" and len(admins) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last admin account")

    await _user_repo.delete(session, user_id)

    users = await _user_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/user_table.html",
        {"users": users, "current_user": current_user},
    )


# ── User profile ──────────────────────────────────────────────────────────────

_TIMEZONES = sorted(available_timezones())


@router.get("/profile", response_class=HTMLResponse)
async def profile_form(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    saved = request.query_params.get("saved") == "1"
    api_keys = await _user_repo.list_api_keys(session, current_user.id)
    return templates.TemplateResponse(
        request, "profile.html",
        {"current_user": current_user, "timezones": _TIMEZONES, "saved": saved, "api_keys": api_keys},
    )


@router.post("/profile")
async def profile_save(
    request: Request,
    timezone: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if timezone not in available_timezones():
        api_keys = await _user_repo.list_api_keys(session, current_user.id)
        return templates.TemplateResponse(
            request, "profile.html",
            {"current_user": current_user, "timezones": _TIMEZONES, "saved": False,
             "error": "Invalid timezone", "api_keys": api_keys},
            status_code=400,
        )
    await _user_repo.update_timezone(session, current_user.id, timezone)
    return RedirectResponse(url="/profile?saved=1", status_code=302)


# ── Quota management ──────────────────────────────────────────────────────────

class QuotaUpdate(BaseModel):
    max_cpus: int | None = None
    max_memory_gb: int | None = None
    max_ssd_gb: int | None = None
    max_hdd_gb: int | None = None


@router.patch("/api/users/{user_id}/quota")
async def set_user_quota(
    user_id: UUID,
    body: QuotaUpdate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    current = await _quota_repo.get_limits_for_update(session, str(user_id))
    quota = await _quota_repo.set(
        session,
        user_id=user_id,
        max_cpus=      body.max_cpus      if body.max_cpus      is not None else current["max_cpus"],
        max_memory_gb= body.max_memory_gb if body.max_memory_gb is not None else current["max_memory_gb"],
        max_ssd_gb=    body.max_ssd_gb    if body.max_ssd_gb    is not None else current["max_ssd_gb"],
        max_hdd_gb=    body.max_hdd_gb    if body.max_hdd_gb    is not None else current["max_hdd_gb"],
    )
    return JSONResponse({
        "user_id":       str(user_id),
        "max_cpus":      quota.max_cpus,
        "max_memory_gb": quota.max_memory_gb,
        "max_ssd_gb":    quota.max_ssd_gb,
        "max_hdd_gb":    quota.max_hdd_gb,
    })


@router.post("/profile/api-keys", response_class=HTMLResponse)
async def create_profile_api_key(
    request: Request,
    description: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    raw_key, api_key = await _user_repo.create_api_key(
        session, current_user.id, description.strip() or None
    )
    api_keys = await _user_repo.list_api_keys(session, current_user.id)
    return templates.TemplateResponse(
        request, "partials/api_key_list.html",
        {"api_keys": api_keys, "new_key": raw_key, "current_user": current_user},
    )


@router.delete("/profile/api-keys/{key_id}", response_class=HTMLResponse)
async def revoke_profile_api_key(
    request: Request,
    key_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    revoked = await _user_repo.revoke_api_key(session, current_user.id, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found")
    api_keys = await _user_repo.list_api_keys(session, current_user.id)
    return templates.TemplateResponse(
        request, "partials/api_key_list.html",
        {"api_keys": api_keys, "current_user": current_user},
    )
