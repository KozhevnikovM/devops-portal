import asyncio
import logging
import secrets
import string
import time
from uuid import UUID

import redis as redis_lib

from app.config import settings
from app.domain.enums import BookingStatus
from app.domain.exceptions import SecretDecryptionError
from app.infrastructure.celery_app import celery_app
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.config.ansible import AnsibleConfigError, build_ansible_runner
from app.infrastructure.config.runner import ConfigScriptError, build_config_runner
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter

logger = logging.getLogger(__name__)

repo = BookingRepository()
env_repo = EnvironmentRepository()
image_repo = ImageRepository()
hw_config_repo = HWConfigRepository()
terraform = StubTerraformAdapter() if settings.USE_STUB_TERRAFORM else TerraformVcdAdapter()
config_runner = build_config_runner()
ansible_runner = build_ansible_runner()

# A reachable-but-failed configuration (bad script or Ansible run) keeps the VM (READY +
# config_failed); only an unreachable VM fails outright.
_CONFIG_SOFTWARE_ERRORS = (ConfigScriptError, AnsibleConfigError)


def _needs_configuration(booking) -> bool:
    """Whether a provisioned VM has post-create configuration to run.

    True when a booking carries a ``startup_script`` or selected ``config_roles``. VMs with neither
    go PROVISIONING → READY unchanged.
    """
    return bool(getattr(booking, "startup_script", None)) or bool(getattr(booking, "config_roles", None))


def _run(work):
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


def _acquire_token(tokens: list[str], redis_client, timeout: int = 60) -> tuple[str, str]:
    """Poll until a slot is free; return (lock_key, token)."""
    max_parallel = settings.VCD_TOKEN_MAX_PARALLEL
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for i, token in enumerate(tokens):
            for slot in range(max_parallel):
                lock_key = f"vcd_token_lock:{i}:{slot}"
                if redis_client.set(lock_key, "1", nx=True, ex=settings.VCD_TOKEN_LOCK_TTL):
                    return lock_key, token
        time.sleep(5)
    raise RuntimeError(f"no VCD token available after {timeout}s")


@celery_app.task(
    bind=True,
    max_retries=settings.PROVISION_MAX_RETRIES,
    default_retry_delay=settings.PROVISION_RETRY_DELAY,
    rate_limit=settings.PROVISION_RATE_LIMIT,
)
def provision_vm_task(self, booking_id: str, image_id: str, hw_config_id: str) -> None:
    booking_uuid = UUID(booking_id)
    workspace_id = f"booking-{booking_id}"

    tokens = _token_pool()
    use_semaphore = not settings.USE_STUB_TERRAFORM and bool(tokens)

    lock_key = None
    redis_client = None
    api_token = None

    if use_semaphore:
        redis_client = redis_lib.Redis.from_url(settings.REDIS_URL)
        try:
            lock_key, api_token = _acquire_token(tokens, redis_client)
            logger.info("Acquired token lock %s for booking %s", lock_key, booking_id)
        except RuntimeError as exc:
            logger.warning("Token acquire timed out for booking %s, retrying", booking_id)
            raise self.retry(exc=exc)

    try:
        try:
            # Read config in a short session; no session is held during the apply below.
            image = _run(lambda s: image_repo.sync_get(s, UUID(image_id)))
            hw = _run(lambda s: hw_config_repo.sync_get(s, UUID(hw_config_id)))
            vm_password = "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
            )
            config = {
                "name":             f"portal-{booking_id[:8]}",
                "vapp_template_id": image.vapp_template_id,
                "cpus":             hw.cpus,
                "memory":           hw.memory_mb,
                "disk_size":        hw.disk_mb,
                "vm_password":      vm_password,
            }

            _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.PROVISIONING))
            logger.info("Provisioning started for booking %s", booking_id)

            def _on_progress(msg: str) -> None:
                # Each progress write gets its own short-lived session/connection.
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, msg))

            # No DB connection is held across the (minutes-long) terraform apply.
            result = asyncio.run(
                terraform.apply(workspace_id, config, api_token=api_token, on_progress=_on_progress)
            )
            ip = result["ip"]
            _run(lambda s: repo.sync_set_status_message(s, booking_uuid, None))

            # ── Post-provision: wait for SSH reachability, then run the startup script ──
            # Two distinct outcomes: an unreachable VM is an infra failure (raises → FAILED); a
            # reachable VM whose script fails is still usable → READY with config_failed=True.
            booking = _run(lambda s: repo.sync_get(s, booking_uuid))
            config_failed = False
            config_message = None
            if not settings.USE_STUB_TERRAFORM:
                _run(lambda s: repo.sync_update_status(
                    s, booking_uuid, BookingStatus.CONFIGURING, vm_ip=ip, vm_password=vm_password
                ))
                _on_progress(f"Waiting for {ip} to become reachable…")
                client = config_runner.connect(ip, vm_password, on_progress=_on_progress)  # VmUnreachableError → FAILED
                try:
                    # Reachable: run the bash startup script (if any), then apply Ansible roles
                    # (if any). Either software failure keeps the VM (READY + config_failed).
                    if booking.startup_script:
                        logger.info("Running startup script for booking %s — IP: %s", booking_id, ip)
                        config_runner.run_script(client, booking.startup_script, on_progress=_on_progress)
                    if booking.config_roles:
                        logger.info("Applying %d role(s) for booking %s", len(booking.config_roles), booking_id)
                        ansible_runner.apply_roles(
                            booking, ip=ip, password=vm_password, on_progress=_on_progress,
                            extra_vars=booking.extra_vars or {},
                            label=booking.environment_label or "",
                        )
                except _CONFIG_SOFTWARE_ERRORS as cfg_exc:
                    # VM is up but configuration failed — keep the VM, flag the failure.
                    config_failed = True
                    config_message = str(cfg_exc)
                    logger.warning("Configuration failed for booking %s: %s", booking_id, cfg_exc)
                finally:
                    config_runner.close(client)
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, config_message))

            _run(lambda s: repo.sync_update_status(
                s, booking_uuid, BookingStatus.READY,
                vm_ip=ip, vm_password=vm_password, config_failed=config_failed,
                start_lease=True,  # #223 — the lease starts now that the VM is usable
            ))
            # If this VM is part of an environment, the lease for the whole stack starts once every
            # child is READY — the last one to finish stamps the environment + all its children.
            _run(lambda s: env_repo.sync_start_lease_if_ready_for_booking(s, booking_uuid))
            logger.info(
                "Provisioning complete for booking %s — IP: %s (config_failed=%s)",
                booking_id, ip, config_failed,
            )

        except SecretDecryptionError as exc:
            # Permanent configuration error — wrong/missing SECRETS_ENCRYPTION_KEY.
            # Do not retry; retries would all fail identically and delay the failure signal.
            logger.error("Secret decryption failed for booking %s (not retrying): %s", booking_id, exc)
            try:
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, f"Secret decryption failed: {exc}"))
                _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.FAILED))
            except Exception:
                pass
            return
        except Exception as exc:
            logger.error("Provisioning failed for booking %s: %s", booking_id, exc)
            is_last_attempt = self.request.retries >= self.max_retries
            new_status = BookingStatus.FAILED if is_last_attempt else BookingStatus.RETRY
            try:
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, "Failed — see audit log"))
                _run(lambda s: repo.sync_update_status(s, booking_uuid, new_status))
            except Exception:
                pass
            raise self.retry(exc=exc)
    finally:
        if redis_client and lock_key:
            redis_client.delete(lock_key)
            logger.info("Released token lock %s for booking %s", lock_key, booking_id)
