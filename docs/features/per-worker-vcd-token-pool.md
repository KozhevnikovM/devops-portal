# Feature: VCD Token Pool with Redis Semaphore

## Goal

Allow multiple Celery workers to provision VMs in parallel, each holding an exclusive lock on
one VCD API token, so N VMs can be provisioned concurrently without hitting VCD rate limits.

With 2 tokens and 2 workers, throughput doubles: both workers apply simultaneously, each on its
own token. Tasks beyond the pool size queue normally and pick up a token as soon as one is free.

---

## Approach

Each provisioning task acquires a **per-token Redis lock** before calling the VCD adapter.
At most one task holds each lock at a time. If all tokens are busy the task polls until one
frees (up to a configurable timeout), then raises `Retry` so Celery requeues it.

Workers are **stateless** — any worker can use any token. No per-container env vars.
`docker-compose.yml` stays as a single `worker` service scalable with `--scale worker=N`.

---

## What Changes

### `app/config.py`
```python
VCD_API_TOKENS: str = ""   # comma-separated list; overrides VCD_API_TOKEN when set
VCD_TOKEN_LOCK_TTL: int = 900   # Redis lock TTL in seconds (safety valve for crashed workers)
```
`VCD_API_TOKEN` (single token) remains as fallback for single-token setups.

### `app/infrastructure/terraform/vcd_adapter.py`
`apply()` accepts an optional `api_token` override; `_provider_block()` uses it when provided:

```python
async def apply(self, workspace_id: str, config: dict, api_token: str | None = None) -> dict:
    ...

def _provider_block(self, api_token: str | None = None) -> str:
    token = api_token or settings.VCD_API_TOKEN
    ...
```

`destroy()` unchanged (uses `settings.VCD_API_TOKEN`; dedicated destroy task added later).

### `app/infrastructure/terraform/adapter.py` (Protocol)
```python
async def apply(self, workspace_id: str, config: dict, api_token: str | None = None) -> dict
```

### `app/infrastructure/terraform/stub_adapter.py`
Add `api_token: str | None = None` to `apply()` — parameter ignored.

### `app/tasks/provision.py`
New `_acquire_token()` helper and semaphore release in `finally`:

```python
def _token_pool() -> list[str]:
    if settings.VCD_API_TOKENS:
        return [t.strip() for t in settings.VCD_API_TOKENS.split(",") if t.strip()]
    if settings.VCD_API_TOKEN:
        return [settings.VCD_API_TOKEN]
    return []

def _acquire_token(tokens: list[str], redis_client) -> tuple[int, str]:
    """Try each token in order; return (index, token) for the first lock acquired."""
    deadline = time.monotonic() + 60   # try for up to 60s before giving up
    while time.monotonic() < deadline:
        for i, token in enumerate(tokens):
            if redis_client.set(f"vcd_token_lock:{i}", "1", nx=True, ex=settings.VCD_TOKEN_LOCK_TTL):
                return i, token
        time.sleep(5)
    raise RuntimeError("no VCD token available")
```

Task acquires lock → runs terraform → releases lock in `finally`. On `RuntimeError` the task
raises `self.retry()` so Celery requeues it after `default_retry_delay`.

Stub mode (`USE_STUB_TERRAFORM=true`): token pool is empty; `_acquire_token` is never called.

### `docker-compose.yml`
No structural change — single `worker` service, same as today. Scale with:
```
docker compose up --scale worker=2
```

### `.env.example`
```
# Token pool — set multiple tokens to allow parallel provisioning
# VCD_API_TOKENS=token-a,token-b,token-c
# VCD_TOKEN_LOCK_TTL=900
```

### `docs/admin-guide.md`
New "Parallel provisioning with a token pool" section explaining:
- How to obtain N VCD API tokens
- Setting `VCD_API_TOKENS`
- Scaling workers: `docker compose up --scale worker=N`
- Recommended: workers ≤ token count (extra workers just wait for locks)

---

## Expected Behaviour

| Scenario | Behaviour |
|----------|-----------|
| 2 tokens, 2 workers, 10 tasks queued | 2 VMs provisioned in parallel; tasks 3–10 queue and acquire locks as they free |
| All tokens locked, task times out (60 s) | Task raises `Retry`; requeued after `default_retry_delay` |
| Worker crashes mid-apply | Lock expires after `VCD_TOKEN_LOCK_TTL` seconds; next task can acquire it |
| `VCD_API_TOKENS` empty, `VCD_API_TOKEN` set | Single-token fallback; behaves exactly as today |
| `USE_STUB_TERRAFORM=true` | Semaphore skipped entirely; stub runs without token |
| Username/password auth | `VCD_API_TOKENS` empty; unchanged behaviour |

---

## Out of Scope

- Token rotation / refresh (VCD tokens are long-lived refresh tokens)
- Per-token rate limiting beyond mutual exclusion
- Destroy task token selection (addressed when destroy endpoint is added)
