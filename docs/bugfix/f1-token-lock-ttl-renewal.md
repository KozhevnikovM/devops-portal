# Bugfix F-1 — VCD token-lock TTL expires during long terraform apply

## Root cause

`_acquire_token` in `app/tasks/provision.py` sets the Redis lock key with a fixed TTL
(`settings.VCD_TOKEN_LOCK_TTL`, default 900 s) via `redis_client.set(..., ex=...)` and never
renews it. `_on_progress` is called by `terraform.apply()` roughly every 15 seconds during an
apply, but was not renewing the lock TTL. An apply that runs longer than 900 s silently frees the
token slot while still using the token — a second concurrent task can then claim the same slot,
exceeding `VCD_TOKEN_MAX_PARALLEL` and risking API-level throttling or session collisions on VCD.

## What changes

`_on_progress` in `provision.py`: add `redis_client.expire(lock_key, settings.VCD_TOKEN_LOCK_TTL)`
as the first statement, guarded by `if redis_client and lock_key` (no-op in stub/dev mode where
the semaphore is not used).

## Expected behaviour after fix

The lock TTL is reset to `VCD_TOKEN_LOCK_TTL` on every progress callback (~15 s), so a long apply
never silently releases the token slot mid-provisioning.
