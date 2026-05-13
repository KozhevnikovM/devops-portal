# Bugfix: Concurrent VM creation fails due to VCD token rate limit (Issue #23)

## Root Cause

All `provision_vm_task` instances share the same `VCD_API_TOKEN`. The VCD
provider enforces a rate limit that allows only one authentication per ~2
minutes per token. With worker concurrency set to 4 (`-c 4`), two bookings
created in quick succession both start provisioning immediately, both attempt
to authenticate with the same token within milliseconds of each other, and the
second one is rejected by the VCD API.

The existing retry delay of 10 s is far too short — the task retries 3× within
30 s while the token is still rate-limited, exhausting all retries and marking
the booking FAILED.

## What Changes

**`app/tasks/provision.py`** — two changes to the task decorator:
- `rate_limit="0.5/m"` — Celery will not start more than one provision task
  per 2 minutes per worker process. This is the first line of defence.
- `default_retry_delay=120` — if the task still fails (e.g. transient VCD
  error), the retry waits 2 minutes before the next attempt, matching the
  token cooldown window.

**`docker-compose.yml`** — change worker concurrency from `-c 4` to `-c 1`.
With a single concurrent slot, the rate limit is fully enforced: the worker
picks up the next provisioning task only after the current one finishes, and
Celery's per-worker rate limiter (`0.5/m`) ensures at least 2 minutes between
consecutive task starts.

## Expected Behaviour After Fix

- Two simultaneous bookings are queued; the second waits until 2 minutes after
  the first task started before it is picked up by the worker.
- If the VCD API still rejects a request, the task retries after 120 s (up to
  3 times) instead of hammering the API every 10 s.

## Trade-off

Reducing concurrency to 1 means only one VM is provisioned at a time. For this
deployment (single shared API token with a hard rate limit) this is the correct
constraint. If multiple tokens become available in future, run additional worker
containers — one per token — each with `-c 1`.

## No DB migrations required
