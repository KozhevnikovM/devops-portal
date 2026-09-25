## 1. Routing rule

- [x] 1.1 Add a pure helper in `app/presentation/routes/events.py` that maps a decoded payload to the ordered list of rows to refresh: the booking row always, if `booking_id` is set, and the environment row only when `environment_id` is set and `kind != "progress"`. Verify with the unit tests in 2.1.
- [x] 1.2 Make `_event_stream` iterate the helper's result and call the existing `_render_booking_event` / `_render_environment_event`, keeping the booking-then-environment order and the per-connection `can_manage()` checks. Verify that the existing `tests/test_events_stream.py` tests still pass.
- [x] 1.3 Add a short comment in `partials/environment_row.html` saying the row must not display progress-driven child fields (`status_message`, `provisioning_log`) without revisiting the rule. Verify by review.

## 2. Tests

- [x] 2.1 In `tests/test_events_stream.py`, add routing tests for the helper: progress/child → booking only; progress/standalone → booking only; lifecycle/child → booking + environment; lifecycle/standalone → booking only; no kind/child and unknown kind/child → booking + environment. Verify with `pytest tests/test_events_stream.py`.
- [x] 2.2 Add a stream-level test that feeds `_event_stream` a fake pub/sub yielding one `progress` message for an environment child, with the render functions patched, and asserts `_render_environment_event` is never awaited. Add the lifecycle counterpart and assert that it is awaited. Drive the generator for one message and then close it. Verify with `pytest tests/test_events_stream.py`.
- [ ] 2.3 Run the full suite and verify it passes: `pytest tests/`.

## 3. Docs & quality

- [x] 3.1 Update `docs/api-reference.md` (`GET /events/stream`) to note that progress output refreshes only the booking row, while status changes also refresh the parent environment row. Verify by review.
- [x] 3.2 Run the `py-review` skill on the changed Python files and verify it reports no new findings.
- [ ] 3.3 Runtime check: with the stack up, order a small environment whose child runs a startup script or Ansible role. With the browser network panel open, confirm that `environment-<id>` SSE events arrive only on child status changes, not on each progress line.
