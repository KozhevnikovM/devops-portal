"""Celery task adapter for VM provisioning (#429).

This module is a thin Celery adapter around the `ProvisionBooking` application service.
It owns Celery and infrastructure-specific concerns:
- binding and resetting the request correlation id;
- implementing the CredentialSlotPort on Redis / stub;
- managing the provisioning-lock marker and asyncio.run around Terraform;
- mapping service outcomes to Celery retry control flow;
- providing short-lived database sessions via _run.

All lifecycle policy (transitions, error classification, lease start) is owned
by ProvisionBooking in app/application/use_cases/provision_booking.py.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import redis as redis_lib

from app.application.ports import CredentialSlotUnavailable, SyncBookingRepositoryPort
from app.application.use_cases.provision_booking import (
    ProvisionBooking,
    ProvisioningAttemptFailed,
    ProvisioningDeferred,
)
from app.config import settings
from app.infrastructure import provisioning_lock
from app.infrastructure.celery_app import celery_app
from app.infrastructure.config.ansible import AnsibleConfigError, build_ansible_runner
from app.infrastructure.config.runner import ConfigScriptError, build_config_runner
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.logging_config import request_id_ctx_var
from app.infrastructure.progress_recorder import recorder_from_settings
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter
from app.tasks.teardown import teardown_vm_task

logger = logging.getLogger(__name__)

repo: SyncBookingRepositoryPort = BookingRepository()
env_repo = EnvironmentRepository()
image_repo = ImageRepository()
hw_config_repo = HWConfigRepository()
terraform = (
    StubTerraformAdapter() if settings.USE_STUB_TERRAFORM else TerraformVcdAdapter()
)
config_runner = build_config_runner()
ansible_runner = build_ansible_runner()

# A reachable-but-failed configuration (bad script or Ansible run) keeps the VM (READY +
# config_failed); only an unreachable VM fails outright. Kept for test compatibility.
_CONFIG_SOFTWARE_ERRORS = (ConfigScriptError, AnsibleConfigError)


def _needs_configuration(booking: Any) -> bool:
    """Whether a provisioned VM has post-create configuration to run.

    True when a booking carries a ``startup_script`` or selected ``config_roles``. VMs with neither
    go PROVISIONING → READY unchanged.
    """
    return bool(getattr(booking, "startup_script", None)) or bool(
        getattr(booking, "config_roles", None)
    )


def _run(work: Callable[[Any], Any]) -> Any:
    """Run one unit of DB work in a short-lived session, releasing the connection at once.

    Keeps the worker from pinning a pool connection (and risking idle-in-transaction
    timeouts) across the minutes-long terraform apply — each write commits on its own.
    """
    with SyncSessionLocal() as session:
        return work(session)


def _token_pool() -> list[str]:
    if settings.VCD_API_TOKENS:
        return [t.strip() for t in settings.VCD_API_TOKENS.split(",") if t.strip()]
    if settings.VCD_API_TOKEN:
        return [settings.VCD_API_TOKEN]
    return []


def _acquire_token(
    tokens: list[str], redis_client: Any, timeout: int = 60
) -> tuple[str, str]:
    """Poll until a slot is free; return (lock_key, token)."""
    max_parallel = settings.VCD_TOKEN_MAX_PARALLEL
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for i, token in enumerate(tokens):
            for slot in range(max_parallel):
                lock_key = f"vcd_token_lock:{i}:{slot}"
                if redis_client.set(
                    lock_key, "1", nx=True, ex=settings.VCD_TOKEN_LOCK_TTL
                ):
                    return lock_key, token
        time.sleep(5)
    raise RuntimeError(f"no VCD token available after {timeout}s")


class _NullLease:
    api_token: str | None = None

    def renew(self) -> None:
        pass


class _RedisCredentialLease:
    def __init__(self, redis_client: Any, lock_key: str, api_token: str) -> None:
        self.redis_client = redis_client
        self.lock_key = lock_key
        self.api_token = api_token

    def renew(self) -> None:
        self.redis_client.expire(self.lock_key, settings.VCD_TOKEN_LOCK_TTL)


class _AdapterCredentialSlot:
    def __init__(self, booking_id: str) -> None:
        self.booking_id = booking_id

    @contextmanager
    def acquire(self):
        tokens = _token_pool()
        use_semaphore = not settings.USE_STUB_TERRAFORM and bool(tokens)
        if not use_semaphore:
            yield _NullLease()
            return

        redis_client = redis_lib.Redis.from_url(settings.REDIS_URL)
        try:
            lock_key, api_token = _acquire_token(tokens, redis_client)
            logger.info(
                "Acquired token lock %s for booking %s", lock_key, self.booking_id
            )
        except RuntimeError as exc:
            logger.warning(
                "Token acquire timed out for booking %s, retrying",
                self.booking_id,
            )
            raise CredentialSlotUnavailable(str(exc)) from exc

        lease = _RedisCredentialLease(redis_client, lock_key, api_token)
        try:
            yield lease
        finally:
            try:
                redis_client.delete(lock_key)
                logger.info(
                    "Released token lock %s for booking %s",
                    lock_key,
                    self.booking_id,
                )
            except Exception as rel_exc:  # noqa: BLE001
                logger.warning(
                    "Failed to release token lock %s for booking %s: %s (lock will expire via TTL)",
                    lock_key,
                    self.booking_id,
                    rel_exc,
                )


class _AdapterApplier:
    def __init__(self, booking_id: str) -> None:
        self.booking_id = booking_id

    def apply(
        self,
        workspace_id: str,
        config: dict[str, Any],
        *,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        lock_client = (
            None if settings.USE_STUB_TERRAFORM else provisioning_lock.get_client()
        )
        if lock_client:
            provisioning_lock.acquire(lock_client, self.booking_id)
        try:
            return asyncio.run(
                terraform.apply(
                    workspace_id,
                    config,
                    api_token=api_token,
                    on_progress=on_progress,
                )
            )
        finally:
            if lock_client:
                provisioning_lock.release(lock_client, self.booking_id)


class _AdapterTeardownHandoff:
    def hand_off(self, booking_id: str, request_id: str | None = None) -> None:
        teardown_vm_task.delay(booking_id, request_id=request_id)


@celery_app.task(
    bind=True,
    max_retries=settings.PROVISION_MAX_RETRIES,
    default_retry_delay=settings.PROVISION_RETRY_DELAY,
    rate_limit=settings.PROVISION_RATE_LIMIT,
)
def provision_vm_task(
    self,
    booking_id: str,
    image_id: str,
    hw_config_id: str,
    request_id: str | None = None,
) -> None:
    request_id_token = request_id_ctx_var.set(request_id)
    try:
        booking_uuid = UUID(booking_id)
        image_uuid = UUID(image_id)
        hw_config_uuid = UUID(hw_config_id)
        is_last_attempt = self.request.retries >= self.max_retries

        service = ProvisionBooking(
            bookings=repo,
            images=image_repo,
            hw_configs=hw_config_repo,
            environments=env_repo,
            run=_run,
            applier=_AdapterApplier(booking_id),
            config_runner=config_runner,
            role_applier=ansible_runner,
            progress_sink_factory=lambda persist, label: recorder_from_settings(
                persist, label=label
            ),
            teardown=_AdapterTeardownHandoff(),
            run_configuration=not settings.USE_STUB_TERRAFORM,
            credential_slot=_AdapterCredentialSlot(booking_id),
            request_id=request_id,
        )
        try:
            service.execute(
                booking_uuid,
                image_uuid,
                hw_config_uuid,
                is_last_attempt=is_last_attempt,
            )
        except ProvisioningDeferred as exc:
            raise self.retry(exc=exc.cause)
        except ProvisioningAttemptFailed as exc:
            raise self.retry(exc=exc.cause)
        except Exception as exc:  # noqa: BLE001
            raise self.retry(exc=exc)
    finally:
        request_id_ctx_var.reset(request_id_token)
