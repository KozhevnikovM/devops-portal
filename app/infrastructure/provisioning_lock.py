"""Redis-backed marker: "a terraform apply is currently in flight for this booking" (#394).

`teardown_vm_task` must never call `terraform.destroy()` while the matching `provision_vm_task`
still holds this lock. Terraform's own state lock isn't sufficient protection on its own — the
destroy path's stale-lock recovery (`TerraformVcdAdapter._destroy_state`, see
docs/bugfix/teardown-force-state-unlock.md and docs/bugfix/teardown-force-unlock-robustness.md)
can't tell a lock held by a live sibling task from one abandoned by a dead one, and force-unlocks
either way — so without this marker, releasing a booking while it's still provisioning races
`destroy` against the in-flight `apply` on the same workspace.

The TTL reuses `VCD_TOKEN_LOCK_TTL` — both represent "how long to tolerate a stale in-flight
marker before assuming its holder died" (worker OOM-killed, container killed, etc.).
"""
import redis as redis_lib

from app.config import settings

_KEY_PREFIX = "provisioning-lock:"


def _key(booking_id: str) -> str:
    return f"{_KEY_PREFIX}{booking_id}"


def get_client() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(settings.REDIS_URL)


def acquire(redis_client, booking_id: str) -> None:
    redis_client.set(_key(booking_id), "1", ex=settings.VCD_TOKEN_LOCK_TTL)


def release(redis_client, booking_id: str) -> None:
    redis_client.delete(_key(booking_id))


def is_held(redis_client, booking_id: str) -> bool:
    return bool(redis_client.exists(_key(booking_id)))
