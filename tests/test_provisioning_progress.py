from unittest.mock import MagicMock, call, patch
from uuid import uuid4
from datetime import datetime, timezone

from app.domain.entities import VMImage, HWConfig
from app.domain.enums import BookingStatus


def _make_image():
    return VMImage(
        id=uuid4(), name="Ubuntu 22.04", vapp_template_id="tpl-001",
        is_active=True, created_at=datetime.now(timezone.utc),
    )


def _make_hw():
    return HWConfig(
        id=uuid4(), name="medium", cpus=2, memory_mb=4096, hdd_mb=26624,
        is_active=True, created_at=datetime.now(timezone.utc),
    )


def test_provision_task_sets_status_messages():
    """provision_vm_task writes progress messages then clears on success."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_make_image())
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_make_hw())

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    msg_calls = mock_repo.sync_set_status_message.call_args_list
    messages = [c.args[2] for c in msg_calls]
    assert "Initializing workspace…" in messages
    assert "Applying configuration…" in messages
    assert None in messages  # cleared on success


def test_provision_task_sets_failure_message():
    """provision_vm_task writes failure message when apply raises."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_make_image())
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_make_hw())

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", side_effect=RuntimeError("boom")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    msg_calls = mock_repo.sync_set_status_message.call_args_list
    messages = [c.args[2] for c in msg_calls]
    assert "Failed — see audit log" in messages


def test_teardown_task_sets_status_messages():
    """teardown_vm_task writes progress message then clears on success."""
    booking_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()
    mock_booking.vm_password = "pass"
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=MagicMock(vapp_template_id="tpl"))
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, hdd_mb=26624))

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.asyncio.run", return_value=None),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    msg_calls = mock_repo.sync_set_status_message.call_args_list
    messages = [c.args[2] for c in msg_calls]
    assert "Destroying VM…" in messages
    assert None in messages  # cleared on success


def test_teardown_task_sets_failure_message():
    """teardown_vm_task writes failure message on final retry."""
    booking_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()
    mock_booking.vm_password = "pass"
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=MagicMock(vapp_template_id="tpl"))
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, hdd_mb=26624))

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.asyncio.run", side_effect=RuntimeError("destroy failed")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    msg_calls = mock_repo.sync_set_status_message.call_args_list
    messages = [c.args[2] for c in msg_calls]
    assert "Teardown failed — see audit log" in messages
