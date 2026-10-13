"""Provisioning lifecycle application service (#429).

This is a synchronous application service (use case) designed to run inside the
Celery worker. It owns every lifecycle rule of a VM provisioning attempt:
- pre-slot and post-slot released-booking guards;
- credential slot acquisition and renewal;
- password generation and apply configuration;
- PROVISIONING → CONFIGURING → READY transitions;
- mid-apply release checks and teardown handoff;
- classifying software vs infrastructure configuration errors;
- permanent secret-decryption failures vs retried infrastructure failures;
- starting the environment lease once settled.

It reaches infrastructure strictly through application-layer ports.
"""

import logging
import secrets
import string
from collections.abc import Callable
from enum import Enum
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.orm import Session

from app.application.ports import (
    CredentialSlotPort,
    CredentialSlotUnavailable,
    ProgressSink,
    RoleApplierPort,
    SessionRunner,
    SyncBookingRepositoryPort,
    SyncEnvironmentLeasePort,
    SyncHWConfigReadPort,
    SyncImageReadPort,
    TeardownHandoff,
    VmApplier,
    VmConfigRunnerPort,
)
from app.domain.booking_status import (
    PROVISIONING_OWNED_STATUSES,
    PROVISIONING_PROGRESS_STATUSES,
)
from app.domain.enums import BookingStatus
from app.domain.exceptions import (
    IllegalStatusTransitionError,
    SecretDecryptionError,
    VmConfigurationError,
)

logger = logging.getLogger(__name__)

_TERMINAL_OR_RELEASING = frozenset(
    {
        BookingStatus.RELEASING,
        BookingStatus.RELEASED,
        BookingStatus.FAILED,
    }
)


class ProvisionOutcome(str, Enum):
    SKIPPED_RELEASED = "SKIPPED_RELEASED"
    HANDED_OFF = "HANDED_OFF"
    READY = "READY"
    FAILED_PERMANENTLY = "FAILED_PERMANENTLY"


class ProvisioningAttemptFailed(Exception):
    """Raised when an attempt fails and should be retried by the caller."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class ProvisioningDeferred(Exception):
    """Raised when an attempt cannot proceed and should be deferred/retried."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class ProvisionBooking:
    def __init__(
        self,
        *,
        bookings: SyncBookingRepositoryPort,
        images: SyncImageReadPort,
        hw_configs: SyncHWConfigReadPort,
        environments: SyncEnvironmentLeasePort,
        run: SessionRunner,
        applier: VmApplier,
        config_runner: VmConfigRunnerPort,
        role_applier: RoleApplierPort,
        progress_sink_factory: Callable[
            [Callable[[str, str], None], str], ProgressSink
        ],
        teardown: TeardownHandoff,
        run_configuration: bool,
        credential_slot: CredentialSlotPort,
        request_id: str | None = None,
    ) -> None:
        self.bookings = bookings
        self.images = images
        self.hw_configs = hw_configs
        self.environments = environments
        self.run = run
        self.applier = applier
        self.config_runner = config_runner
        self.role_applier = role_applier
        self.progress_sink_factory = progress_sink_factory
        self.teardown = teardown
        self.run_configuration = run_configuration
        self.credential_slot = credential_slot
        self.request_id = request_id

    def execute(
        self,
        booking_id: UUID,
        image_id: UUID,
        hw_config_id: UUID,
        *,
        is_last_attempt: bool,
    ) -> ProvisionOutcome:
        booking_str = str(booking_id)

        outcome: ProvisionOutcome | None = None
        try:
            # Guard 1: Check before waiting for a credential slot.
            existing = self.run(lambda s: self.bookings.sync_get(s, booking_id))
            if existing.status in _TERMINAL_OR_RELEASING:
                logger.info(
                    "Booking %s was released (status=%s) — skipping provisioning",
                    booking_str,
                    existing.status.value,
                )
                return ProvisionOutcome.SKIPPED_RELEASED

            with self.credential_slot.acquire() as lease:
                outcome = self._execute_with_lease(
                    booking_id=booking_id,
                    image_id=image_id,
                    hw_config_id=hw_config_id,
                    lease=lease,
                    is_last_attempt=is_last_attempt,
                )
                return outcome
        except CredentialSlotUnavailable as exc:
            logger.warning(
                "Token acquire timed out for booking %s, retrying", booking_str
            )
            raise ProvisioningDeferred(exc) from exc
        except (ProvisioningDeferred, ProvisioningAttemptFailed):
            raise
        except Exception as exc:  # noqa: BLE001
            if outcome is not None:
                # Provisioning completed successfully; a cleanup or slot-release
                # error on context exit must not fail or retry the booking.
                logger.warning(
                    "Post-provision cleanup error for booking %s: %s",
                    booking_str,
                    exc,
                )
                return outcome
            self._handle_generic_failure(
                booking_id,
                exc,
                is_last_attempt=is_last_attempt,
                lifecycle=self.run,
            )

    def _execute_with_lease(
        self,
        booking_id: UUID,
        image_id: UUID,
        hw_config_id: UUID,
        lease: Any,
        *,
        is_last_attempt: bool,
    ) -> ProvisionOutcome:
        booking_str = str(booking_id)

        sink: ProgressSink | None = None

        def _lifecycle(work: Callable[[Session], Any]) -> Any:
            if sink is not None:
                sink.flush()
            return self.run(work)

        try:
            # Guard 2: Check again once the credential slot is held.
            existing = self.run(lambda s: self.bookings.sync_get(s, booking_id))
            if existing.status in _TERMINAL_OR_RELEASING:
                logger.info(
                    "Booking %s was released (status=%s) — skipping provisioning",
                    booking_str,
                    existing.status.value,
                )
                return ProvisionOutcome.SKIPPED_RELEASED

            image = self.run(lambda s: self.images.sync_get(s, image_id))
            hw = self.run(lambda s: self.hw_configs.sync_get(s, hw_config_id))

            vm_password = existing.vm_password or "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
            )
            workspace_id = f"booking-{booking_str}"
            config = {
                "name": f"portal-{booking_str[:8]}",
                "vapp_template_id": image.vapp_template_id,
                "cpus": hw.cpus,
                "memory": hw.memory_mb,
                "disk_size": hw.disk_mb,
                "vm_password": vm_password,
            }

            if existing.status not in (
                BookingStatus.PROVISIONING,
                BookingStatus.CONFIGURING,
            ):
                self.run(
                    lambda s: self.bookings.sync_update_status(
                        s, booking_id, BookingStatus.PROVISIONING
                    )
                )
            logger.info("Provisioning started for booking %s", booking_str)

            def persist_fn(chunk: str, last: str) -> None:
                self.run(
                    lambda s: self.bookings.sync_append_progress(
                        s,
                        booking_id,
                        chunk,
                        last,
                        PROVISIONING_PROGRESS_STATUSES,
                    )
                )

            sink = self.progress_sink_factory(persist_fn, booking_str)

            def _on_progress(msg: str) -> None:
                lease.renew()
                if sink is not None:
                    sink.record(msg)

            result = self.applier.apply(
                workspace_id,
                config,
                api_token=lease.api_token,
                on_progress=_on_progress,
            )
            ip = result["ip"]

            _lifecycle(
                lambda s: self.bookings.sync_set_status_message(
                    s,
                    booking_id,
                    None,
                    if_status_in=PROVISIONING_OWNED_STATUSES,
                )
            )

            booking = self.run(lambda s: self.bookings.sync_get(s, booking_id))
            if booking.status not in PROVISIONING_OWNED_STATUSES:
                logger.info(
                    "Booking %s was released while its terraform apply was in flight (status=%s) — not configuring",
                    booking_str,
                    booking.status.value,
                )
                sink.close()
                sink = None
                if booking.status == BookingStatus.RELEASING:
                    self.teardown.hand_off(booking_str, request_id=self.request_id)
                return ProvisionOutcome.HANDED_OFF

            config_failed = False
            config_message = None
            if self.run_configuration:
                _lifecycle(
                    lambda s: self.bookings.sync_update_status(
                        s,
                        booking_id,
                        BookingStatus.CONFIGURING,
                        vm_ip=ip,
                        vm_password=vm_password,
                    )
                )
                _on_progress(f"Waiting for {ip} to become reachable…")
                client = self.config_runner.connect(
                    ip, vm_password, on_progress=_on_progress
                )
                try:
                    if booking.startup_script:
                        logger.info(
                            "Running startup script for booking %s — IP: %s",
                            booking_str,
                            ip,
                        )
                        self.config_runner.run_script(
                            client,
                            booking.startup_script,
                            on_progress=_on_progress,
                        )
                    if booking.config_roles:
                        logger.info(
                            "Applying %d role(s) for booking %s",
                            len(booking.config_roles),
                            booking_str,
                        )
                        self.role_applier.apply_roles(
                            booking,
                            ip=ip,
                            password=vm_password,
                            on_progress=_on_progress,
                            extra_vars=booking.extra_vars or {},
                            label=booking.environment_label or "",
                        )
                except VmConfigurationError as cfg_exc:
                    config_failed = True
                    config_message = str(cfg_exc)
                    logger.warning(
                        "Configuration failed for booking %s: %s",
                        booking_str,
                        cfg_exc,
                    )
                finally:
                    self.config_runner.close(client)

                _lifecycle(
                    lambda s: self.bookings.sync_set_status_message(
                        s,
                        booking_id,
                        config_message,
                        if_status_in=PROVISIONING_OWNED_STATUSES,
                    )
                )

            _lifecycle(
                lambda s: self.bookings.sync_update_status(
                    s,
                    booking_id,
                    BookingStatus.READY,
                    vm_ip=ip,
                    vm_password=vm_password,
                    config_failed=config_failed,
                    start_lease=True,
                )
            )
            _lifecycle(
                lambda s: self.environments.sync_start_lease_if_ready_for_booking(
                    s, booking_id
                )
            )
            logger.info(
                "Provisioning complete for booking %s — IP: %s (config_failed=%s)",
                booking_str,
                ip,
                config_failed,
            )
            return ProvisionOutcome.READY

        except SecretDecryptionError as exc:
            logger.error(
                "Secret decryption failed for booking %s (not retrying): %s",
                booking_str,
                exc,
            )
            err_msg = f"Secret decryption failed: {exc}"
            try:
                _lifecycle(
                    lambda s: self.bookings.sync_set_status_message(
                        s,
                        booking_id,
                        err_msg,
                        if_status_in=PROVISIONING_OWNED_STATUSES,
                    )
                )
                _lifecycle(
                    lambda s: self.bookings.sync_update_status(
                        s, booking_id, BookingStatus.FAILED
                    )
                )
                _lifecycle(
                    lambda s: self.environments.sync_start_lease_if_ready_for_booking(
                        s, booking_id
                    )
                )
            except Exception as write_exc:  # noqa: BLE001
                logger.warning(
                    "Best-effort failure write failed for booking %s: %s",
                    booking_str,
                    write_exc,
                )
            return ProvisionOutcome.FAILED_PERMANENTLY

        except Exception as exc:  # noqa: BLE001
            self._handle_generic_failure(
                booking_id,
                exc,
                is_last_attempt=is_last_attempt,
                lifecycle=_lifecycle,
            )

        finally:
            if sink is not None:
                sink.close()

    def _handle_generic_failure(
        self,
        booking_id: UUID,
        exc: Exception,
        *,
        is_last_attempt: bool,
        lifecycle: Callable[[Callable[[Session], Any]], Any],
    ) -> NoReturn:
        booking_str = str(booking_id)
        logger.exception("Provisioning failed for booking %s", booking_str)
        new_status = BookingStatus.FAILED if is_last_attempt else BookingStatus.RETRY
        try:
            lifecycle(
                lambda s: self.bookings.sync_set_status_message(
                    s,
                    booking_id,
                    "Failed — see audit log",
                    if_status_in=PROVISIONING_OWNED_STATUSES,
                )
            )
            lifecycle(
                lambda s: self.bookings.sync_update_status(s, booking_id, new_status)
            )
            if new_status == BookingStatus.FAILED:
                lifecycle(
                    lambda s: self.environments.sync_start_lease_if_ready_for_booking(
                        s, booking_id
                    )
                )
        except IllegalStatusTransitionError:
            # A booking still in PENDING cannot transition to RETRY; it remains
            # in its current status for the next retry attempt.
            pass
        except Exception as write_exc:  # noqa: BLE001
            logger.warning(
                "Best-effort failure write failed for booking %s: %s",
                booking_str,
                write_exc,
            )
        raise ProvisioningAttemptFailed(exc) from exc
