"""Unit tests for ShareNamespaceUseCase and RevokeNamespaceShareUseCase.

Use cases are tested with mocked repos and a fake session — no DB required.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.entities import Booking, NamespaceShare, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingNotFoundError,
    BookingPermissionError,
    NamespaceShareDuplicateError,
    NamespaceShareNotFoundError,
    NamespaceShareSelfError,
    NamespaceShareUserNotFoundError,
)

_NOW = datetime.now(timezone.utc)
_OWNER_ID = uuid4()
_OTHER_ID = uuid4()
_BOOKING_ID = uuid4()
_NS_ID = uuid4()
_SHARE_ID = uuid4()


def _user(uid=None, username="alice", role="user"):
    uid = uid or uuid4()
    return User(id=uid, username=username, password_hash="", role=role,
                is_active=True, created_at=_NOW)


def _booking(status=BookingStatus.READY, resource_type=ResourceType.NAMESPACE,
             owner_id=None, created_by=None):
    oid = owner_id or str(_OWNER_ID)
    return Booking(
        id=_BOOKING_ID, user_id=oid, status=status, resource_type=resource_type,
        ttl_minutes=120, expires_at=_NOW, created_at=_NOW, namespace_id=_NS_ID,
        namespace_name="dev1", cluster_name="prod", created_by=created_by,
    )


def _share(username="alice"):
    return NamespaceShare(
        id=_SHARE_ID, booking_id=_BOOKING_ID,
        shared_with_user_id=_OTHER_ID, shared_with_username=username,
        created_at=_NOW,
    )


def _make_share_uc(booking=None, share=None):
    booking_repo = MagicMock()
    if booking is None:
        booking = _booking()
    booking_repo.get = AsyncMock(return_value=booking)

    share_repo = MagicMock()
    share_repo.create = AsyncMock(return_value=share or _share())

    from app.application.use_cases.share_namespace import ShareNamespaceUseCase
    uc = ShareNamespaceUseCase(booking_repo, share_repo)
    return uc, booking_repo, share_repo


def _make_revoke_uc(booking=None):
    booking_repo = MagicMock()
    if booking is None:
        booking = _booking()
    booking_repo.get = AsyncMock(return_value=booking)

    share_repo = MagicMock()
    share_repo.delete = AsyncMock(return_value=True)

    from app.application.use_cases.revoke_namespace_share import RevokeNamespaceShareUseCase
    uc = RevokeNamespaceShareUseCase(booking_repo, share_repo)
    return uc, booking_repo, share_repo


def _fake_session():
    s = AsyncMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    return s


def _patch_user_repo(user=None):
    """Return a context-manager patch for the UserRepository used inside the share use case."""
    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=user)
    return patch(
        "app.infrastructure.repositories.user_repo.UserRepository",
        return_value=mock_repo,
    )


def _patch_user_repo_revoke(user=None):
    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=user)
    return patch(
        "app.infrastructure.repositories.user_repo.UserRepository",
        return_value=mock_repo,
    )


# ── ShareNamespaceUseCase ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_valid_creates_share():
    caller = _user(uid=_OWNER_ID, username="bob")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_share_uc()
    session = _fake_session()

    with _patch_user_repo(recipient):
        result = await uc.execute(session, _BOOKING_ID, "alice", caller)

    share_repo.create.assert_awaited_once_with(session, _BOOKING_ID, recipient.id)
    assert result.shared_with_username == "alice"


@pytest.mark.asyncio
async def test_share_self_raises_400():
    caller = _user(uid=_OWNER_ID, username="bob")
    # recipient is the same person as the caller
    recipient = _user(uid=_OWNER_ID, username="bob")
    uc, _, _ = _make_share_uc()
    session = _fake_session()

    with _patch_user_repo(recipient):
        with pytest.raises(NamespaceShareSelfError):
            await uc.execute(session, _BOOKING_ID, "bob", caller)


@pytest.mark.asyncio
async def test_share_unknown_user_raises_400():
    caller = _user(uid=_OWNER_ID, username="bob")
    uc, _, _ = _make_share_uc()
    session = _fake_session()

    with _patch_user_repo(None):  # user not found
        with pytest.raises(NamespaceShareUserNotFoundError):
            await uc.execute(session, _BOOKING_ID, "ghost", caller)


@pytest.mark.asyncio
async def test_share_non_namespace_booking_raises():
    caller = _user(uid=_OWNER_ID, username="bob")
    vm_booking = _booking(resource_type=ResourceType.VM)
    uc, _, _ = _make_share_uc(booking=vm_booking)
    session = _fake_session()
    recipient = _user(uid=_OTHER_ID, username="alice")

    with _patch_user_repo(recipient):
        with pytest.raises(ValueError, match="NAMESPACE"):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_share_released_booking_raises():
    caller = _user(uid=_OWNER_ID, username="bob")
    released = _booking(status=BookingStatus.RELEASED)
    uc, _, _ = _make_share_uc(booking=released)
    session = _fake_session()
    recipient = _user(uid=_OTHER_ID, username="alice")

    with _patch_user_repo(recipient):
        with pytest.raises(ValueError, match="RELEASED"):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_share_failed_booking_raises():
    caller = _user(uid=_OWNER_ID, username="bob")
    failed = _booking(status=BookingStatus.FAILED)
    uc, _, _ = _make_share_uc(booking=failed)
    session = _fake_session()
    recipient = _user(uid=_OTHER_ID, username="alice")

    with _patch_user_repo(recipient):
        with pytest.raises(ValueError, match="FAILED"):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_share_duplicate_raises_409():
    caller = _user(uid=_OWNER_ID, username="bob")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_share_uc()
    share_repo.create = AsyncMock(side_effect=IntegrityError("", "", Exception()))
    session = _fake_session()

    with _patch_user_repo(recipient):
        with pytest.raises(NamespaceShareDuplicateError):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_share_non_owner_raises_403():
    # caller is neither the owner, the creator, nor admin
    caller = _user(uid=uuid4(), username="carol", role="user")
    uc, _, _ = _make_share_uc()
    session = _fake_session()
    recipient = _user(uid=_OTHER_ID, username="alice")

    with _patch_user_repo(recipient):
        with pytest.raises(BookingPermissionError):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_share_admin_can_share_any():
    admin = _user(uid=uuid4(), username="admin", role="admin")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_share_uc()
    session = _fake_session()

    with _patch_user_repo(recipient):
        result = await uc.execute(session, _BOOKING_ID, "alice", admin)

    share_repo.create.assert_awaited_once()


# ── RevokeNamespaceShareUseCase ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_owner_succeeds():
    caller = _user(uid=_OWNER_ID, username="bob")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_revoke_uc()
    session = _fake_session()

    with _patch_user_repo_revoke(recipient):
        await uc.execute(session, _BOOKING_ID, "alice", caller)

    share_repo.delete.assert_awaited_once_with(session, _BOOKING_ID, recipient.id)


@pytest.mark.asyncio
async def test_revoke_non_owner_raises_403():
    caller = _user(uid=uuid4(), username="carol", role="user")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_revoke_uc()
    session = _fake_session()

    with _patch_user_repo_revoke(recipient):
        with pytest.raises(BookingPermissionError):
            await uc.execute(session, _BOOKING_ID, "alice", caller)

    share_repo.delete.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_nonexistent_share_raises_404():
    caller = _user(uid=_OWNER_ID, username="bob")
    recipient = _user(uid=_OTHER_ID, username="alice")
    uc, _, share_repo = _make_revoke_uc()
    share_repo.delete = AsyncMock(return_value=False)  # nothing deleted
    session = _fake_session()

    with _patch_user_repo_revoke(recipient):
        with pytest.raises(NamespaceShareNotFoundError):
            await uc.execute(session, _BOOKING_ID, "alice", caller)


@pytest.mark.asyncio
async def test_revoke_unknown_username_raises_404():
    caller = _user(uid=_OWNER_ID, username="bob")
    uc, _, share_repo = _make_revoke_uc()
    session = _fake_session()

    with _patch_user_repo_revoke(None):  # user not found
        with pytest.raises(NamespaceShareUserNotFoundError):
            await uc.execute(session, _BOOKING_ID, "ghost", caller)

    share_repo.delete.assert_not_called()
