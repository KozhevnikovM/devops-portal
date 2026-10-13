"""Unit tests for ProvisionBooking application service (design Decision 8).

Exercises every scenario in specs/vm-provisioning-lifecycle/spec.md using pure
in-memory fakes. No patch(), no Celery, no Redis, no Terraform, and no real database.
"""

import importlib
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.ports import CredentialSlotUnavailable
from app.application.use_cases.provision_booking import (
    ProvisionBooking,
    ProvisioningAttemptFailed,
    ProvisioningDeferred,
    ProvisionOutcome,
)
from app.domain.booking_status import can_transition
from app.domain.entities import Booking, HWConfig, VMImage
from app.domain.enums import BookingStatus, DriveType, ResourceType
from app.domain.exceptions import (
    IllegalStatusTransitionError,
    SecretDecryptionError,
)
from app.infrastructure.config.ansible import AnsibleConfigError
from app.infrastructure.config.runner import ConfigScriptError, VmUnreachableError

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeCredentialLease:
    def __init__(
        self, slot: "FakeCredentialSlot", api_token: str | None = "fake-token"
    ) -> None:
        self.slot = slot
        self.api_token = api_token

    def renew(self) -> None:
        self.slot.renew_count += 1


class FakeCredentialSlot:
    def __init__(
        self, *, raise_unavailable: bool = False, api_token: str | None = "token-1"
    ) -> None:
        self.raise_unavailable = raise_unavailable
        self.api_token = api_token
        self.acquire_count = 0
        self.release_count = 0
        self.renew_count = 0

    @contextmanager
    def acquire(self):
        if self.raise_unavailable:
            raise CredentialSlotUnavailable("all slots busy")
        self.acquire_count += 1
        lease = FakeCredentialLease(self, api_token=self.api_token)
        try:
            yield lease
        finally:
            self.release_count += 1


class FakeBookingStore:
    def __init__(self, event_log: list[str]) -> None:
        self.bookings: dict[UUID, Booking] = {}
        self.event_log = event_log
        self.fail_writes = False
        self.sync_get_calls = 0
        self.fail_get_on_call: int | None = None

    def add(self, booking: Booking) -> None:
        self.bookings[booking.id] = booking

    def sync_get(self, session: Any, booking_id: UUID) -> Booking:
        self.sync_get_calls += 1
        if (
            self.fail_get_on_call is not None
            and self.sync_get_calls == self.fail_get_on_call
        ):
            raise RuntimeError("Database connection lost during sync_get")
        return self.bookings[booking_id]

    def sync_update_status(
        self,
        session: Any,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
        vm_password: str | None = None,
        config_failed: bool | None = None,
        start_lease: bool = False,
        actor_id: str = "system",
    ) -> None:
        if self.fail_writes:
            raise RuntimeError("Database write failure")
        self.event_log.append(f"update_status:{status.value}")
        booking = self.bookings[booking_id]
        if booking.status != status and not can_transition(booking.status, status):
            raise IllegalStatusTransitionError(
                f"Cannot transition {booking.status} -> {status}"
            )
        booking.status = status
        if vm_ip is not None:
            booking.vm_ip = vm_ip
        if vm_password is not None:
            booking.vm_password = vm_password
        if config_failed is not None:
            booking.config_failed = config_failed

    def sync_set_status_message(
        self,
        session: Any,
        booking_id: UUID,
        message: str | None,
        if_status_in: frozenset[BookingStatus] | None = None,
    ) -> bool:
        if self.fail_writes:
            raise RuntimeError("Database write failure")
        self.event_log.append(f"status_message:{message}")
        booking = self.bookings[booking_id]
        if if_status_in is not None and booking.status not in if_status_in:
            return False
        booking.status_message = message
        return True

    def sync_append_progress(
        self,
        session: Any,
        booking_id: UUID,
        chunk: str,
        last_message: str,
        accepting: frozenset[BookingStatus],
    ) -> None:
        self.event_log.append(f"append_progress:{last_message}")


class FakeImageStore:
    def __init__(self) -> None:
        self.images: dict[UUID, VMImage] = {}

    def sync_get(self, session: Any, image_id: UUID) -> VMImage:
        return self.images.get(
            image_id,
            VMImage(
                id=image_id,
                name="Ubuntu",
                vapp_template_id="tpl-ub-22",
                is_active=True,
                created_at=datetime.now(UTC),
            ),
        )


class FakeHWConfigStore:
    def __init__(self) -> None:
        self.configs: dict[UUID, HWConfig] = {}

    def sync_get(self, session: Any, hw_config_id: UUID) -> HWConfig:
        return self.configs.get(
            hw_config_id,
            HWConfig(
                id=hw_config_id,
                name="medium",
                cpus=4,
                memory_mb=8192,
                disk_mb=40960,
                drive_type=DriveType.SSD,
                is_active=True,
                created_at=datetime.now(UTC),
            ),
        )


class FakeEnvironmentStore:
    def __init__(self, event_log: list[str]) -> None:
        self.lease_started_for: list[UUID] = []
        self.event_log = event_log

    def sync_start_lease_if_ready_for_booking(
        self, session: Any, booking_id: UUID
    ) -> bool:
        self.event_log.append(f"start_lease:{booking_id}")
        self.lease_started_for.append(booking_id)
        return True


class FakeApplier:
    def __init__(
        self,
        event_log: list[str],
        *,
        ip: str = "192.168.1.100",
        raise_exc: Exception | None = None,
        on_apply: Callable[[], None] | None = None,
    ) -> None:
        self.ip = ip
        self.raise_exc = raise_exc
        self.on_apply = on_apply
        self.applied_configs: list[dict[str, Any]] = []
        self.applied_tokens: list[str | None] = []
        self.event_log = event_log

    def apply(
        self,
        workspace_id: str,
        config: dict[str, Any],
        *,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.event_log.append("applier:apply")
        self.applied_configs.append(config)
        self.applied_tokens.append(api_token)
        if on_progress:
            on_progress("Creating VM in vCloud Director...")
        if self.on_apply:
            self.on_apply()
        if self.raise_exc:
            raise self.raise_exc
        return {"ip": self.ip}


class FakeConfigRunner:
    def __init__(
        self,
        event_log: list[str],
        *,
        connect_exc: Exception | None = None,
        script_exc: Exception | None = None,
    ) -> None:
        self.connect_exc = connect_exc
        self.script_exc = script_exc
        self.closed_clients: list[Any] = []
        self.ran_scripts: list[str] = []
        self.event_log = event_log

    def connect(
        self, ip: str, password: str, on_progress: Callable[[str], None] | None = None
    ) -> Any:
        self.event_log.append("config_runner:connect")
        if on_progress:
            on_progress("Connecting SSH...")
        if self.connect_exc:
            raise self.connect_exc
        return "fake-client"

    def run_script(
        self, client: Any, script: str, on_progress: Callable[[str], None] | None = None
    ) -> None:
        self.event_log.append("config_runner:run_script")
        self.ran_scripts.append(script)
        if on_progress:
            on_progress("Running startup script...")
        if self.script_exc:
            raise self.script_exc

    def close(self, client: Any) -> None:
        self.event_log.append("config_runner:close")
        self.closed_clients.append(client)


class FakeRoleApplier:
    def __init__(
        self,
        event_log: list[str],
        *,
        raise_exc: Exception | None = None,
        on_apply: Callable[[], None] | None = None,
    ) -> None:
        self.raise_exc = raise_exc
        self.on_apply = on_apply
        self.applied_bookings: list[Booking] = []
        self.event_log = event_log

    def apply_roles(
        self,
        booking: Booking,
        *,
        ip: str,
        password: str,
        on_progress: Callable[[str], None] | None = None,
        extra_vars: dict[str, Any] | None = None,
        label: str = "",
    ) -> None:
        self.event_log.append("role_applier:apply_roles")
        self.applied_bookings.append(booking)
        if on_progress:
            on_progress("Applying Ansible roles...")
        if self.on_apply:
            self.on_apply()
        if self.raise_exc:
            raise self.raise_exc


class FakeProgressSink:
    def __init__(self, event_log: list[str], label: str) -> None:
        self.event_log = event_log
        self.label = label
        self.records: list[str] = []
        self.is_closed = False

    def record(self, message: str) -> None:
        self.records.append(message)
        self.event_log.append(f"sink_record:{message}")

    def flush(self) -> None:
        self.event_log.append("sink_flush")

    def close(self) -> None:
        self.is_closed = True
        self.event_log.append("sink_close")


class FakeTeardownHandoff:
    def __init__(self, event_log: list[str]) -> None:
        self.handoffs: list[tuple[str, str | None]] = []
        self.event_log = event_log

    def hand_off(self, booking_id: str, request_id: str | None = None) -> None:
        self.event_log.append(f"teardown_handoff:{booking_id}")
        self.handoffs.append((booking_id, request_id))


# ── Fixtures & Helpers ────────────────────────────────────────────────────────


def _make_booking(
    *,
    status: BookingStatus = BookingStatus.PENDING,
    startup_script: str | None = None,
    config_roles: list[str] | None = None,
    vm_password: str | None = None,
) -> Booking:
    now = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        user_id="user-1",
        status=status,
        resource_type=ResourceType.VM,
        ttl_minutes=120,
        expires_at=now,
        created_at=now,
        startup_script=startup_script,
        config_roles=config_roles or [],
        vm_password=vm_password,
    )


class Harness:
    def __init__(
        self,
        *,
        run_configuration: bool = True,
        raise_slot_unavailable: bool = False,
        slot_token: str | None = "vcd-token-abc",
        applier_ip: str = "10.0.0.99",
        applier_exc: Exception | None = None,
        on_apply: Callable[[], None] | None = None,
        connect_exc: Exception | None = None,
        script_exc: Exception | None = None,
        roles_exc: Exception | None = None,
        on_roles: Callable[[], None] | None = None,
        request_id: str | None = "req-1234",
    ) -> None:
        self.event_log: list[str] = []
        self.booking_store = FakeBookingStore(self.event_log)
        self.image_store = FakeImageStore()
        self.hw_config_store = FakeHWConfigStore()
        self.env_store = FakeEnvironmentStore(self.event_log)
        self.slot = FakeCredentialSlot(
            raise_unavailable=raise_slot_unavailable, api_token=slot_token
        )
        self.applier = FakeApplier(
            self.event_log, ip=applier_ip, raise_exc=applier_exc, on_apply=on_apply
        )
        self.config_runner = FakeConfigRunner(
            self.event_log, connect_exc=connect_exc, script_exc=script_exc
        )
        self.role_applier = FakeRoleApplier(
            self.event_log, raise_exc=roles_exc, on_apply=on_roles
        )
        self.teardown = FakeTeardownHandoff(self.event_log)
        self.sinks_created: list[FakeProgressSink] = []

        def sink_factory(persist: Any, label: str) -> FakeProgressSink:
            sink = FakeProgressSink(self.event_log, label)
            self.sinks_created.append(sink)
            return sink

        self.service = ProvisionBooking(
            bookings=self.booking_store,
            images=self.image_store,
            hw_configs=self.hw_config_store,
            environments=self.env_store,
            run=lambda work: work(None),
            applier=self.applier,
            config_runner=self.config_runner,
            role_applier=self.role_applier,
            progress_sink_factory=sink_factory,
            teardown=self.teardown,
            run_configuration=run_configuration,
            credential_slot=self.slot,
            request_id=request_id,
        )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_module_does_not_import_prohibited_layers():
    """Verify that provision_booking imports zero infrastructure, tasks, celery, redis or asyncio."""
    module = importlib.import_module("app.application.use_cases.provision_booking")
    source_file = module.__file__
    assert source_file is not None
    with open(source_file) as f:
        content = f.read()
    prohibited = ["app.infrastructure", "app.tasks", "celery", "redis", "asyncio"]
    for word in prohibited:
        assert word not in content, f"Found prohibited import '{word}' in {source_file}"


@pytest.mark.parametrize(
    "status", [BookingStatus.RELEASING, BookingStatus.RELEASED, BookingStatus.FAILED]
)
def test_released_while_queued_never_acquires_slot(status: BookingStatus):
    """Scenario: Released while queued (and RELEASED, FAILED variants) — slot never acquired."""
    h = Harness()
    booking = _make_booking(status=status)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.SKIPPED_RELEASED
    assert h.slot.acquire_count == 0
    assert h.slot.release_count == 0
    assert len(h.applier.applied_configs) == 0
    # No status writes or message writes
    assert not any(e.startswith("update_status") for e in h.event_log)
    assert not any(e.startswith("status_message") for e in h.event_log)


def test_released_with_no_credential_slot_free():
    """Scenario: Released, with no credential slot free — ends without waiting for slot."""
    h = Harness(raise_slot_unavailable=True)
    booking = _make_booking(status=BookingStatus.RELEASED)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.SKIPPED_RELEASED
    assert h.slot.acquire_count == 0
    assert not any(e.startswith("update_status") for e in h.event_log)


def test_released_during_slot_wait_releases_slot_without_apply():
    """Scenario: Released while waiting for a credential slot — slot acquired then released without apply."""
    h = Harness()
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    # Simulate release while waiting for the slot (status changes right as slot is acquired)
    orig_acquire = h.slot.acquire

    @contextmanager
    def acquire_and_release_booking():
        with orig_acquire() as lease:
            booking.status = BookingStatus.RELEASING
            yield lease

    h.slot.acquire = acquire_and_release_booking

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.SKIPPED_RELEASED
    assert h.slot.acquire_count == 1
    assert h.slot.release_count == 1
    assert len(h.applier.applied_configs) == 0
    assert not any(e.startswith("update_status") for e in h.event_log)


def test_slot_unavailable_raises_deferred_with_no_writes():
    """Scenario: No credential slot free — raises ProvisioningDeferred, writes nothing."""
    h = Harness(raise_slot_unavailable=True)
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    with pytest.raises(ProvisioningDeferred) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "all slots busy" in str(exc_info.value)
    assert not any(e.startswith("update_status") for e in h.event_log)
    assert not any(e.startswith("status_message") for e in h.event_log)
    assert len(h.sinks_created) == 0


@pytest.mark.parametrize(
    "status", [BookingStatus.PROVISIONING, BookingStatus.CONFIGURING]
)
def test_recovery_redispatch_proceeds(status: BookingStatus):
    """Scenario: Startup recovery re-dispatch for PROVISIONING or CONFIGURING proceeds."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=status)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.status == BookingStatus.READY
    assert booking.vm_ip == "10.0.0.99"
    assert h.slot.release_count == 1


def test_configuration_enabled_full_lifecycle():
    """Scenario: Configuration enabled — moves PROVISIONING -> CONFIGURING -> READY."""
    h = Harness(run_configuration=True)
    booking = _make_booking(
        status=BookingStatus.PENDING,
        startup_script="echo test",
        config_roles=["nginx"],
    )
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.status == BookingStatus.READY
    assert booking.config_failed is False
    assert booking.vm_ip == "10.0.0.99"
    assert len(h.config_runner.ran_scripts) == 1
    assert len(h.role_applier.applied_bookings) == 1
    assert len(h.config_runner.closed_clients) == 1
    assert h.slot.renew_count > 0  # renewed on each progress message
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1

    # Invariant: Every lifecycle write was preceded by a sink flush
    for i, event in enumerate(h.event_log):
        if event.startswith(("update_status:CONFIGURING", "update_status:READY")):
            assert h.event_log[i - 1] == "sink_flush", (
                f"Write at index {i} was not preceded by flush: {h.event_log[: i + 1]}"
            )


def test_stub_provisioning_skips_configuring():
    """Scenario: Stub provisioning — moves PROVISIONING -> READY without CONFIGURING."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.status == BookingStatus.READY
    assert not any("update_status:CONFIGURING" in e for e in h.event_log)
    assert len(h.config_runner.ran_scripts) == 0
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_existing_password_is_reused():
    """Scenario: Existing password is reused across attempts."""
    h = Harness(run_configuration=False)
    booking = _make_booking(
        status=BookingStatus.PENDING, vm_password="hunter2-plaintext"
    )
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.vm_password == "hunter2-plaintext"
    assert h.applier.applied_configs[0]["vm_password"] == "hunter2-plaintext"


def test_password_generated_when_missing():
    """Scenario: 16-character alphanumeric password is generated when missing."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PENDING, vm_password=None)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.vm_password is not None
    assert len(booking.vm_password) == 16
    assert booking.vm_password.isalnum()


def test_released_mid_apply_hands_off_to_teardown():
    """Scenario: Released mid-apply — handoff to teardown once, no configuration."""

    def release_during_apply():
        booking.status = BookingStatus.RELEASING

    h = Harness(
        run_configuration=True, on_apply=release_during_apply, request_id="corr-999"
    )
    booking = _make_booking(status=BookingStatus.PENDING, startup_script="echo hi")
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.HANDED_OFF
    assert h.teardown.handoffs == [(str(booking.id), "corr-999")]
    assert len(h.config_runner.ran_scripts) == 0
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_already_settled_mid_apply_no_handoff():
    """Scenario: Teardown already settled the booking (RELEASED) mid-apply — no handoff."""

    def settle_during_apply():
        booking.status = BookingStatus.RELEASED

    h = Harness(run_configuration=True, on_apply=settle_during_apply)
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.HANDED_OFF
    assert len(h.teardown.handoffs) == 0
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_startup_script_fails_is_ready_with_config_failed():
    """Scenario: Startup script fails — READY with config_failed=True, no retry."""
    h = Harness(
        run_configuration=True,
        script_exc=ConfigScriptError("exit 127: command not found"),
    )
    booking = _make_booking(status=BookingStatus.PENDING, startup_script="false")
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.status == BookingStatus.READY
    assert booking.config_failed is True
    assert "exit 127" in (booking.status_message or "")
    assert len(h.config_runner.closed_clients) == 1
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_ansible_role_fails_is_ready_with_config_failed():
    """Scenario: Ansible role fails — READY with config_failed=True, no retry."""
    h = Harness(
        run_configuration=True,
        roles_exc=AnsibleConfigError("playbook task failed: git clone"),
    )
    booking = _make_booking(status=BookingStatus.PENDING, config_roles=["git"])
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.READY
    assert booking.status == BookingStatus.READY
    assert booking.config_failed is True
    assert "playbook task failed" in (booking.status_message or "")
    assert len(h.config_runner.closed_clients) == 1
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_vm_unreachable_is_infrastructure_failure_retried():
    """Scenario: VM unreachable — infrastructure failure, retried when not last attempt."""
    h = Harness(
        run_configuration=True, connect_exc=VmUnreachableError("SSH timeout after 60s")
    )
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert isinstance(exc_info.value.cause, VmUnreachableError)
    assert booking.status == BookingStatus.RETRY
    assert booking.status_message == "Failed — see audit log"
    assert booking.id not in h.env_store.lease_started_for
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_infrastructure_failure_on_last_attempt_is_failed():
    """Scenario: Last attempt fails — moves to FAILED, lease started if settled."""
    h = Harness(
        run_configuration=False,
        applier_exc=RuntimeError("vCloud Director connection reset"),
    )
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=True)

    assert "connection reset" in str(exc_info.value.cause)
    assert booking.status == BookingStatus.FAILED
    assert booking.status_message == "Failed — see audit log"
    assert booking.id in h.env_store.lease_started_for
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_secret_decryption_failure_fails_permanently():
    """Scenario: Wrong or missing encryption key — FAILED permanently, no retry."""
    h = Harness(
        run_configuration=True, roles_exc=SecretDecryptionError("bad encryption key")
    )
    booking = _make_booking(
        status=BookingStatus.PENDING, config_roles=["role-with-secrets"]
    )
    h.booking_store.add(booking)

    outcome = h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert outcome == ProvisionOutcome.FAILED_PERMANENTLY
    assert booking.status == BookingStatus.FAILED
    assert "Secret decryption failed" in (booking.status_message or "")
    assert booking.id in h.env_store.lease_started_for
    assert h.sinks_created[0].is_closed is True
    assert h.slot.release_count == 1


def test_best_effort_writes_on_failure_do_not_hide_cause():
    """Scenario: Failure recording writes raising does not hide the original cause or outcome."""
    # Case 1: Generic failure
    h = Harness(
        run_configuration=False,
        applier_exc=RuntimeError("Original error"),
        on_apply=lambda: setattr(h.booking_store, "fail_writes", True),
    )
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "Original error" in str(exc_info.value.cause)

    # Case 2: Secret decryption failure
    h2 = Harness(
        run_configuration=True,
        roles_exc=SecretDecryptionError("Bad key"),
        on_roles=lambda: setattr(h2.booking_store, "fail_writes", True),
    )
    booking2 = _make_booking(status=BookingStatus.PENDING, config_roles=["secrets"])
    h2.booking_store.add(booking2)

    outcome2 = h2.service.execute(booking2.id, uuid4(), uuid4(), is_last_attempt=False)
    assert outcome2 == ProvisionOutcome.FAILED_PERMANENTLY


def test_lease_start_matrix():
    """Scenario: A settled environment child starts lease on READY and final FAILED, never on RETRY."""
    # READY -> starts lease
    h_ready = Harness(run_configuration=False)
    b_ready = _make_booking()
    h_ready.booking_store.add(b_ready)
    h_ready.service.execute(b_ready.id, uuid4(), uuid4(), is_last_attempt=False)
    assert b_ready.id in h_ready.env_store.lease_started_for

    # RETRY -> does not start lease
    h_retry = Harness(run_configuration=False, applier_exc=RuntimeError("err"))
    b_retry = _make_booking()
    h_retry.booking_store.add(b_retry)
    with pytest.raises(ProvisioningAttemptFailed):
        h_retry.service.execute(b_retry.id, uuid4(), uuid4(), is_last_attempt=False)
    assert b_retry.id not in h_retry.env_store.lease_started_for

    # Final FAILED -> starts lease
    h_failed = Harness(run_configuration=False, applier_exc=RuntimeError("err"))
    b_failed = _make_booking()
    h_failed.booking_store.add(b_failed)
    with pytest.raises(ProvisioningAttemptFailed):
        h_failed.service.execute(b_failed.id, uuid4(), uuid4(), is_last_attempt=True)
    assert b_failed.id in h_failed.env_store.lease_started_for


def test_guard_1_db_failure_records_retry_and_raises_attempt_failed():
    """Pre-slot guard DB failure: stays PENDING, attempts status message, never acquires slot."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)
    h.booking_store.fail_get_on_call = 1

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "Database connection lost during sync_get" in str(exc_info.value.cause)
    assert h.slot.acquire_count == 0
    # Booking stays PENDING because PENDING cannot transition to RETRY; Celery retries the PENDING booking.
    assert booking.status == BookingStatus.PENDING
    assert booking.status_message == "Failed — see audit log"


def test_guard_1_db_failure_on_provisioning_transitions_to_retry():
    """Pre-slot guard DB failure on a booking already in PROVISIONING transitions to RETRY."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PROVISIONING)
    h.booking_store.add(booking)
    h.booking_store.fail_get_on_call = 1

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "Database connection lost during sync_get" in str(exc_info.value.cause)
    assert h.slot.acquire_count == 0
    assert booking.status == BookingStatus.RETRY
    assert booking.status_message == "Failed — see audit log"


def test_guard_2_db_failure_records_retry_releases_slot_and_raises_attempt_failed():
    """Post-slot guard DB failure: stays PENDING, releases slot, raises ProvisioningAttemptFailed."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PENDING)
    h.booking_store.add(booking)
    h.booking_store.fail_get_on_call = 2

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "Database connection lost during sync_get" in str(exc_info.value.cause)
    assert h.slot.acquire_count == 1
    assert h.slot.release_count == 1
    # Booking stays PENDING because PENDING cannot transition to RETRY; Celery retries the PENDING booking.
    assert booking.status == BookingStatus.PENDING
    assert booking.status_message == "Failed — see audit log"


def test_guard_2_db_failure_on_provisioning_transitions_to_retry():
    """Post-slot guard DB failure on a booking already in PROVISIONING transitions to RETRY."""
    h = Harness(run_configuration=False)
    booking = _make_booking(status=BookingStatus.PROVISIONING)
    h.booking_store.add(booking)
    h.booking_store.fail_get_on_call = 2

    with pytest.raises(ProvisioningAttemptFailed) as exc_info:
        h.service.execute(booking.id, uuid4(), uuid4(), is_last_attempt=False)

    assert "Database connection lost during sync_get" in str(exc_info.value.cause)
    assert h.slot.acquire_count == 1
    assert h.slot.release_count == 1
    assert booking.status == BookingStatus.RETRY
    assert booking.status_message == "Failed — see audit log"
