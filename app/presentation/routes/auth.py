import json
import secrets
from uuid import UUID

import bcrypt
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.infrastructure.auth import require_admin, require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.user_repo import UserRepository
from app.domain.entities import User

router = APIRouter()
templates = Jinja2Templates(directory="app/presentation/templates")

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
