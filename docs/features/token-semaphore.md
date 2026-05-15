# Feature #49 — Configurable Redis Token Semaphore

## Goal

Give operators two knobs to tune how the VCD token pool semaphore behaves:

1. **`VCD_TOKEN_SEMAPHORE`** — disable Redis locking entirely, even when a token pool is configured.
2. **`VCD_TOKEN_MAX_PARALLEL`** — allow more than one concurrent provisioning job per token.

Both default to the existing behaviour (`true` / `1`), so no changes are required for existing deployments.

---

## Background

When `VCD_API_TOKENS` is set and `USE_STUB_TERRAFORM=false`, the provisioning worker acquires a
per-token Redis lock before calling `terraform apply`. This prevents two workers from using the
same token simultaneously.

Two gaps existed:

- **No way to disable locking** — operators using username/password auth or running a single worker
  with no contention had no escape hatch if Redis was unavailable or undesirable.
- **Max one job per token** — a VCD environment that can handle concurrent calls on the same token
  had no way to express that.

---

## Changes

### New config settings (`app/config.py`)

| Setting | Type | Default | Description |
|---|---|---|---|
| `VCD_TOKEN_SEMAPHORE` | bool | `true` | Set `false` to skip Redis locking even when `VCD_API_TOKENS` is configured |
| `VCD_TOKEN_MAX_PARALLEL` | int | `1` | Max concurrent provisioning jobs per token |

### Semaphore logic (`app/tasks/provision.py`)

`use_semaphore` condition extended:
```python
use_semaphore = (
    not settings.USE_STUB_TERRAFORM
    and bool(tokens)
    and settings.VCD_TOKEN_SEMAPHORE
)
```

Lock key format changed from `vcd_token_lock:{token_idx}` to `vcd_token_lock:{token_idx}:{slot}`
to support multiple slots per token. With `VCD_TOKEN_MAX_PARALLEL=N`, each token gets N lock slots.
`_acquire_token` iterates all `(token, slot)` pairs and returns the first free one.

---

## Expected behaviour

| `VCD_TOKEN_SEMAPHORE` | `VCD_TOKEN_MAX_PARALLEL` | Result |
|---|---|---|
| `true` (default) | `1` (default) | One job per token — existing behaviour unchanged |
| `true` | `2` | Two concurrent jobs per token allowed |
| `false` | any | No locking; tokens are passed round-robin without coordination |

### Example: 3 tokens, 2 parallel jobs each → 6 concurrent VMs

```bash
VCD_API_TOKENS=token-a,token-b,token-c
VCD_TOKEN_MAX_PARALLEL=2
```

Lock slots created: `0:0`, `0:1`, `1:0`, `1:1`, `2:0`, `2:1` — 6 total.
Scale workers to match: `docker compose up -d --scale worker=6`.

---

## Backward compatibility

- Existing deployments with no new settings set behave identically.
- Lock key format changes from `vcd_token_lock:N` to `vcd_token_lock:N:0`. Any stale locks
  from the old format in Redis will expire naturally via `VCD_TOKEN_LOCK_TTL` and cause no harm.
