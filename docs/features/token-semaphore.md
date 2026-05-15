# Feature #49 — Configurable parallel jobs per token (`VCD_TOKEN_MAX_PARALLEL`)

## Goal

Allow more than one concurrent provisioning job per VCD API token.

Previously the Redis semaphore enforced a strict one-job-per-token mutex. With this change,
operators can set `VCD_TOKEN_MAX_PARALLEL=N` to allow N concurrent jobs per token, without
changing any other behaviour.

---

## Background

When `VCD_API_TOKENS` is set and `USE_STUB_TERRAFORM=false`, the provisioning worker acquires a
per-token Redis lock before calling `terraform apply`. Each token had exactly one lock slot,
so only one job could use it at a time.

Some VCD environments can safely handle concurrent API calls on the same token. The hard-coded
mutex had no way to express that.

---

## Changes

### New config setting (`app/config.py`)

| Setting | Type | Default | Description |
|---|---|---|---|
| `VCD_TOKEN_MAX_PARALLEL` | int | `1` | Max concurrent provisioning jobs per token |

Default of `1` preserves the existing one-job-per-token behaviour.

### Lock key format (`app/tasks/provision.py`)

Changed from `vcd_token_lock:{token_idx}` to `vcd_token_lock:{token_idx}:{slot}`.

With `VCD_TOKEN_MAX_PARALLEL=N`, each token gets N slots. `_acquire_token` iterates
all `(token, slot)` pairs and returns the first free one.

---

## Expected behaviour

| `VCD_TOKEN_MAX_PARALLEL` | Behaviour |
|---|---|
| `1` (default) | One job per token — unchanged from before |
| `2` | Two concurrent jobs per token allowed |

### Example: 3 tokens × 2 parallel jobs = 6 concurrent VMs

```bash
VCD_API_TOKENS=token-a,token-b,token-c
VCD_TOKEN_MAX_PARALLEL=2
```

Lock slots: `0:0`, `0:1`, `1:0`, `1:1`, `2:0`, `2:1` — 6 total.
Scale workers to match: `docker compose up -d --scale worker=6`.

---

## Backward compatibility

- Existing deployments with no new setting behave identically.
- Lock key format changes from `vcd_token_lock:N` to `vcd_token_lock:N:0`. Stale locks
  from the old format expire naturally via `VCD_TOKEN_LOCK_TTL` and cause no harm.
