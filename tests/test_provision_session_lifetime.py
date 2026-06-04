"""Regression test for #143 — provision worker uses short-lived sessions.

Before the fix one SyncSessionLocal() was held open across the whole terraform apply, pinning a
pool connection. After the fix each DB write runs in its own short session and no session is open
during the apply.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.domain.entities import HWConfig, VMImage
from app.domain.enums import BookingStatus


class _SessionFactory:
    """Fake SyncSessionLocal: tracks how many sessions are open at any moment."""

    def __init__(self):
        self.open_now = 0
        self.max_concurrent = 0
        self.total_opened = 0

    def __call__(self):
        factory = self
        sess = MagicMock(name=f"session-{factory.total_opened}")
        cm = MagicMock()

        def _enter(*_a):
            factory.open_now += 1
            factory.total_opened += 1
            factory.max_concurrent = max(factory.max_concurrent, factory.open_now)
            return sess

        def _exit(*_a):
            factory.open_now -= 1
            return False

        cm.__enter__ = _enter
        cm.__exit__ = _exit
        return cm


def test_no_session_held_across_apply():
    now = datetime.now(timezone.utc)
    image = VMImage(id=uuid4(), name="Ubuntu", vapp_template_id="tpl-1", is_active=True, created_at=now)
    hw = HWConfig(id=uuid4(), name="medium", cpus=2, memory_mb=4096, disk_mb=26624, is_active=True, created_at=now)

    factory = _SessionFactory()
    open_during_apply = []

    def _fake_run(coro):
        # Record how many sessions are open at the moment terraform.apply runs.
        open_during_apply.append(factory.open_now)
        if hasattr(coro, "close"):
            coro.close()  # silence "coroutine never awaited"
        return {"ip": "10.0.0.1"}

    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=image)
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=hw)

    with (
        patch("app.tasks.provision.SyncSessionLocal", factory),
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", side_effect=_fake_run),
    ):
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[str(uuid4()), str(image.id), str(hw.id)])

    # The core fix: no DB session is open while terraform.apply runs.
    assert open_during_apply == [0]
    # Never more than one session open at a time, and several were opened (one per write).
    assert factory.max_concurrent == 1
    assert factory.total_opened >= 3

    # The PROVISIONING write and the READY write used different session instances.
    status_calls = mock_repo.sync_update_status.call_args_list
    provisioning_sess = next(c.args[0] for c in status_calls if c.args[2] == BookingStatus.PROVISIONING)
    ready_sess = next(c.args[0] for c in status_calls if c.args[2] == BookingStatus.READY)
    assert provisioning_sess is not ready_sess
