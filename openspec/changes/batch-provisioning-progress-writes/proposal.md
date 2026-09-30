## Why

Every progress callback from a provisioning or teardown task (Ansible output, startup-script output, SSH-wait messages, Terraform progress) opens a fresh DB session, loads the booking, appends to `provisioning_log`, sets `status_message`, and commits (`BookingRepository.sync_record_progress`, called from `_on_progress` in `app/tasks/provision.py` and `app/tasks/teardown.py`). Ansible and startup scripts call it once per output line, so a noisy role produces one UPDATE + COMMIT per line against the shared PostgreSQL. #435/#440 throttled the SSE *notifications* for these writes, but not the writes themselves (#444). The write load should be measured, not guessed. It is the P1 follow-up after #493 on the performance tracker (#498).

## What Changes

- Introduce a per-attempt **progress recorder** that buffers progress callbacks in memory and persists them in batched commits:
  - The first callback after a quiet period is persisted at once (leading edge), so an isolated line is as fresh as today.
  - Later callbacks within the flush interval (default **500 ms**, `PROGRESS_FLUSH_INTERVAL_MS`) are buffered and persisted together, at most once per interval, by a trailing flush that fires even if no further callback arrives.
  - A buffer that reaches **50 messages** (`PROGRESS_FLUSH_MAX_MESSAGES`) or **16,384 characters** (`PROGRESS_FLUSH_MAX_CHARS`) is flushed right away. The buffered log text is also tail-capped at the existing 50,000-character log cap, so a single huge message or unbroken output cannot grow the buffer without limit.
  - `PROGRESS_FLUSH_INTERVAL_MS=0` persists every callback as it arrives (today's behaviour).
- A flush applies the batch in **one atomic UPDATE**. It appends the buffered lines to `provisioning_log`, keeping the existing 50,000-character tail cap, and sets `status_message` to the last buffered message. The resulting log is identical to committing each line on its own: same order, same content, multi-line snapshots unchanged, no de-duplication.
- The progress `status_message` is only written while the booking is still in a status that accepts that producer's progress (provisioning: `PROVISIONING`/`CONFIGURING`; teardown: `RELEASING`). A late flush can therefore never overwrite a READY/FAILED/RETRY/configuration-error message. Log lines are still appended.
- Tasks flush the recorder (a **barrier**) before every status-message write, status transition, controlled failure/retry and the release-during-provisioning early return. The recorder is closed at the end of every attempt. A closed recorder cancels its timer and accepts nothing, so no buffered state is carried into a later attempt.
- Progress persistence becomes **best-effort**. A failed flush is logged and never raises into Terraform/SSH/Ansible. A failed in-run flush (leading-edge, trailing or size-triggered) keeps its batch, bounded by the log cap, and retries it no sooner than one interval later. A failed barrier flush drops its buffer so the lifecycle write can go ahead without being reordered. **Behaviour change:** today a DB error while recording a progress line fails the provisioning attempt.
- The progress row-changed notification is published once per successful flush, after the commit. The #440 coalescer and routing are unchanged, and lifecycle notifications stay immediate.
- The recorder holds no DB session between flushes. Each flush uses its own short-lived session, as `_run` does today. The VCD token-lock TTL refresh still runs on every callback.
- Crash semantics are documented. A hard kill (SIGKILL, OOM, worker loss) can lose the unflushed tail. In normal operation that tail is at most one flush interval of output, within the size thresholds above. While flushes are failing and batches are being retained, it is at most 50,000 characters of log text (the log cap). Handled exceptions always pass through a barrier flush.
- Measure commits and DB time for the same deterministic burst before and after, and record the numbers.

## Capabilities

### New Capabilities
- `progress-persistence`: how provisioning/teardown progress output is persisted. Covers batching bounds, freshness, the log cap, ordering, barriers around lifecycle writes, attempt isolation, flush-failure behaviour and crash-loss bounds.

### Modified Capabilities
- `live-row-updates`: "Progress notifications are coalesced per booking" currently says every progress line is persisted (implicitly one commit per line). It changes to say every line's content is persisted in batched commits, and that one progress notification follows each successful flush.

## Impact

- **Code**: new `app/infrastructure/progress_recorder.py`, a threaded, clock-injectable recorder with no DB knowledge. `BookingRepository.sync_record_progress` is replaced by a batch method taking the log chunk, the last message and the statuses that accept it, and `SyncBookingRepositoryPort` is updated to match. `app/tasks/provision.py` and `app/tasks/teardown.py` wire the recorder and add barriers. A shared log-cap constant replaces the literal `50_000`. New settings go in `app/config.py`.
- **DB**: no schema change or migration. Fewer, larger UPDATEs.
- **Docs**: `docs/admin-guide.md` gets the three new settings, both crash-loss bounds (normal and degraded), and the note that `SSE_PROGRESS_COALESCE_MS` now applies per flush.
- **Tests**: unit tests for the recorder using a fake clock and timer. Task-level tests for completion, configuration failure, Terraform/SSH failure, retry, release-during-provisioning early return, idle freshness and flush failure. A Postgres integration test for the atomic append and status guard. A measured before/after burst.
- **Not in scope**: SSE fan-out (#435), changes to the progress callbacks' own throttling (e.g. the VCD adapter's 15 s push), and the provisioning log's size or schema.
