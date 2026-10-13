## Context

`app/tasks/provision.py` is today both the composition root and the whole lifecycle. At import it builds module-level collaborators:
- `repo`, `env_repo`, `image_repo`, `hw_config_repo`;
- `terraform`, `config_runner`, `ansible_runner`.

One `provision_vm_task` body then interleaves Celery and Redis plumbing with the lifecycle rules listed in `proposal.md`.

Several constraints shape the approach:
- **Behaviour is already specified in parts.** `progress-persistence` covers the flush barrier, ownership-guarded status messages and per-attempt buffers. `environment-lifecycle` covers lease start on settlement. `vm-provisioning-recovery` covers orphan reconciliation, which lives inside `TerraformVcdAdapter` and is untouched here. The new `vm-provisioning-lifecycle` spec covers the rest.
- **The existing tests are the regression net.** About 150 `patch("app.tasks.provision.<name>")` calls across 15 files, including the Postgres race test `tests/integration/test_provision_release_race.py`, patch these module globals: `SyncSessionLocal`, `repo`, `env_repo`, `image_repo`, `hw_config_repo`, `terraform`, `asyncio.run`, `config_runner`, `ansible_runner`, `provisioning_lock`, `teardown_vm_task`, `redis_lib`, `recorder_from_settings` and `settings.*`.
- **CLAUDE.md:** the application layer never imports infrastructure. Use cases depend on `ports.py` Protocols. Tasks call `asyncio.run` once per `TerraformAdapter` call. Each DB write runs in its own short-lived `SyncSessionLocal`.
- **CI (#433):** `ports.py` and `app/domain/` are Mypy-blocking, and every touched file must be fully Ruff-clean.

## Goals / Non-Goals

**Goals:**
- Every rule in `specs/vm-provisioning-lifecycle` is decided in one application-layer class, which unit tests can drive with plain fakes.
- `provision_vm_task` holds only the adapter concerns that the spec's last requirement lists.
- Existing tests pass with unchanged assertions. Only the two booking-read fixtures named in Decision 9 change.

**Non-Goals:**
- No change to `teardown_vm_task`. The same extraction for teardown is a follow-up, and the handoff port is shaped so teardown can reuse it.
- No change to `BookingRepository`. Its side effects stay as they are: audit rows, `_publish_lifecycle` events and the ownership-guarded `if_status_in` writes. Explicit post-commit publication is a separate #429 slice.
- No unit-of-work or transaction abstraction beyond the existing "run this in a short session" helper.
- No changes to retry counts, rate limits, the token semaphore algorithm (slot keys, TTL, polling) or the provisioning-lock semantics. Only *when* the slot is taken changes (Decision 9).

## Decisions

### 1. One application service, `ProvisionBooking`, in `app/application/use_cases/provision_booking.py`

It goes in `use_cases/` because it is one business operation, which matches the house rule of one operation per file. The issue calls it a service. The class is named `ProvisionBooking`, and its entry point is:

```python
def execute(self, booking_id: UUID, image_id: UUID, hw_config_id: UUID, *, is_last_attempt: bool) -> ProvisionOutcome
```

It is **sync**. Its collaborators are the sync repository methods, and it runs inside the Celery worker. A sync use case is new in this codebase; the module docstring will say so.

`ProvisionOutcome` is a small enum: `SKIPPED_RELEASED`, `HANDED_OFF`, `READY`, `FAILED_PERMANENTLY`. An infrastructure failure is not an outcome value, and neither is an unavailable credential slot. The service raises `ProvisioningDeferred(cause)` when the slot port gives up, and it writes nothing. The task maps it to `self.retry(exc=cause)` exactly like today's token-timeout path. The service records the RETRY or FAILED status and then raises `ProvisioningAttemptFailed(cause)`, a new application exception carrying the original error. Only the task knows how to reschedule, so it catches the exception and calls `self.retry(exc=cause)`. This keeps the decision of which status to record (`is_last_attempt` → FAILED, else RETRY) in the service. The retry *policy* (count, delay, rate limit) stays a Celery decorator concern.

*Alternative considered:* the service returns `RETRY_REQUESTED` instead of raising. Rejected: the task would need the cause anyway to pass to `self.retry(exc=…)`, and raising keeps the original traceback.

### 2. Ports: model only what the service calls

New Protocols in `app/application/ports.py`, all `@runtime_checkable`:

| Port | Methods | Satisfied by |
|---|---|---|
| `SyncBookingRepositoryPort` (exists) | `sync_get`, `sync_update_status`, `sync_set_status_message`, `sync_append_progress` | `BookingRepository` |
| `SyncImageReadPort` | `sync_get(session, image_id) -> VMImage` | `ImageRepository` |
| `SyncHWConfigReadPort` | `sync_get(session, hw_config_id) -> HWConfig` | `HWConfigRepository` |
| `SyncEnvironmentLeasePort` | `sync_start_lease_if_ready_for_booking(session, booking_id) -> bool` | `EnvironmentRepository` |
| `SessionRunner` | `__call__(work: Callable[[Session], T]) -> T` | the task's `_run` (one short-lived `SyncSessionLocal` per call) |
| `CredentialSlotPort` | `acquire() -> ContextManager[CredentialLease]`, raises `CredentialSlotUnavailable`; `CredentialLease` has `api_token: str \| None` and `renew()` | task-side adapter (Decision 9) |
| `VmApplier` | `apply(workspace_id, config, *, api_token, on_progress) -> dict` (sync) | task-side adapter (Decision 4) |
| `VmConfigRunnerPort` | `connect`, `run_script`, `close` | `SshConfigRunner`, `StubConfigRunner` |
| `RoleApplierPort` | `apply_roles(booking, *, ip, password, on_progress, extra_vars, label)` | `AnsibleConfigRunner`, `StubAnsibleRunner` |
| `ProgressSink` | `record`, `flush`, `close` | `ProgressRecorder` |
| `TeardownHandoff` | `hand_off(booking_id: str, request_id: str \| None) -> None` | task-side adapter (Decision 5) |

`CredentialSlotUnavailable` is an application exception defined beside the ports, so both the service and the task adapter can use it without the service importing infrastructure.

The service takes one `SessionRunner`, `run`. It builds today's `_lifecycle` barrier itself, "flush the attempt's sink, then `run`", which `progress-persistence` requires before every lifecycle write. Building it needs the per-attempt sink. The service therefore receives `progress_sink_factory(persist, label) -> ProgressSink`, creates the sink after the PROVISIONING transition as today, and closes it on every exit path. The barrier and the per-attempt isolation rule now live in the application layer. The task passes the module-global `recorder_from_settings` as the factory, so the existing patch target is kept.

The ports keep the `Session` type in their signatures. That is the concession `ports.py` already documents, and it is needed so the repositories satisfy the ports structurally.

`tests/test_repository_ports.py` gains conformance rows for each port with a concrete implementation. For the three task-side adapters, the service's unit tests and the adapter tests in Decision 9 are the check.

### 3. Domain exception for "reachable but misconfigured"

Add `VmConfigurationError(BookingError)` to `app/domain/exceptions.py`. Then:

- `ConfigScriptError(ConfigError, VmConfigurationError)`
- `AnsibleConfigError(ConfigError, VmConfigurationError)`

`ConfigError` stays the base of `VmUnreachableError`, so an unreachable VM is still *not* a `VmConfigurationError` and still takes the infrastructure-failure path. The service catches `VmConfigurationError` and `SecretDecryptionError`, which is already a domain exception, without importing any infrastructure. Existing `except ConfigScriptError` / `except ConfigError` sites keep working because only bases are added.

*Alternative considered:* the configurer port returns a result object instead of raising. Rejected: it would change two infrastructure classes' contracts and their tests for no gain.

### 4. The Terraform apply crosses the async boundary in the task

`VmApplier` is a **sync** port. The task implements it with a small closure that:
1. acquires the provisioning-lock marker (skipped under stub);
2. runs `asyncio.run(terraform.apply(workspace_id, config, api_token=…, on_progress=…))`;
3. releases the marker in `finally`.

This keeps CLAUDE.md's rule of one `asyncio.run` per Terraform call, in the task. Redis stays out of the application layer. `patch("app.tasks.provision.asyncio.run")`, `…terraform` and `…provisioning_lock` keep working. The service still owns building the apply config dict (`name`, `vapp_template_id`, `cpus`, `memory`, `disk_size`, `vm_password`) and the `booking-{id}` workspace id, because both are derived from booking data.

*Alternative considered:* an async service. Rejected: every other collaborator is sync, and the service would need `asyncio.run` around SSH and DB calls.

### 5. Teardown handoff through a narrow port, wired to the module global

When the booking was released mid-apply, the service calls `TeardownHandoff.hand_off(booking_id, request_id)`. The task implements it as `teardown_vm_task.delay(booking_id, request_id=request_id)` on its **module global**, so the race tests' `patch("app.tasks.provision.teardown_vm_task", …)` still intercepts the call.

*Alternative considered:* reuse `TaskDispatcher.dispatch_teardown` through `CeleryTaskDispatcher`. Rejected for this slice: `CeleryTaskDispatcher` imports `app.tasks.teardown` lazily, so the existing patches would stop intercepting it and test assertions would have to change. `TeardownHandoff` is a subset of `TaskDispatcher`, so a later slice can collapse the two.

### 6. Adapter-only knobs are constructor flags, not settings reads

The service never reads `settings`. The task passes `run_configuration = not settings.USE_STUB_TERRAFORM` and `request_id`. The service calls `lease.renew()` on every progress line. That replaces today's `redis_client.expire` inside `_on_progress`, so no separate activity callback is needed. The task reads these values per invocation, not at import, so `patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", …)` still applies.

### 7. The task builds the service per invocation from its module globals

Inside `provision_vm_task`, after binding the correlation id, the task builds the service. It no longer acquires the token itself (Decision 9):

```python
ProvisionBooking(
    bookings=repo, images=image_repo, hw_configs=hw_config_repo, environments=env_repo,
    run=_run, applier=..., config_runner=config_runner, role_applier=ansible_runner,
    progress_sink_factory=recorder_from_settings, teardown=..., run_configuration=...,
    credential_slot=..., request_id=request_id,
)
```

The globals are read at call time, so every existing `patch("app.tasks.provision.<name>")` still takes effect. The module keeps every currently patched name, including `_token_pool`, `_acquire_token` and `_needs_configuration`, which `tests/test_vm_startup_script.py` imports.

This is the compatibility mechanism that lets the existing suite run with unchanged assertions. The rule for the code PR: **no existing test assertion may change.** A patch target may move only if no wiring-preserving alternative exists. A fixture may change only for the extra booking read in Decision 9. Each such change must be listed in the PR description.

### 8. Testing: fakes for rules, existing tests for wiring

A new file, `tests/test_provision_booking_service.py`, holds in-memory fakes:
- a dict-backed booking store that applies the real `can_transition` guard and the `if_status_in` ownership check;
- a recording applier, configurer and role applier;
- a list-backed progress sink;
- a recording teardown handoff and lease port;
- `run = lambda work: work(None)`.

It has one test per spec scenario. It patches nothing.

The existing task tests then prove the adapter wiring, and the integration race test proves the real-DB interleaving.

### 9. The released-booking guard runs before the credential slot is taken (PR #519 review)

**Problem.** Today the task acquires the VCD token slot first (`_acquire_token` polls for up to 60 s) and only then reads the booking. A booking released while its task was queued, with every slot busy, waits out the timeout and gets `self.retry()`. That repeats until the retries run out, which contradicts the spec's "no retry" rule. Keeping that order in the adapter would also leave a lifecycle ordering rule outside the application layer.

**Decision.** The slot becomes a port that the service acquires, and the service fixes the order:

1. Read the booking. If it is RELEASING, RELEASED or FAILED, return `SKIPPED_RELEASED` without touching the slot.
2. `with credential_slot.acquire() as lease:` On `CredentialSlotUnavailable`, raise `ProvisioningDeferred` and write nothing.
3. Read the booking again and apply the same guard, because a release can land during the wait. If it is released, leave the `with` block, which releases the slot, and return `SKIPPED_RELEASED`.
4. Read the image and hardware config, then continue as before. Pass `lease.api_token` to `VmApplier.apply` and call `lease.renew()` on each progress line. The slot is released when the `with` block exits, on every path.

Both checks call one guard function, so the rule has a single definition. The second read replaces today's single read, which already happened after the slot was taken. The cost is one extra primary-key read per attempt.

**Adapter.** The task implements `CredentialSlotPort` in two ways:
- **Redis slot:** a context manager over the module-global `_token_pool()` and `_acquire_token(tokens, redis_client)`, called at acquire time. `test_provision_progress_batching.py` patches both, so they stay module globals. It maps `_acquire_token`'s `RuntimeError` to `CredentialSlotUnavailable(cause)`, and deletes the slot key on exit.
- **Null slot:** for `USE_STUB_TERRAFORM` or an empty token pool. `api_token` is `None` and `renew()` does nothing, as today.

**Tests.**
- **Service unit tests:**
  - released at the first check: the slot is never acquired;
  - released at the second check: the slot is acquired and released, and no apply runs;
  - slot unavailable: `ProvisioningDeferred` is raised and no write happens.
- **Real-adapter regression test** in `tests/test_provision_credential_slot_order.py`, the case the reviewer noted that stub-mode tests cannot reach. Run the real `provision_vm_task` with:
  - `USE_STUB_TERRAFORM=False`;
  - `VCD_API_TOKENS` set;
  - `redis_lib.Redis.from_url` patched so `set(..., nx=True)` always fails;
  - `time.monotonic`/`time.sleep` patched so the wait cannot block.

  Assert:
  - a RELEASED booking makes no `set` attempt, does not call `self.retry`, and writes nothing;
  - a PENDING booking is rescheduled with no status change.
- **Fixture updates, with no assertion changes:** two existing fixtures sequence booking reads per attempt. Each gains the extra read:
  - `tests/test_provision_progress_batching.py`'s `sync_get`, which counts `_gets % 2`;
  - the `statuses` list in `tests/test_release_during_provisioning.py::test_provision_task_hands_off_to_teardown_when_released_mid_apply`.

*Alternative considered:* a pre-check in the task before `_acquire_token`. Rejected: the guard would then be defined in both the adapter and the service, which is the duplication #429 is removing.

*Alternative considered:* an atomic "move to PROVISIONING only if startable" repository write in place of the second read. It would also close the small read-then-write window that exists today. Deferred to the `BookingRepository` slice, because this slice does not change the repository.

## Risks / Trade-offs

- **[Risk] A subtle ordering change** between lifecycle writes, flushes and handoff, which the tests miss. → Mitigations:
  - the service ports today's statement order one-for-one; the code PR diff is reviewed side by side against the old body;
  - the `progress-persistence` barrier tests (`test_provision_progress_batching.py`) and the integration race test run unchanged.
- **[Risk] Module-global wiring stays a test seam,** so the old patch-heavy tests remain. → Accepted for this slice: they are the regression net. New rules get fake-based tests. Retiring the old tests is a follow-up once teardown is extracted too.
- **[Trade-off] Two near-duplicate "teardown" ports** (`TeardownHandoff` ⊂ `TaskDispatcher`). → Documented in `ports.py`; collapse it in the teardown slice.
- **[Trade-off] One extra booking read per attempt** (Decision 9). → It is a primary-key `session.get` in its own short session, which is negligible next to a Terraform apply. In return a released booking no longer waits up to 60 s for a slot, or retries.
- **[Risk] Changing when the slot is taken could leak a slot on a new exit path.** → The slot is held only inside the `with` block, so every exit releases it. The service tests assert a release on every path where the slot was acquired.
- **[Risk] Multiple inheritance on the config exceptions.** The infra exception classes get a second base class. → Both bases are plain `Exception` subclasses with no `__init__` overrides, so there is no MRO hazard. One unit test asserts that `VmUnreachableError` is *not* a `VmConfigurationError`.

## Migration Plan

- Code-only and backwards-compatible: no migration, and the task name and signature are unchanged.
- Queued messages and in-flight retries are processed by the new adapter as before.
- After deploy, the only observable difference is Decision 9: a booking that is already released no longer waits for a slot or retries.
- Rollback is a plain revert.
