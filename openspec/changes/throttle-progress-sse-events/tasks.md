## 1. Configuration

- [x] 1.1 Add `SSE_PROGRESS_COALESCE_MS: int = 750` to `app/config.py` (0 disables coalescing), and check that `settings.SSE_PROGRESS_COALESCE_MS` is `750` when nothing overrides it

## 2. Coalescer and publish API (`app/infrastructure/events.py`)

- [x] 2.1 Add `kind` (`"progress"` | `"lifecycle"`) to `_payload`. Make `publish_row_changed` / `apublish_row_changed` publish `kind="lifecycle"` without changing their signatures. Verify with a unit test that decodes the published JSON and asserts `booking_id`, `environment_id`, `kind`
- [x] 2.2 Implement `ProgressCoalescer` (leading + trailing per booking id, one lock, at most one armed timer per open window, **at most one publish per booking in flight: the window's timer is armed only after the publish returns, with delay `max(0, W - publish duration)`**, injectable `timer_factory` and `clock`, publish outside the lock, idle entries dropped when their window closes, timer callback re-checks under the lock that its entry is still current and pending) as described in design D2/D3. Verify with deterministic unit tests using a fake virtual-time scheduler (no `sleep`)
- [x] 2.3 Add `publish_progress_changed(*, booking_id, environment_id=None)` routed through a module-level coalescer built from `SSE_PROGRESS_COALESCE_MS`, with a pass-through when the value is `0`. Make `publish_row_changed` call `coalescer.cancel(booking_id)` before publishing. `cancel` forgets the booking's entry rather than restarting its window (design D2). Verify with unit tests for the 0-window pass-through, for lifecycle cancelling a pending trailing publish, and for the first progress line within W after a lifecycle publish going out immediately (leading edge)
- [x] 2.4 Keep every publish best-effort, including the one fired from the timer thread: exceptions are logged and swallowed, and the coalescer entry stays usable. Verify with a unit test where the Redis publish raises inside the trailing flush and a later progress publish for the same booking is still attempted

## 3. Repository wiring

- [x] 3.1 Switch `BookingRepository.sync_record_progress` to `publish_progress_changed`, still after the commit. Leave every other mutating method on `publish_row_changed`. Verify with `pytest tests/test_provisioning_log_view.py tests/test_sse_row_changed.py`
- [x] 3.2 Extend the autouse `mock_row_changed_publish` fixture in `tests/conftest.py` to also patch `booking_repo.publish_progress_changed`. Update the `sync_record_progress` assertions in `tests/test_sse_row_changed.py` to expect the progress mock and to confirm the lifecycle mock is *not* called. Verify with `pytest tests/` passing and no test reaching real Redis

## 4. Acceptance tests (spec scenarios)

- [x] 4.1 Burst: 100 `publish_progress_changed` calls for one booking across 1 s of fake time (W = 750 ms), then fire due timers. Assert at most 3 publishes, and that the last one comes after the final call (trailing edge)
- [x] 4.2 Burst then silence: after the burst, advance the fake clock by W and fire the timer. Assert exactly one trailing publish with `kind="progress"`, and none after that
- [x] 4.3 Independence: interleave bursts for bookings A and B. Assert each gets its own leading and trailing publish, each payload carries only its own id, and cancelling A leaves B's pending trailing publish intact
- [x] 4.4 Lifecycle immediacy: during a pending progress window, `publish_row_changed` for the same booking publishes immediately with `kind="lifecycle"`, and a timer that fires afterwards publishes nothing (stale-entry re-check). A progress line within W after that lifecycle publish goes out immediately
- [x] 4.5 Best-effort cancellation race: with a fake timer whose callback is paused after it has decided to flush, run a lifecycle publish, then let the callback finish. Assert at most one `kind="progress"` publish follows the lifecycle publish and no further trailing publish is armed
- [x] 4.6 Overlapping producers: two independent coalescer instances (standing in for two worker processes) bursting for the same booking each publish at most one progress notification per window plus their own trailing publish
- [x] 4.7 Real-thread smoke test: using the real `threading.Timer` with a short window (e.g. 50 ms) and a mocked Redis client, a burst yields a trailing publish within about 2 × W. Keep it fast (under 0.5 s) and not flaky
- [x] 4.8 Slow Redis (PR #447 review): a trailing publish blocks for longer than W while more progress arrives, then a lifecycle cancel happens. Assert at most one publish is ever in flight for the booking, at most one `kind="progress"` follows the lifecycle publish, and no timer is left armed. Also assert that a publish slower than W makes the next one start right after it returns, never concurrently. Verify both tests fail against the pre-fix coalescer

## 5. Runtime verification and docs

- [x] 5.1 Run the stack end-to-end (`docker compose up`, stub Terraform) with a logged-in `GET /events/stream` connection and `redis-cli SUBSCRIBE portal:row-changed` open. Create a real VM booking, then drive a burst of a few hundred to over a thousand progress lines through the real `BookingRepository.sync_record_progress` from inside the worker container, both after READY and overlapping the stub apply. (The stub adapter emits a single progress line and skips startup scripts/Ansible, so it can't produce a burst by itself.) Confirm only a few `kind: progress` messages per second for that booking, that the final SSE render shows the last line, and that the lifecycle messages (status-message clear, READY) arrive immediately mid-burst. If Docker isn't available, say so explicitly and ask the user to verify
- [x] 5.2 Document `SSE_PROGRESS_COALESCE_MS` in `docs/admin-guide.md` (live-updates / SSE section: purpose, default, `0` to disable, worker restart needed), and check that the section renders and matches the setting name in `app/config.py`
- [x] 5.3 Run the `py-review` skill on the changed Python files and resolve its findings
