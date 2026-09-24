## 1. Configuration

- [ ] 1.1 Add `SSE_PROGRESS_COALESCE_MS: int = 750` to `app/config.py` (0 disables coalescing), and check that `settings.SSE_PROGRESS_COALESCE_MS` is `750` when nothing overrides it

## 2. Coalescer and publish API (`app/infrastructure/events.py`)

- [ ] 2.1 Add `kind` (`"progress"` | `"lifecycle"`) to `_payload`. Make `publish_row_changed` / `apublish_row_changed` publish `kind="lifecycle"` without changing their signatures. Verify with a unit test that decodes the published JSON and asserts `booking_id`, `environment_id`, `kind`
- [ ] 2.2 Implement `ProgressCoalescer` (leading + trailing per booking id, one lock, at most one timer per booking, injectable `clock` and `timer_factory`, publish outside the lock, idle entries dropped) as described in design D2/D3. Verify with deterministic unit tests using a fake clock and fake timers (no `sleep`)
- [ ] 2.3 Add `publish_progress_changed(*, booking_id, environment_id=None)` routed through a module-level coalescer built from `SSE_PROGRESS_COALESCE_MS`, with a pass-through when the value is `0`. Make `publish_row_changed` call `coalescer.cancel(booking_id)` before publishing. Verify with unit tests for the 0-window pass-through and for lifecycle cancelling a pending trailing publish
- [ ] 2.4 Keep every publish best-effort, including the one fired from the timer thread: exceptions are logged and swallowed, and the coalescer entry stays usable. Verify with a unit test where the Redis publish raises inside the trailing flush and a later progress publish for the same booking is still attempted

## 3. Repository wiring

- [ ] 3.1 Switch `BookingRepository.sync_record_progress` to `publish_progress_changed`, still after the commit. Leave every other mutating method on `publish_row_changed`. Verify with `pytest tests/test_provisioning_log_view.py tests/test_sse_row_changed.py`
- [ ] 3.2 Extend the autouse `mock_row_changed_publish` fixture in `tests/conftest.py` to also patch `booking_repo.publish_progress_changed`. Update the `sync_record_progress` assertions in `tests/test_sse_row_changed.py` to expect the progress mock and to confirm the lifecycle mock is *not* called. Verify with `pytest tests/` passing and no test reaching real Redis

## 4. Acceptance tests (spec scenarios)

- [ ] 4.1 Burst: 100 `publish_progress_changed` calls for one booking across 1 s of fake time (W = 750 ms), then fire due timers. Assert at most 3 publishes, and that the last one comes after the final call (trailing edge)
- [ ] 4.2 Burst then silence: after the burst, advance the fake clock by W and fire the timer. Assert exactly one trailing publish with `kind="progress"`, and none after that
- [ ] 4.3 Independence: interleave bursts for bookings A and B. Assert each gets its own leading and trailing publish, each payload carries only its own id, and cancelling A leaves B's pending trailing publish intact
- [ ] 4.4 Lifecycle immediacy: during a pending progress window, `publish_row_changed` for the same booking publishes immediately with `kind="lifecycle"`, and the pending trailing publish never fires
- [ ] 4.5 Real-thread smoke test: using the real `threading.Timer` with a short window (e.g. 50 ms) and a mocked Redis client, a burst yields a trailing publish within about 2 × W. Keep it fast (under 0.5 s) and not flaky

## 5. Runtime verification and docs

- [ ] 5.1 Run a stub-Terraform provisioning end-to-end (`docker compose up`) with the browser on the bookings page, and a startup script that prints about 200 lines quickly. Watch `redis-cli SUBSCRIBE portal:row-changed` and confirm only a few `kind: progress` messages per second for that booking, that the row ends up showing the last line, and that READY appears immediately. If Docker isn't available, say so explicitly and ask the user to verify
- [ ] 5.2 Document `SSE_PROGRESS_COALESCE_MS` in `docs/admin-guide.md` (live-updates / SSE section: purpose, default, `0` to disable, worker restart needed), and check that the section renders and matches the setting name in `app/config.py`
- [ ] 5.3 Run the `py-review` skill on the changed Python files and resolve its findings
