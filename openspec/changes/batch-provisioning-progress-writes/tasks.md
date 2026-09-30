## 1. Baseline measurement

- [ ] 1.1 Add `tests/integration/test_progress_write_amplification.py` (marked `integration`) that replays the D8 burst (2,000 three-line snapshots, fake clock +5 ms/line) through today's per-line `sync_record_progress` against Postgres, counting commits via an `after_commit` listener and timing DB work. Run it on `main`'s code and record the baseline commit count and DB time for the code PR description. Verify: it prints ~2,000 commits and a DB time.

## 2. Domain constants and settings

- [ ] 2.1 Add `PROVISIONING_LOG_MAX_CHARS = 50_000` to `app/domain/constants.py`. Add `PROVISIONING_PROGRESS_STATUSES = {PROVISIONING, CONFIGURING}` and `TEARDOWN_PROGRESS_STATUSES = {RELEASING}` frozensets to `app/domain/booking_status.py`. Verify: a domain unit test asserts their contents and that the domain layer still has no framework imports.
- [ ] 2.2 Add `PROGRESS_FLUSH_INTERVAL_MS=500`, `PROGRESS_FLUSH_MESSAGE_THRESHOLD=50` and `PROGRESS_FLUSH_CHAR_THRESHOLD=16384` to `app/config.py`, validated as non-negative, with both flush thresholds ≥ 1. Verify: a settings test covers the defaults and rejects negative values.

## 3. ProgressRecorder

- [ ] 3.1 Implement `app/infrastructure/progress_recorder.py` per design D1/D2: `record`/`flush`/`close`, one lock, leading-edge plus trailing timer, message/char thresholds, buffered text tail-capped at `PROVISIONING_LOG_MAX_CHARS`, `interval=0` pass-through, retry backoff of `max(interval, 0.5 s)` that also gates the `interval=0` pass-through, injectable `clock`/`timer_factory`. Verify: the unit tests in 3.2 pass.
- [ ] 3.2 Add `tests/test_progress_recorder.py` with a fake clock and a manually fired fake timer, covering:
  - an isolated line persists immediately;
  - a burst of N lines over T commits at most `ceil(T/0.5)+ceil(N/50)+1` times, and 1,000 lines with the clock frozen commit at most 21 times including the final flush;
  - burst then silence: the trailing timer persists the last message within one interval;
  - the batched chunk equals the per-line concatenation, with repeated multi-line snapshots preserved and not de-duplicated;
  - one message over 50,000 characters flushes immediately; the chunk and the last message passed to `persist` are each at most 50,000 characters (the tail), and the recorder keeps no reference to the original string;
  - continuous output never buffers more than the cap;
  - `interval=0` persists every record.

  Verify: `pytest tests/test_progress_recorder.py`.
- [ ] 3.3 Add failure and isolation tests to the same file:
  - a trailing or threshold flush failure retains the buffer, and the next flush persists the retained and new messages in order;
  - an oversized message (e.g. 5 MB) whose leading-edge persist fails: the retained log text and pending status message are each at most 50,000 characters, and the retry persists exactly the capped tails;
  - a failed leading-edge persist keeps the first message (it is not dropped), and the trailing timer retries it one retry delay later together with the messages recorded meanwhile;
  - `interval=0` with a persist that keeps failing: after the first failure, the timer is armed for 0.5 s (not 0), records during backoff make no persist calls, repeated failures cause at most one attempt per 0.5 s of fake-clock time, and after a success each record persists immediately again;
  - during retry backoff, records past the flush thresholds trigger no extra persist attempts (at most one per retry delay, checked with the fake clock); the buffer may exceed 50 messages and 16,384 characters, but its log text never exceeds 50,000 characters;
  - a barrier `flush()` failure drops the buffer and logs a warning, and nothing from it is persisted later;
  - `record` never raises when `persist` raises;
  - after `close()`, `record` is a no-op and a timer that fires late persists nothing;
  - `close()` is idempotent.

  Verify: `pytest tests/test_progress_recorder.py`.

## 4. Repository: atomic batched append

- [ ] 4.1 Replace `BookingRepository.sync_record_progress` with `sync_append_progress(session, booking_id, chunk, last_message, accepting)`: a single `UPDATE … SET provisioning_log = right(coalesce(...) || :chunk, :cap), status_message = CASE WHEN status IN :accepting …` with `RETURNING` of the routing columns. It commits, then calls `publish_progress_changed`, and raises `BookingNotFoundError` when no row matches. Update `SyncBookingRepositoryPort` to match. Verify: `grep -rn sync_record_progress app` returns nothing and mypy passes via the `py-review` skill.
- [ ] 4.2 Rewrite the `sync_record_progress` unit tests in `tests/test_provisioning_log_view.py` against `sync_append_progress` using a mock session. Cover: the statement is executed and committed once, the notification is published after the commit with the returned routing, there is no publish when the commit raises, and a missing row raises `BookingNotFoundError`. Verify: `pytest tests/test_provisioning_log_view.py`.
- [ ] 4.3 Add `tests/integration/test_progress_append.py` (Postgres), covering:
  - an append keeps the 50,000-character tail cap;
  - a batch equals the per-line result for the same messages;
  - `status_message` is set while PROVISIONING/CONFIGURING and left unchanged once READY/FAILED/RETRY/RELEASING for the provisioning status set, and likewise for the teardown set;
  - two concurrent appends from separate sessions both land in full.

  Verify: `pytest -m integration tests/integration/test_progress_append.py`.

## 5. Task wiring and barriers

- [ ] 5.1 In `app/tasks/provision.py`:
  - create one `ProgressRecorder` per execution after the PROVISIONING transition, with `persist = lambda chunk, last: _run(lambda s: repo.sync_append_progress(s, booking_uuid, chunk, last, PROVISIONING_PROGRESS_STATUSES))`;
  - keep the token-lock `expire` on every `_on_progress` call, then `recorder.record(msg)`;
  - add a `_lifecycle(work)` helper that flushes the recorder and then calls `_run` for every status-message, status-transition and env-lease write, on the success, configuration-failure, `SecretDecryptionError` and generic-exception/retry paths;
  - `recorder.close()` before the release-during-provisioning early return and in a `finally`.

  Verify: the tests in 5.3 pass.
- [ ] 5.2 Apply the same pattern in `app/tasks/teardown.py` with `TEARDOWN_PROGRESS_STATUSES`: barrier before clearing the status message, before the RELEASED/FAILED transitions and before the force-release writes; `close()` in `finally`. Verify: `pytest tests/test_teardown_task.py tests/test_teardown_session_lifetime.py`.
- [ ] 5.3 Add `tests/test_provision_progress_batching.py`, with the recorder built on a fake clock and timer and the repository mocked to log calls in order. Cover:
  - normal completion: all progress is appended before `set_status_message(None)` and before READY;
  - configuration failure: script/Ansible output is appended before the config-error message and READY;
  - Terraform failure and SSH-unreachable failure: output is appended before "Failed — see audit log" and RETRY/FAILED;
  - retry: attempt 2 persists no attempt-1 message, and attempt 1's timer fired after its return persists nothing;
  - release-during-provisioning early return: buffered output is flushed before `teardown_vm_task.delay` and the recorder is closed;
  - idle freshness: a single SSH-wait line persists immediately, and a burst then silence persists within one interval;
  - a progress DB failure mid-apply does not fail the attempt;
  - the token lock is refreshed on buffered callbacks.

  Verify: `pytest tests/test_provision_progress_batching.py`.
- [ ] 5.4 Update the existing provisioning and teardown tests that asserted one `sync_record_progress` call per line (`tests/test_provisioning_progress.py`, `tests/test_provision_task.py`, `tests/test_release_during_provisioning.py`, `tests/test_*_session_lifetime.py`, and any others found by grep). Keep the session-lifetime tests asserting that no session is open during apply/destroy or between flushes. Verify: `pytest tests/ -m "not integration"` is green.

## 6. Measurement, docs and quality gate

- [ ] 6.1 Point the amplification test from 1.1 at the new path. Run it with `PROGRESS_FLUSH_INTERVAL_MS=0` and with the defaults on the same burst, assert that the batched commits are at most `ceil(10/0.5)+ceil(2000/50)+2` and that the final logs are identical, and record the before/after commits and DB time in the code PR description and in a table in `design.md` (D8). Verify: `pytest -m integration tests/integration/test_progress_write_amplification.py -s` prints both runs.
- [ ] 6.2 Update `docs/admin-guide.md`:
  - document the three new settings with defaults (calling the two count settings flush thresholds, not buffer limits, and naming the two 50,000-character hard bounds, on buffered log text and on the status message; also note that a single progress message longer than 50,000 characters now has its status message cut to its tail), freshness and crash-loss implications, and the `PROGRESS_FLUSH_INTERVAL_MS=0` rollback lever (one commit per callback while the database is healthy, with retries at most every 500 ms after a failure);
  - correct the `SSE_PROGRESS_COALESCE_MS` row ("every line is still saved", now in batched commits);
  - state both SIGKILL loss bounds, using the spec's wording: at most one flush interval of output (below the flush thresholds) in normal operation, and at most 50,000 log characters while flushes are failing.

  Check `docs/api-reference.md` for any mention of per-line progress persistence. Verify: the docs diff is reviewed in the PR.
- [ ] 6.3 Run the `py-review` skill (ruff, mypy, bandit) on the changed Python files and fix any findings. Verify: a clean `py-review` report.
- [ ] 6.4 Runtime check: run the stack with `docker compose up`, order a VM with a noisy startup script (e.g. `for i in $(seq 1 2000); do echo line $i; done`), and confirm that the row updates live, the provisioning log shows all lines in order within the cap, READY's status message is not overwritten, and the Postgres `xact_commit` delta for the run is far below the line count. Verify: observations noted in the code PR.
