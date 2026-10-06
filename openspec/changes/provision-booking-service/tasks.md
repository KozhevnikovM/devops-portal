## 1. Tracking and baseline

- [ ] 1.1 In the code PR description, reference #429 and say that this is the provisioning slice. List the follow-up slices: `BookingRepository` decomposition, post-commit/outbox events, the teardown extraction and the admin route split. Verify by the issue link on the PR.
- [ ] 1.2 On a fresh branch off `main`, before changing anything, run `pytest tests/ -m "not integration"` and `pytest -m integration` against the PostgreSQL 15 test database, and record the pass counts. The final run (6.4) must match them, plus the new tests. Verify that the recorded baseline is in the PR description.

## 2. Domain and ports

- [ ] 2.1 Add `VmConfigurationError(BookingError)` to `app/domain/exceptions.py` (design Decision 3). Add it as a second base of `ConfigScriptError` (`app/infrastructure/config/runner.py`) and `AnsibleConfigError` (`app/infrastructure/config/ansible.py`). Verify with a unit test:
  - both are `VmConfigurationError` and `ConfigError`;
  - `VmUnreachableError` is not a `VmConfigurationError`.
- [ ] 2.2 Add the `@runtime_checkable` Protocols to `app/application/ports.py` (design Decisions 2 and 9): `SyncImageReadPort`, `SyncHWConfigReadPort`, `SyncEnvironmentLeasePort`, `SessionRunner`, `CredentialSlotPort` with `CredentialLease`, `VmApplier` (taking `api_token`), `VmConfigRunnerPort`, `RoleApplierPort`, `ProgressSink` and `TeardownHandoff`. Also add the `CredentialSlotUnavailable` application exception. Note in the docstring that `TeardownHandoff` is a subset of `TaskDispatcher`, to collapse in the teardown slice. Verify:
  - `mypy app/domain/ app/application/ports.py --ignore-missing-imports` is clean;
  - Ruff is clean on both files.
- [ ] 2.3 Extend `tests/test_repository_ports.py` with conformance rows:
  - `ImageRepository` → `SyncImageReadPort`;
  - `HWConfigRepository` → `SyncHWConfigReadPort`;
  - `EnvironmentRepository` → `SyncEnvironmentLeasePort`;
  - `SshConfigRunner`/`StubConfigRunner` → `VmConfigRunnerPort`;
  - `AnsibleConfigRunner`/`StubAnsibleRunner` → `RoleApplierPort`;
  - `ProgressRecorder` → `ProgressSink`.

  Verify that the test file passes.

## 3. ProvisionBooking service

- [ ] 3.1 Create `app/application/use_cases/provision_booking.py` with `ProvisionBooking`, `ProvisionOutcome`, `ProvisioningAttemptFailed` and `ProvisioningDeferred` (design Decisions 1, 6, 7 and 9). Begin with the Decision 9 order:
  - the released-booking guard;
  - `with credential_slot.acquire() as lease`, mapping `CredentialSlotUnavailable` to `ProvisioningDeferred` with no write;
  - the same guard again;
  - the image and hardware-config reads.

  Then port the rest of today's `provision_vm_task` body one step at a time, in the same order:
  - reusing or generating the password, and building the apply config and workspace id;
  - the PROVISIONING transition;
  - creating the sink with the lifecycle flush barrier;
  - the apply through `VmApplier` with `lease.api_token`, calling `lease.renew()` on each progress line;
  - the ownership-guarded message clear;
  - the mid-apply release check and handoff;
  - CONFIGURING, connect, script, roles and close;
  - the configuration-error message;
  - READY with `start_lease=True`;
  - the environment lease start;
  - the `SecretDecryptionError` and generic-failure branches, with their best-effort writes.

  Close the sink, and release the slot, on every exit path. Verify:
  - the module imports nothing from `app.infrastructure`, `app.tasks`, `celery`, `redis` or `asyncio`; check with `grep` and with a unit test that inspects the module's imports;
  - Ruff is clean.
- [ ] 3.2 Add `tests/test_provision_booking_service.py` with in-memory fakes and no `patch()` (design Decision 8). The fake booking store must apply `can_transition` and the `if_status_in` ownership check. Add one test per scenario in `specs/vm-provisioning-lifecycle/spec.md`:
  - released while queued, plus RELEASED and FAILED variants: the slot is never acquired;
  - released during the slot wait (second check): the slot is acquired and then released, with no apply and no write;
  - slot unavailable: `ProvisioningDeferred` is raised, with no status write and no message write;
  - recovery re-dispatch from PROVISIONING and CONFIGURING;
  - configuration enabled and stub provisioning;
  - existing password reused, and a generated password of 16 alphanumeric characters;
  - released mid-apply (handoff once, with the request id) and already settled (no handoff);
  - startup script fails, roles fail, VM unreachable;
  - not the last attempt and last attempt (`ProvisioningAttemptFailed` raised with the original cause);
  - secret decryption fails (`FAILED_PERMANENTLY`, nothing raised);
  - lease start on READY, on a final FAILED and on a secret failure, and none on RETRY;
  - best-effort writes: a store that raises while recording a failure still surfaces the original cause, or `FAILED_PERMANENTLY`.

  Also assert that every lifecycle write is preceded by a sink flush, that the sink is closed on each path, and that an acquired slot is released on each path. Verify that the file passes.

## 4. Thin Celery adapter

- [ ] 4.1 Rewrite `provision_vm_task` in `app/tasks/provision.py` as the adapter (design Decisions 4–7). It keeps:
  - the decorator and its signature;
  - the `request_id_ctx_var` binding;
  - a `CredentialSlotPort` adapter: a Redis slot over the module-global `_token_pool`/`_acquire_token`, resolved at acquire time, or a null slot under stub or with no tokens (design Decision 9). The task no longer acquires the token before the service runs;
  - `_run`;
  - an applier closure around the provisioning lock and `asyncio.run(terraform.apply(...))`;
  - a teardown handoff calling the module-global `teardown_vm_task.delay`;
  - mapping `ProvisioningDeferred` to `raise self.retry(exc=cause)`;
  - building the service from the module globals at call time;
  - mapping `ProvisioningAttemptFailed` to `raise self.retry(exc=cause)`;
  - the `request_id_ctx_var` reset in `finally`.

  Keep every module-level name that tests patch or import, including `_needs_configuration`. Verify:
  - `ruff check` and `ruff format --check` pass on the file;
  - the module no longer contains any `BookingStatus.READY`/`RETRY`/`CONFIGURING` decision logic.
- [ ] 4.2 Run the existing provisioning tests unchanged: `tests/test_provision_task.py`, `test_provision_progress_batching.py`, `test_provision_session_lifetime.py`, `test_provisioning_progress.py`, `test_release_during_provisioning.py`, `test_vm_password.py`, `test_vm_startup_script.py`, `test_vm_ansible_roles.py`, `test_booking_configuring_state.py`, `test_environment_lease_start.py`, and `test_drive_type_quota.py`. Verify that all pass with no assertion changed. Only two fixtures change, each gaining the extra booking read from design Decision 9:
  - the `sync_get` read counter in `test_provision_progress_batching.py`;
  - the `statuses` list in `test_release_during_provisioning.py::test_provision_task_hands_off_to_teardown_when_released_mid_apply`.

  List both, and any patch target that had to move, in the PR description with the reason (design Decision 7).
- [ ] 4.3 Add `tests/test_provision_credential_slot_order.py`, the real-adapter regression for the PR #519 review (design Decision 9). Run the real `provision_vm_task` with:
  - `USE_STUB_TERRAFORM=False`;
  - `VCD_API_TOKENS` set;
  - `redis_lib.Redis.from_url` patched so `set(..., nx=True)` always fails;
  - the wait's clock and sleep patched so the test cannot block.

  Cases:
  - **RELEASED, and also RELEASING:** no `set` attempt, no `Retry` raised, no status or message write;
  - **PENDING:** `Retry` is raised, there is no status write, and no booking write happens before it.

  Verify that the file passes, and that the RELEASED case fails if the token acquisition is temporarily moved back ahead of the guard.
- [ ] 4.4 Run the Postgres integration tests that drive the task: `tests/integration/test_provision_release_race.py`, `test_environment_lease_reconciliation.py`, `test_environment_construction_race.py` and `test_environment_child_release_regression.py`. Verify that all pass against PostgreSQL 15.

## 5. Runtime check and docs

- [ ] 5.1 Runtime check in the worker, following the worker runtime-check recipe: `docker compose up`, order a VM in stub mode, and confirm PENDING → PROVISIONING → READY in the UI. With `VCD_API_TOKENS` set and every slot key pre-set in Redis, release a queued booking and dispatch its provisioning. Confirm that the worker log shows the skip with no slot wait and no retry. Then run the real task in the worker with Terraform patched, a startup script that fails, and `USE_STUB_TERRAFORM=False`. Confirm:
  - CONFIGURING appears;
  - the booking ends READY with `config_failed`;
  - the error message is shown;
  - releasing during a slow fake apply hands off to teardown.

  Verify by recording the observed transitions in the PR description.
- [ ] 5.2 Update `CLAUDE.md`, the `app/tasks/` and `use_cases` bullets, to name `provision_booking` and to describe `provision_vm_task` as a thin adapter. `docs/admin-guide.md` and `docs/api-reference.md` need no change, because there is no user-facing change. Confirm this in the PR description. Verify by reviewing the diff.

## 6. Quality gates and final run

- [ ] 6.1 Run the `py-review` skill on all changed Python files and fix any findings. Verify a clean report.
- [ ] 6.2 Run `bandit -r app/ --severity-level high --exclude tests/ -b .bandit-baseline.json -q`. Verify that there are no new findings.
- [ ] 6.3 Run `mypy app/domain/ app/application/ports.py --ignore-missing-imports`. Verify that it is clean.
- [ ] 6.4 Run `pytest tests/ -m "not integration"` and `pytest -m integration`. Verify that both pass, with counts equal to the 1.2 baseline plus the new tests.

## 7. Spec sync (after the code PR is approved)

- [ ] 7.1 Run `/opsx:sync` to create `openspec/specs/vm-provisioning-lifecycle/spec.md`, then `/opsx:archive`, and commit both to the code PR branch before merging. Verify that `openspec validate --strict` passes on the main specs.
