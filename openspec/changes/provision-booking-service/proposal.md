## Why

#429 found that orchestration and business rules have built up in adapters. The Celery task `provision_vm_task` (`app/tasks/provision.py`) is the clearest case, and this change covers it. The other directions in #429 get their own changes. They are the `BookingRepository` decomposition, explicit event publication or an outbox, and splitting the admin routes.

One 200-line function body mixes several concerns:

- **Celery adapter concerns:** retry policy, correlation-id binding, Redis VCD-token semaphore, session-per-write choreography, `asyncio.run`.
- **Lifecycle rules:**
  - skip a booking released while the task was queued;
  - hand off to teardown when the booking is released mid-apply;
  - an unreachable VM fails, but a reachable VM whose script or roles fail is kept as `READY` + `config_failed`;
  - a secret-decryption failure fails at once with no retry;
  - an attempt goes to `RETRY` until the last one, then `FAILED`;
  - a settled child may start its environment's lease.

None of these rules can be reused or tested without the worker. The tests show it: 15 test files reach into `app.tasks.provision`, and about 150 `patch("app.tasks.provision.<global>")` calls patch module globals such as `SyncSessionLocal`, `asyncio.run`, `repo` and `settings.USE_STUB_TERRAFORM`. A rule change today means editing the Celery task. Mistakes there are easy to make, and some have already shipped (#394, #434).

## What Changes

- **A `ProvisionBooking` application service owns the provisioning lifecycle.** A new application-layer module decides each step and each outcome of one provisioning attempt:
  - the released-booking guard;
  - reusing or generating the VM password;
  - the `PROVISIONING → CONFIGURING → READY` transitions;
  - the mid-apply release check and teardown handoff;
  - classifying configuration failures as software or infrastructure;
  - the secret-decryption and last-attempt failure rules;
  - starting the environment lease.

  It depends only on application ports and domain types. It imports no Celery, Redis, SQLAlchemy session factory, Terraform, SSH or Ansible module.
- **New ports in `app/application/ports.py`**, so the service reaches infrastructure only through Protocols. Each concrete class already matches its port structurally:
  - sync image and hardware-config reads;
  - the environment lease-start call;
  - a sync "apply this VM" port;
  - the post-provision configuration runner and the role applier;
  - a progress sink;
  - a short-lived-session runner.
- **A domain exception for "VM reachable, configuration failed".** The service needs to tell a software failure from an infrastructure failure without importing the infrastructure `ConfigScriptError`/`AnsibleConfigError`. Both become subclasses of a new domain exception.
- **`provision_vm_task` becomes a thin Celery adapter.** It keeps only:
  - binding the correlation id;
  - acquiring, renewing and releasing a VCD token;
  - the provisioning-lock marker and `asyncio.run` around `terraform.apply`;
  - per-write sessions;
  - building the service from its collaborators;
  - turning the service's outcome into Celery control flow (`self.retry`).

  The task name, its arguments and its retry settings stay the same. Messages already queued when the change deploys keep working.
- **Unit tests of the service use in-memory fakes,** with no `patch()` of module globals. They cover every lifecycle rule. The existing task tests stay as the adapter-level regression net, and their assertions do not change.
- **No behaviour change:** statuses, status messages, audit entries, live-row events, progress batching, retry counts and timing all stay as they are. The new capability states this existing behaviour as a contract for the first time.

## Capabilities

### New Capabilities

- `vm-provisioning-lifecycle`: the observable lifecycle contract of one VM provisioning attempt. It covers:
  - which statuses a booking passes through;
  - what happens when the booking is released before or during the apply;
  - how configuration failures are classified;
  - which failures are retried and which are permanent;
  - when an environment child's settlement starts the environment lease.

  It also requires that these rules be enforced by code that can be exercised without the task queue, the lock store or the cloud provider. This is the #429 acceptance direction for this slice.

### Modified Capabilities

_None._ `progress-persistence`, `vm-provisioning-recovery`, `environment-lifecycle` and `live-row-updates` keep their requirements. This change only moves where their provisioning-side rules are enforced.

## Impact

- **Code:**
  - new: `app/application/use_cases/provision_booking.py`;
  - extended: `app/application/ports.py` and `app/domain/exceptions.py`;
  - the base classes of `ConfigScriptError` (`app/infrastructure/config/runner.py`) and `AnsibleConfigError` (`app/infrastructure/config/ansible.py`) change;
  - `app/tasks/provision.py` shrinks to the adapter.
- **Unchanged:**
  - `app/tasks/teardown.py`, startup recovery in `app/main.py`, `CeleryTaskDispatcher`, the beat tasks, the repositories, the templates and the API;
  - no migration.
- **Tests:**
  - new service unit tests with fakes;
  - port-conformance checks, added to `tests/test_repository_ports.py`;
  - the existing `tests/test_provision_*.py`, `test_release_during_provisioning.py`, `test_vm_*`, `test_booking_configuring_state.py` and the integration race tests keep passing with unchanged assertions.
- **CI (#433 gates):**
  - `app/application/ports.py` and `app/domain/` must stay Mypy-clean;
  - every touched file must be fully Ruff-clean, including the rewritten `app/tasks/provision.py`.
- **Follow-ups (not here):** the other #429 slices:
  - `BookingRepository` decomposition;
  - post-commit or outbox event publication;
  - an equivalent extraction for `teardown_vm_task`;
  - the admin route split.
