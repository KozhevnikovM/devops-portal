"""Integration: a release racing the end of the terraform apply keeps teardown's status message (#444).

The provisioning lock is released right after the apply. A teardown that was waiting on it can then
move the booking to RELEASING and write its own progress (or even settle it) *before* the
provisioning task's step-boundary clear of the status message. That clear — like every
provisioning-owned status-message write — must only apply while provisioning still owns the booking,
checked atomically in the UPDATE, so it can't erase teardown's message.

The interleaving is forced deterministically: the mocked ``provisioning_lock.release`` performs the
teardown writes through the real repository, against real Postgres, at exactly that point.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.booking_status import TEARDOWN_PROGRESS_STATUSES
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import BookingAuditModel, BookingModel
from app.infrastructure.repositories import booking_repo as booking_repo_mod
from app.infrastructure.repositories.booking_repo import BookingRepository
from tests.integration._environment_lease import make_sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]


def _teardown_progress(Session, booking_id):
    repo = BookingRepository()
    with Session() as s:
        repo.sync_update_status(s, booking_id, BookingStatus.RELEASING)
    with Session() as s:
        repo.sync_append_progress(s, booking_id, "Destroying vApp…\n", "Destroying vApp…", TEARDOWN_PROGRESS_STATUSES)
    return BookingStatus.RELEASING, "Destroying vApp…"


def _teardown_failed(Session, booking_id):
    _teardown_progress(Session, booking_id)
    repo = BookingRepository()
    with Session() as s:
        repo.sync_set_status_message(s, booking_id, "Teardown failed — see audit log")
    with Session() as s:
        repo.sync_update_status(s, booking_id, BookingStatus.FAILED)
    return BookingStatus.FAILED, "Teardown failed — see audit log"


@pytest.mark.parametrize("teardown", [_teardown_progress, _teardown_failed])
async def test_release_after_lock_release_keeps_teardown_status_message(async_engine: AsyncEngine, teardown):
    Session = make_sessionmaker()
    booking_id = uuid4()
    with Session() as s:
        s.add(BookingModel(
            id=booking_id, user_id="inttest-owner", status="PENDING", resource_type="VM",
            ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc),
        ))
        s.commit()

    expected = {}
    lock = MagicMock()
    lock.release.side_effect = lambda client, bid: expected.update(result=teardown(Session, booking_id))

    async def apply(workspace_id, config, api_token=None, on_progress=None):
        for i in range(5):
            on_progress(f"tf line {i}")
        return {"ip": "10.0.0.7"}

    terraform = MagicMock()
    terraform.apply = apply
    teardown_task = MagicMock()
    try:
        with (
            patch.object(booking_repo_mod, "publish_row_changed", lambda **_: None),
            patch.object(booking_repo_mod, "publish_progress_changed", lambda **_: None),
            patch("app.tasks.provision.SyncSessionLocal", Session),
            patch("app.tasks.provision.image_repo", MagicMock()),
            patch("app.tasks.provision.hw_config_repo", MagicMock()),
            patch("app.tasks.provision.terraform", terraform),
            patch("app.tasks.provision.provisioning_lock", lock),
            patch("app.tasks.provision.teardown_vm_task", teardown_task),
            patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
            patch("app.tasks.provision.settings.VCD_API_TOKENS", ""),
            patch("app.tasks.provision.settings.VCD_API_TOKEN", ""),
        ):
            from app.tasks.provision import provision_vm_task
            # In a thread: the task calls asyncio.run(), which can't nest in this test's loop.
            await asyncio.to_thread(provision_vm_task.apply, args=[str(booking_id), str(uuid4()), str(uuid4())])

        status, message = expected["result"]
        with Session() as s:
            model = s.get(BookingModel, booking_id)
            assert model.status == status.value
            assert model.status_message == message       # not erased by provisioning's clear
            assert model.provisioning_log.startswith("tf line 0\n")  # provisioning output kept
            assert "Destroying vApp…\n" in model.provisioning_log
        # didn't configure; handed off to teardown only while it was still RELEASING
        assert teardown_task.delay.call_count == (1 if status == BookingStatus.RELEASING else 0)
        with Session() as s:
            statuses = [a.new_status for a in s.query(BookingAuditModel).filter_by(booking_id=booking_id)]
        assert "CONFIGURING" not in statuses and "READY" not in statuses
    finally:
        with Session() as s:
            s.execute(delete(BookingAuditModel).where(BookingAuditModel.booking_id == booking_id))
            s.execute(delete(BookingModel).where(BookingModel.id == booking_id))
            s.commit()


async def test_provisioning_owned_clear_still_applies_while_provisioning(async_engine: AsyncEngine, monkeypatch):
    from app.domain.booking_status import PROVISIONING_OWNED_STATUSES

    monkeypatch.setattr(booking_repo_mod, "publish_row_changed", lambda **_: None)
    Session = make_sessionmaker()
    booking_id = uuid4()
    with Session() as s:
        s.add(BookingModel(
            id=booking_id, user_id="inttest-owner", status="PROVISIONING", resource_type="VM",
            ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc),
            status_message="tf line 4",
        ))
        s.commit()
    try:
        with Session() as s:
            written = BookingRepository().sync_set_status_message(
                s, booking_id, None, if_status_in=PROVISIONING_OWNED_STATUSES,
            )
        assert written is True
        with Session() as s:
            assert s.get(BookingModel, booking_id).status_message is None
    finally:
        with Session() as s:
            s.execute(delete(BookingModel).where(BookingModel.id == booking_id))
            s.commit()
