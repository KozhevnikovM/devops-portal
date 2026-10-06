## 1. Tracking and baseline

- [ ] 1.1 In the code PR description, reference #429 and say that this is the provisioning slice. List the follow-up slices: `BookingRepository` decomposition, post-commit/outbox events, the teardown extraction and the admin route split. Verify by the issue link on the PR.
- [ ] 1.2 On a fresh branch off `main`, before changing anything, run `pytest tests/ -m "not integration"` and `pytest -m integration` against the PostgreSQL 15 test database, and record the pass counts. The final run (6.4) must match them, plus the new tests. Verify that the recorded baseline is in the PR description.

## 2. Domain and ports

- [ ] 2.1 Add `VmConfigurationError(BookingError)` to `app/domain/exceptions.py` (design Decision 3). Add it as a second base of `ConfigScriptError` (`app/infrastructure/config/runner.py`) and `AnsibleConfigError` (`app/infrastructure/config/ansible.py`). Verify with a unit test:
  - both are `VmConfigurationError` and `ConfigError`;
  - `VmUnreachableError` is not a `VmConfigurationError`.
- [ ] 2.2 Add the `@runtime_checkable` Protocols to `app/application/ports.py` (design Decision 2): `SyncImageReadPort`, `SyncHWConfigReadPort`, `SyncEnvironmentLeasePort`, `SessionRunner`, `VmApplier`, `VmConfigRunnerPort`, `RoleApplierPort`, `ProgressSink` and `TeardownHandoff`. Note in the docstring that `TeardownHandoff` is a subset of `TaskDispatcher`, to collapse in the teardown slice. Verify:
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

- [ ] 3.1 Create `app/application/use_cases/provision_booking.py` with `ProvisionBooking`, `ProvisionOutcome` and `ProvisioningAttemptFailed` (design Decisions 1, 6 and 7). Port today's `provision_vm_task` body one step at a time, in the same order:
  - the released-booking guard;
  - reusing or generating the password, and building the apply config and workspace id;
  - the PROVISIONING transition;
  - creating the sink with the lifecycle flush barrier;
  - the apply through `VmApplier`;
  - the ownership-guarded message clear;
  - the mid-apply release check and handoff;
  - CONFIGURING, connect, script, roles and close;
  - the configuration-error message;
  - READY with `start_lease=True`;
  - the environment lease start;
  - the `SecretDecryptionError` and generic-failure branches, with their best-effort writes.

  Close the sink on every exit path. Verify:
  - the module imports nothing from `app.infrastructure`, `app.tasks`, `celery`, `redis` or `asyncio`; check with `grep` and with a unit test that inspects the module's imports;
  - Ruff is clean.
- [ ] 3.2 Add `tests/test_provision_booking_service.py` with in-memory fakes and no `patch()` (design Decision 8). The fake booking store must apply `can_transition` and the `if_status_in` ownership check. Add one test per scenario in `specs/vm-provisioning-lifecycle/spec.md`:
  - released while queued, plus RELEASED and FAILED variants;
  - recovery re-dispatch from PROVISIONING and CONFIGURING;
  - configuration enabled and stub provisioning;
  - existing password reused, and a generated password of 16 alphanumeric characters;
  - released mid-apply (handoff once, with the request id) and already settled (no handoff);
  - startup script fails, roles fail, VM unreachable;
  - not the last attempt and last attempt (`ProvisioningAttemptFailed` raised with the original cause);
  - secret decryption fails (`FAILED_PERMANENTLY`, nothing raised);
  - lease start on READY, on a final FAILED and on a secret failure, and none on RETRY;
  - best-effort writes: a store that raises while recording a failure still surfaces the original cause, or `FAILED_PERMANENTLY`.

  Also assert that every lifecycle write is preceded by a sink flush, and that the sink is closed on each path. Verify that the file passes.

## 4. Thin Celery adapter

- [ ] 4.1 Rewrite `provision_vm_task` in `app/tasks/provision.py` as the adapter (design Decisions 4–7). It keeps:
  - the decorator and its signature;
  - the `request_id_ctx_var` binding;
  - `_token_pool`/`_acquire_token`, with the token-timeout `self.retry` that changes no status;
  - `_run`;
  - an applier closure around the provisioning lock and `asyncio.run(terraform.apply(...))`;
  - a teardown handoff calling the module-global `teardown_vm_task.delay`;
  - `on_activity` renewing the token lock;
  - building the service from the module globals at call time;
  - mapping `ProvisioningAttemptFailed` to `raise self.retry(exc=cause)`;
  - token-lock release in `finally`.

  Keep every module-level name that tests patch or import, including `_needs_configuration`. Verify:
  - `ruff check` and `ruff format --check` pass on the file;
  - the module no longer contains any `BookingStatus.READY`/`RETRY`/`CONFIGURING` decision logic.
- [ ] 4.2 Run the existing provisioning tests unchanged: `tests/test_provision_task.py`, `test_provision_progress_batching.py`, `test_provision_session_lifetime.py`, `test_provisioning_progress.py`, `test_release_during_provisioning.py`, `test_vm_password.py`, `test_vm_startup_script.py`, `test_vm_ansible_roles.py`, `test_booking_configuring_state.py`, `test_environment_lease_start.py`, and `test_drive_type_quota.py`. Verify that all pass with no assertion changed. If a patch target had to move, list it in the PR description with the reason (design Decision 7).
- [ ] 4.3 Run the Postgres integration tests that drive the task: `tests/integration/test_provision_release_race.py`, `test_environment_lease_reconciliation.py`, `test_environment_construction_race.py` and `test_environment_child_release_regression.py`. Verify that all pass against PostgreSQL 15.

## 5. Runtime check and docs

- [ ] 5.1 Runtime check in the worker, following the worker runtime-check recipe: `docker compose up`, order a VM in stub mode, and confirm PENDING → PROVISIONING → READY in the UI. Then run the real task in the worker with Terraform patched, a startup script that fails, and `USE_STUB_TERRAFORM=False`. Confirm:
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
