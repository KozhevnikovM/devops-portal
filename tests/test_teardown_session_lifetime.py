"""Regression tests for #294: teardown held one DB connection across the full terraform destroy."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
from uuid import uuid4

import pytest

from app.domain.enums import BookingStatus, ResourceType
from app.tasks import teardown as td


def _make_booking(resource_type=ResourceType.VM, vm_password="secret"):
    b = MagicMock()
    b.id = uuid4()
    b.resource_type = resource_type
    b.image_id = uuid4()
    b.hw_config_id = uuid4()
    b.vm_password = vm_password
    return b


def _make_image():
    img = MagicMock()
    img.vapp_template_id = "tpl-001"
    return img


def _make_hw():
    hw = MagicMock()
    hw.cpus = 2
    hw.memory_mb = 2048
    hw.disk_mb = 20480
    return hw


def _mock_session_factory(booking, image, hw):
    """Return a context-manager factory that serves canned objects via sync_* calls."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    repo_mock = MagicMock()
    repo_mock.sync_get.return_value = booking
    image_repo_mock = MagicMock()
    image_repo_mock.sync_get.return_value = image
    hw_repo_mock = MagicMock()
    hw_repo_mock.sync_get.return_value = hw
    return session, repo_mock, image_repo_mock, hw_repo_mock


# ── Core: session is short-lived, not held across destroy ────────────────────


def test_session_opened_multiple_times_not_once(monkeypatch):
    """_run() must open a new session per DB call — not one session for the whole task."""
    booking_id = str(uuid4())
    booking = _make_booking()
    image = _make_image()
    hw = _make_hw()

    open_count = 0

    class _FakeSession:
        def __enter__(self):
            return MagicMock(
                sync_get=MagicMock(return_value=booking),
            )
        def __exit__(self, *a):
            return False

    sessions_opened = []

    def fake_session_local():
        sessions_opened.append(1)
        sess = MagicMock()
        sess.__enter__ = MagicMock(return_value=sess)
        sess.__exit__ = MagicMock(return_value=False)
        return sess

    monkeypatch.setattr(td, "SyncSessionLocal", fake_session_local)

    # Patch repo/image/hw at the module level so _run() lambdas resolve them.
    monkeypatch.setattr(td.repo, "sync_get", MagicMock(return_value=booking))
    monkeypatch.setattr(td.repo, "sync_update_status", MagicMock())
    monkeypatch.setattr(td.repo, "sync_set_status_message", MagicMock())
    monkeypatch.setattr(td.image_repo, "sync_get", MagicMock(return_value=image))
    monkeypatch.setattr(td.hw_config_repo, "sync_get", MagicMock(return_value=hw))

    destroy_called_with_open_sessions = []

    def fake_destroy(*a, **kw):
        # Record how many sessions are open right now (should be 0 — all closed).
        destroy_called_with_open_sessions.append(len(sessions_opened))
        import asyncio
        async def _noop(): pass
        return _noop()

    monkeypatch.setattr(td.terraform, "destroy", fake_destroy)

    task = td.teardown_vm_task
    task.apply(args=[booking_id])

    # More than one session must have been opened (one per DB call).
    assert len(sessions_opened) > 1, "Expected multiple short-lived sessions, got one long one"


def test_no_session_held_during_destroy(monkeypatch):
    """The SyncSessionLocal context must be exited before terraform.destroy is called."""
    booking_id = str(uuid4())
    booking = _make_booking()
    image = _make_image()
    hw = _make_hw()

    open_sessions: list[int] = []  # +1 on enter, -1 on exit

    class _TrackingSession:
        def __enter__(self):
            open_sessions.append(1)
            return self
        def __exit__(self, *a):
            open_sessions.append(-1)
            return False

    monkeypatch.setattr(td, "SyncSessionLocal", _TrackingSession)
    monkeypatch.setattr(td.repo, "sync_get", MagicMock(return_value=booking))
    monkeypatch.setattr(td.repo, "sync_update_status", MagicMock())
    monkeypatch.setattr(td.repo, "sync_set_status_message", MagicMock())
    monkeypatch.setattr(td.image_repo, "sync_get", MagicMock(return_value=image))
    monkeypatch.setattr(td.hw_config_repo, "sync_get", MagicMock(return_value=hw))

    balance_at_destroy: list[int] = []

    def fake_destroy(*a, **kw):
        balance_at_destroy.append(sum(open_sessions))
        import asyncio
        async def _noop(): pass
        return _noop()

    monkeypatch.setattr(td.terraform, "destroy", fake_destroy)

    td.teardown_vm_task.apply(args=[booking_id])

    assert balance_at_destroy == [0], (
        f"Expected 0 open sessions during destroy, got {balance_at_destroy}"
    )


# ── Status transitions on success ────────────────────────────────────────────


def test_releasing_then_released_on_success(monkeypatch):
    """RELEASING is written before destroy; RELEASED after — same as before the fix."""
    booking_id = str(uuid4())
    booking = _make_booking()
    image = _make_image()
    hw = _make_hw()

    status_calls: list[BookingStatus] = []

    def fake_update_status(session, bid, status, **kw):
        status_calls.append(status)

    monkeypatch.setattr(td, "SyncSessionLocal", lambda: MagicMock(__enter__=lambda s: s, __exit__=lambda s, *a: False))
    monkeypatch.setattr(td.repo, "sync_get", MagicMock(return_value=booking))
    monkeypatch.setattr(td.repo, "sync_update_status", fake_update_status)
    monkeypatch.setattr(td.repo, "sync_set_status_message", MagicMock())
    monkeypatch.setattr(td.image_repo, "sync_get", MagicMock(return_value=image))
    monkeypatch.setattr(td.hw_config_repo, "sync_get", MagicMock(return_value=hw))

    async def _noop(*a, **kw): pass
    monkeypatch.setattr(td.terraform, "destroy", _noop)

    td.teardown_vm_task.apply(args=[booking_id])

    assert status_calls == [BookingStatus.RELEASING, BookingStatus.RELEASED]


# ── Force teardown path ───────────────────────────────────────────────────────


def test_force_teardown_writes_released_even_on_destroy_error(monkeypatch):
    """force=True must mark RELEASED even when terraform.destroy raises."""
    booking_id = str(uuid4())
    booking = _make_booking()
    image = _make_image()
    hw = _make_hw()

    status_calls: list[BookingStatus] = []

    def fake_update_status(session, bid, status, **kw):
        status_calls.append(status)

    monkeypatch.setattr(td, "SyncSessionLocal", lambda: MagicMock(__enter__=lambda s: s, __exit__=lambda s, *a: False))
    monkeypatch.setattr(td.repo, "sync_get", MagicMock(return_value=booking))
    monkeypatch.setattr(td.repo, "sync_update_status", fake_update_status)
    monkeypatch.setattr(td.repo, "sync_set_status_message", MagicMock())
    monkeypatch.setattr(td.image_repo, "sync_get", MagicMock(return_value=image))
    monkeypatch.setattr(td.hw_config_repo, "sync_get", MagicMock(return_value=hw))

    async def _fail(*a, **kw):
        raise RuntimeError("network timeout")

    monkeypatch.setattr(td.terraform, "destroy", _fail)

    td.teardown_vm_task.apply(args=[booking_id, True])

    assert BookingStatus.RELEASED in status_calls


# ── Pooled resource path is unchanged ────────────────────────────────────────


def test_pooled_namespace_released_and_queue_promoted(monkeypatch):
    """NAMESPACE bookings must be released and promote_next_queued called — no terraform."""
    booking_id = str(uuid4())
    booking = _make_booking(resource_type=ResourceType.NAMESPACE)

    promote_calls: list[str] = []
    status_calls: list[BookingStatus] = []

    def fake_update_status(session, bid, status, **kw):
        status_calls.append(status)

    def fake_promote(session, rt):
        promote_calls.append(rt)

    monkeypatch.setattr(td, "SyncSessionLocal", lambda: MagicMock(__enter__=lambda s: s, __exit__=lambda s, *a: False))
    monkeypatch.setattr(td.repo, "sync_get", MagicMock(return_value=booking))
    monkeypatch.setattr(td.repo, "sync_update_status", fake_update_status)
    monkeypatch.setattr(td.repo, "sync_promote_next_queued", fake_promote)

    destroy_mock = MagicMock()
    monkeypatch.setattr(td.terraform, "destroy", destroy_mock)

    td.teardown_vm_task.apply(args=[booking_id])

    assert status_calls == [BookingStatus.RELEASED]
    assert promote_calls == [ResourceType.NAMESPACE.value]
    destroy_mock.assert_not_called()
