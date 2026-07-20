# v0.11.0 Plan

## Goal

v0.11.0 closes two live production-reliability bugs (I2, I3), eliminates five confirmed-stale code-quality defects that the architecture review re-verified as still open (D6, I5, beat healthcheck, API versioning, the admin router's self-instantiated object graph), and lays the structural foundation required for safe larger refactors in v0.12.0 by standing up a Postgres-backed integration test tier (T1) and extracting the `ResourceDetails` value object before the next resource type is added. Every item in scope was confirmed open in the 2026-07-17 architecture review against HEAD; no speculative work is included.

---

## Scope

### In scope

**Sprint 1 — Live bugs and quick wins (1–3 days each)**
- F-1: Renew VCD token-lock TTL from the existing `_on_progress` callback (`provision.py`)
- F-2: Classify `VmUnreachableError` as a config-phase failure so retries reuse the already-persisted `vm_password` (`provision.py`)
- F-3: Remove the concrete-dispatcher fallback from `CreateBookingUseCase`; thread `DEV_USER_ID` and `SECRET_VARS_ENABLED` through constructors (`create_booking.py`, `order_environment.py`)
- F-4: Add a PID-file-based healthcheck to the beat service (`docker-compose.prod.yml`)
- F-5: Replace the environment-listing N+1 with a single batched query (`environment_repo.py`)
- F-6: Add `/api/v1` prefix as a stable alias for all JSON API routes; keep unversioned paths working (`main.py`, three router files)

**Sprint 2 — Structural prerequisites (1–2 weeks)**
- F-7: Stand up the Postgres integration test tier — real `asyncpg` engine, `alembic upgrade head` fixture, `pytest.mark.integration` tag, key repository/quota/status-guard tests
- F-8: Route `admin.py`, `auth.py`, and `api.py` through `deps.py`; extract `ForceReleaseBookingUseCase`; add `hx_error()` helper
- F-9: Extract `VMDetails`, `NamespaceDetails`, `StaticVMDetails`, and `ResourceFootprint` value objects out of `Booking`

### Out of scope

- `SyncBookingRepositoryPort` for the Celery worker path (P1-C) — deferred to v0.12.0; depends on F-7 completing first so the refactor can be integration-tested.
- Decomposing `OrderEnvironmentUseCase` into a Process Manager (P2-A) — XL effort; v0.12.0 after F-7 and F-8 land.
- Domain events for queue-promotion and audit-write (P2-B) — correct direction; deferred one release to avoid colliding with F-8's structural change to the same route layer.
- Full bounded-context module split (`app/domain/{catalog,booking,identity}/`) — long-term; F-9 is the required load-bearing first step.
- Fernet encryption of `vm_password`/`static_vms.password` (S3) — decision doc revisit is on the board; deferred because it requires a migration and a new secret-management story not yet in scope.
- `RecoverStuckBookingsUseCase` extraction from `main.py` lifespan — valid finding; small blast radius today; v0.12.0.
- C4 context and component level diagrams (P3-D) — documentation work; v0.12.0.
- Formal `UserRole`/Policy VO (P4-A) — not blocking anything in v0.11.0.

---

## Features / Improvements

### F-1: Renew VCD Token-Lock TTL from Progress Callback
**Review ref:** I2

**What:** In `app/tasks/provision.py`, the `_on_progress` nested function is called by `terraform.apply()` roughly every 15 seconds during a long apply. It already has the outer scope's `redis_client` and `lock_key` variables in its closure. Add `if redis_client and lock_key: redis_client.expire(lock_key, settings.VCD_TOKEN_LOCK_TTL)` as the first line of `_on_progress`, before the `repo.sync_set_status_message` call. The `if` guard covers the `use_semaphore=False` path (stub mode / no token pool) where both are `None`. No config changes, no new dependencies.

**Why:** The lock is acquired once with a fixed TTL (`settings.VCD_TOKEN_LOCK_TTL`, default 900 s). An apply that runs longer than 900 s silently frees the slot while still using the token, allowing a second concurrent task to claim the same slot and exceeding `VCD_TOKEN_MAX_PARALLEL` — the scenario the entire token-pool mechanism exists to prevent.

**Acceptance criteria:**
- A provisioning task running longer than `VCD_TOKEN_LOCK_TTL` seconds does not release its lock mid-apply (verified by checking the TTL on the Redis key via `redis_client.ttl(lock_key)` after each `_on_progress` call in a test).
- The `redis_client.expire` call is guarded so it is a no-op when `redis_client is None` (no regression in stub/dev mode).
- Existing `tests/test_provision_task.py` and `tests/test_provisioning_progress.py` still pass.

---

### F-2: Classify VmUnreachableError as a Config-Phase Failure
**Review ref:** I3

**What:** In `app/tasks/provision.py`, `vm_password` is generated and then stored to the DB in the CONFIGURING status update. When `config_runner.connect()` raises `VmUnreachableError`, the generic `except Exception` block retries the entire task and regenerates a brand-new `vm_password` — a password that does not match the one baked into the already-provisioned VM by Terraform.

The fix: at the start of the task body, after the `booking_uuid` assignment, call `repo.sync_get(session, booking_uuid)` to read `existing_booking`. Replace the inline password generation with: `vm_password = existing_booking.vm_password if existing_booking.vm_password else "".join(secrets.choice(...) for _ in range(16))`. No changes to retry logic, no new exception types — `VmUnreachableError` still propagates to the generic retry path; it just no longer overwrites the persisted password.

**Why:** A transient SSH failure right after a successful Terraform apply causes the retry to write a new password to the DB that was never applied to the VM. The booking reaches READY with a credential that does not unlock the VM.

**Acceptance criteria:**
- A `VmUnreachableError` on the first attempt followed by a successful connect on the second attempt results in READY with the same `vm_password` used in the Terraform apply.
- A fresh first-attempt booking with `vm_password=None` still generates a new password as before.
- `tests/test_provision_task.py` covers the retry-with-existing-password path (new test case).

---

### F-3: Remove Concrete-Dispatcher Fallback and Settings Reads from Application Layer
**Review ref:** D6

**What:** Two changes across two files:

1. `app/application/use_cases/create_booking.py`: Remove the `_dispatch()` method that lazy-imports `CeleryTaskDispatcher`. Change the `dispatcher` parameter in `__init__` from `dispatcher: TaskDispatcher | None = None` to `dispatcher: TaskDispatcher`. Raise `ValueError("dispatcher is required")` if it is `None` at construction time. Remove `uid = user_id or settings.DEV_USER_ID`; change `execute()` so `user_id: str` is non-optional. All callers already pass `user_id`; verify by grepping `create_booking_uc.execute(` across route files. The `deps.py` wiring already passes `dispatcher=dispatcher`, so no change is needed there.

2. `app/application/use_cases/order_environment.py`: Remove the inline `settings.SECRET_VARS_ENABLED` read. Thread `secret_vars_enabled: bool` as a constructor parameter of `OrderEnvironmentUseCase`, defaulting to `settings.SECRET_VARS_ENABLED` in `deps.py` at construction time.

**Why:** `TaskDispatcher` exists as a port precisely so the application layer never imports Celery. The lazy-import fallback re-opens that door — currently dead code in production but structurally a latent coupling. The settings reads create an implicit dependency on the global config singleton.

**Acceptance criteria:**
- `from app.infrastructure.celery_dispatcher import CeleryTaskDispatcher` no longer appears in `create_booking.py`.
- `from app.config import settings` no longer appears in the per-line usage in `create_booking.py` or `order_environment.py`.
- Constructing `CreateBookingUseCase` with no `dispatcher` argument raises `ValueError` immediately.
- All existing `tests/test_create_booking.py` tests pass; add one that verifies the `ValueError` on missing dispatcher.

---

### F-4: Add Beat Container Healthcheck
**Review ref:** Architecture review C4 / T3-partial

**What:** In `docker-compose.prod.yml`, the `beat` service has no `healthcheck` block. Use a PID-file-based approach: add `--pidfile /tmp/celerybeat.pid` to the beat `command`, then add:

```yaml
healthcheck:
  test: ["CMD-SHELL", "test -f /tmp/celerybeat.pid && kill -0 $(cat /tmp/celerybeat.pid) 2>/dev/null || exit 1"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 30s
```

The `kill -0 PID` check confirms the process identified by the PID file is still running without sending any signal.

**Why:** A silently-dead beat container stops TTL enforcement and stale-provisioning reaping without any operational visibility — both are safety mechanisms for quota and resource cleanup.

**Acceptance criteria:**
- `docker compose -f docker-compose.prod.yml config` validates without error.
- Beat container reaches `healthy` status within 90 s of start.
- Killing the beat process inside the container results in the container transitioning to `unhealthy`.

---

### F-5: Fix Environment-Listing N+1 Queries
**Review ref:** I5

**What:** Two changes in `app/infrastructure/repositories/environment_repo.py`:

1. `_list()`: Replace the per-environment `await self._children(session, model.id)` loop with a single `_children_batch(session, env_ids)` query using `BookingModel.environment_id.in_(env_ids)` with the same 4-way joins as `_children()`, then group results in Python by `environment_id`.

2. `get_by_namespace()`: After collecting `env_ids`, replace the per-ID `await self.get(session, env_id)` loop with a batched `EnvironmentModel.id.in_(env_ids)` query combined with the same `_children_batch` call.

Add a new private `_children_batch(session, env_ids: list[UUID]) -> dict[UUID, list[Booking]]` method that executes one query and returns results grouped by environment.

**Why:** With N environments, `_list()` issues N+1 DB round-trips. The bottleneck for fleet-dashboard use has been open since the 2026-07-10 review.

**Acceptance criteria:**
- `_list()` for M environments issues exactly 2 DB queries (verified with SQLAlchemy `echo=True` in a test).
- `get_by_namespace()` for K matching environments issues exactly 2 DB queries.
- All existing `tests/test_environment_lifecycle.py` and `tests/test_environment_ui.py` tests pass.

---

### F-6: Add /api/v1 Prefix for JSON API Routes
**Review ref:** NEW (API versioning)

**What:** Three JSON API routers are currently mounted at bare paths in `main.py`. Approach: strip the `/api` segment from each router's own `prefix`, then mount each router twice — once at `prefix="/api"` (backward-compatible) and once at `prefix="/api/v1"` (canonical). Mark the `/api` alias mounts with `include_in_schema=False` so `/docs` shows only `/api/v1/...` paths. Specifically:

- `api.py`: change `prefix="/api"` → `prefix=""`.
- `api_bookings.py`: change `prefix="/api/bookings"` → `prefix="/bookings"`.
- `api_environments.py`: change `prefix="/api/environments"` → `prefix="/environments"`.
- `main.py`: include each router under both `prefix="/api"` (`include_in_schema=False`) and `prefix="/api/v1"`.

Update `docs/api-reference.md` to document `/api/v1/...` as the canonical base and `/api/...` as a deprecated alias.

**Why:** Jenkins/CI is a documented first-class API consumer. Any breaking change currently has no migration path. Adding versioning now costs one router-mount change per router; after pipelines hardcode the unversioned path it becomes a coordinated multi-team migration.

**Acceptance criteria:**
- `GET /api/v1/bookings` returns the same response as `GET /api/bookings`.
- `GET /docs` shows only `/api/v1/...` paths in the OpenAPI UI.
- All existing `tests/test_api_bookings.py` and `tests/test_api_namespaces.py` tests pass without modification.
- New `tests/test_api_v1_routes.py` verifies HTTP 200 on `/api/v1/bookings`, `/api/v1/environments`, `/api/v1/images`.

---

### F-7: Stand Up Postgres Integration Test Tier
**Review ref:** T1

**What:** Create a new `tests/integration/` package with its own `conftest.py` and `pytest.mark.integration` marker.

`tests/integration/conftest.py` fixtures:
- `async_engine` (session-scoped): `create_async_engine` pointed at `TEST_POSTGRES_URL` env var (default `postgresql+asyncpg://portal:portal@localhost:5433/portal_test`).
- Run `alembic upgrade head` once per session.
- `async_session` (function-scoped): wraps each test in a `BEGIN`/`ROLLBACK` savepoint so tests never commit and the DB stays clean between runs.

Add `asyncpg>=0.29.0` to `requirements-dev.txt` (already listed; confirm it is present).

Key integration test modules:
- `tests/integration/test_booking_status_guard.py`: Verify `booking_repo.update_status()` raises `IllegalStatusTransitionError` on disallowed moves under real Postgres.
- `tests/integration/test_quota_concurrent_writes.py`: Two concurrent `CreateBookingUseCase.execute()` calls, assert quota ceiling enforced under real `SELECT FOR UPDATE`.
- `tests/integration/test_queue_promotion.py`: Verify `SELECT FOR UPDATE SKIP LOCKED` prevents double-promotion of the same queued slot.

CI: `pytest -m "not integration"` (existing stage, no DB) + new stage: spin up Postgres, `pytest -m integration --timeout=60`.

**Why:** Every high-severity bug in this codebase's history was a SQL-semantics or lock-ordering bug that 611 mocked tests cannot exercise. T1 is the prerequisite that makes F-8 and F-9 verifiable.

**Acceptance criteria:**
- `pytest -m integration` passes on a machine with Postgres at `TEST_POSTGRES_URL`.
- `pytest -m "not integration"` passes with no DB dependency.
- At least one test uses `asyncio.gather` to simulate concurrent requests and verifies quota enforcement under real lock semantics.
- The `async_session` fixture's ROLLBACK isolation is verified: two tests inserting the same booking ID do not conflict.

---

### F-8: Route admin.py / auth.py / api.py Through deps.py; Extract ForceReleaseBookingUseCase
**Review ref:** P2-E

**What:**

**`admin.py` (lines 30–36):** Remove all seven module-level repository instantiations. Replace with imports from `app.presentation.deps` singletons (`_deps.booking_repo`, `_deps.image_repo`, etc.). This preserves existing module-path patch targets in `tests/test_admin_*` with zero test changes.

**`auth.py`:** Remove `_quota_repo`, `_user_repo`, `_image_repo`, `_hw_config_repo` instantiations; replace with `deps.*` singletons.

**`api.py`:** Remove `_image_repo`, `_hw_config_repo`, `_namespace_repo`, `_role_repo`, `_blueprint_repo`, `_static_vm_repo` instantiations; replace with `deps.*` singletons.

**`ForceReleaseBookingUseCase`:** Extract the inline logic from `admin_force_release_booking` into `app/application/use_cases/force_release_booking.py`. Constructor takes `BookingRepositoryPort` and `TaskDispatcher`. `execute(session, booking_id, actor_id)` encapsulates: `get()` → status/resource-type validation → `update_status(RELEASING)` → `dispatch_teardown_force()` → `get()` re-fetch. Wire in `deps.py`.

**`hx_error()` helper:** Add `app/presentation/utils.py` with an `hx_error(content, target, reswap) -> HTMLResponse` helper. Replace all 13 inline `HX-Retarget` response patterns in `admin.py` with `hx_error(...)` calls.

**Why:** Two object-construction paths mean any DI-level change (caching, read-replica routing) must be made twice and nothing enforces that `admin.py` gets the update. This has been on the books since 2026-06-30.

**Acceptance criteria:**
- `grep -n "BookingRepository()\|ImageRepository()\|HWConfigRepository()" app/presentation/routes/admin.py app/presentation/routes/auth.py app/presentation/routes/api.py` returns no output.
- `ForceReleaseBookingUseCase` has a unit test covering the status check, dispatch call, and re-fetch sequence.
- All existing `tests/test_admin_*.py`, `tests/test_auth.py`, and `tests/test_api_bookings.py` pass without modification.
- `grep "HX-Retarget" app/presentation/routes/admin.py` returns no output.

---

### F-9: Extract ResourceDetails and ResourceFootprint Value Objects from Booking
**Review ref:** Architecture review — god-entity / VO extraction

**What:** Create `app/domain/resource_details.py` with:

- `@dataclass(frozen=True) class VMDetails`: `image_id`, `image_name`, `hw_config_id`, `hw_config_name`, `vm_ip`, `vm_password`, `startup_script`, `config_roles: tuple`, `extra_vars: dict`, `config_failed`.
- `@dataclass(frozen=True) class NamespaceDetails`: `namespace_id`, `namespace_name`, `cluster_name`, `api_url`.
- `@dataclass(frozen=True) class StaticVMDetails`: `static_vm_id`, `static_vm_name`, `static_vm_host`, `static_vm_username`, `static_vm_password`, `static_vm_ssh_key`.
- `@dataclass(frozen=True) class ResourceFootprint`: `cpus`, `memory_mb`, `disk_mb`, `drive_type`. Methods: `memory_gb() -> int` (`math.ceil(self.memory_mb / 1024)`), `disk_gb() -> int` (`math.ceil(self.disk_mb / 1024)`).

Add `details: VMDetails | NamespaceDetails | StaticVMDetails | None = field(default=None)` and `footprint: ResourceFootprint | None = field(default=None)` to `Booking` in `entities.py`.

Update `booking_repo.py`'s `_to_entity()` to populate `details` and `footprint` from existing flat model columns — **no Alembic migration required** because the flat columns remain on `BookingModel`. Mark deprecated flat fields on `Booking` with `# deprecated: use self.details.{field}`.

Update `create_booking.py:68-69` and `quota_repo.py:74-76` to use `ResourceFootprint.memory_gb()` / `disk_gb()` — the two sides now share one rounding implementation, removing the structural duplication that caused the D3 quota floor/ceil bug.

**Why:** `Booking` carries 34 fields from three implicit bounded contexts. Adding a 4th resource type currently requires touching ~12 files. The `math.ceil` duplication is structurally identical to the D3 bug — two sides currently agree but nothing enforces they continue to.

**Acceptance criteria:**
- `VMDetails`, `NamespaceDetails`, `StaticVMDetails`, `ResourceFootprint` are `frozen=True` dataclasses with no SQLAlchemy or FastAPI imports.
- `booking.details` is populated for all bookings returned by `BookingRepository.get()` — verified via F-7 integration tests.
- `math.ceil` for quota rounding appears in exactly one place: `ResourceFootprint.memory_gb()` and `ResourceFootprint.disk_gb()` — verified by `grep -rn "math.ceil" app/`.
- No Alembic migration is required; all existing tests pass without modification.

---

## DB Migrations

None. All changes are application-layer-only. The `Booking` entity gains `details` and `footprint` as computed Python fields populated from existing DB columns. Flat columns remain on `BookingModel` and are candidates for removal in v0.12.0 after all consumers have migrated to VO accessors.

---

## API Changes

None for HTMX routes.

For JSON API routes (F-6):
- All routes at `/api/bookings/*`, `/api/environments/*`, and `/api/{images,hw-configs,namespaces,roles,blueprints,static-vms}/*` gain parallel routes at `/api/v1/...`.
- The `/api/...` paths remain fully functional; excluded from OpenAPI schema (`include_in_schema=False`) so `/docs` shows only `/api/v1/...`.
- No request or response schemas change.

---

## Testing Plan

**Unit tests (new, added alongside each feature):**
- F-1: Verify `redis_client.expire` is called inside `_on_progress`; verify no-op when `redis_client is None`.
- F-2: Verify the retry path reuses `existing_booking.vm_password` and does not invoke `secrets.choice`.
- F-3: Verify `ValueError` is raised when `CreateBookingUseCase` is constructed with `dispatcher=None`.
- F-5: Query-count assertion for environment list (≤ 2 queries for N environments).
- F-6: New `tests/test_api_v1_routes.py` with parametrized GET checks on `/api/v1/...` paths.
- F-8: `ForceReleaseBookingUseCase` unit test via mocked ports.

**Integration tests (new tier — F-7, prerequisite for F-8 and F-9):**
- `test_booking_status_guard.py`: Real INSERT + UPDATE; assert `IllegalStatusTransitionError` on invalid transitions.
- `test_quota_concurrent_writes.py`: Concurrent `asyncio.gather`; assert quota ceiling under `SELECT FOR UPDATE`.
- `test_queue_promotion.py`: Assert no double-promotion under concurrent reads with `SKIP LOCKED`.
- `test_resource_details_vo.py` (added with F-9): Assert `booking.details` and `booking.footprint` populated correctly for all three resource types.

**CI pipeline:**
- Existing stage: `pytest -m "not integration"` (no DB required).
- New stage: `docker compose up -d postgres` → healthcheck wait → `pytest -m integration --timeout=60` → teardown.

---

## Risks

- **F-2 password reuse edge case:** If `existing_booking.vm_password` is populated but the Terraform workspace was destroyed between retries by a racing teardown beat task, the old password is reused against a newly-provisioned VM with a fresh password. Mitigation: the teardown beat task only acts on RELEASED/FAILED bookings; a booking in RETRY/CONFIGURING is neither, so the race window is extremely narrow. Document in the function docstring.
- **F-6 duplicate route registration:** Including each router twice doubles route entries in Starlette's trie. At ~30 routes × 3 routers this is negligible; verify Starlette logs no warnings about duplicate operations.
- **F-7 CI Docker availability:** If CI runs inside a container without Docker-in-Docker, use a `docker-compose.ci.yml` override that exposes Postgres on a fixed port rather than spinning a new container. Document in `docs/development.md`.
- **F-8 test patch paths:** Some `tests/test_admin_*` tests patch module-level names by path (e.g. `mocker.patch("app.presentation.routes.admin._booking_repo")`). Verify patch targets still resolve after the import-from-deps change; update to `mocker.patch("app.presentation.deps.booking_repo")` if needed.
- **F-9 mutable fields in frozen dataclasses:** `VMDetails.extra_vars: dict` is mutable, which is incompatible with a truly immutable `frozen=True`. Accepted trade-off: use `frozen=True` with `config_roles: tuple` (converted in `_to_entity`) and document that `extra_vars` is a shallow-mutable field. Alternatively use `types.MappingProxyType` for `extra_vars`; decide at implementation time.

---

## Sequencing

1. **F-1 + F-2** — implement in parallel (both in `provision.py`), ship as one PR. Live bugs; highest priority.
2. **F-3** — immediately after; no dependencies. Half-day change. Batch with F-4.
3. **F-4** — one-liner infra change; no code dependencies. Batch with F-3 in the same PR.
4. **F-5** — `environment_repo.py` only; no dependencies. Add query-count assertion before merging.
5. **F-6** — after F-3 and F-5 are merged so the router layer is stable.
6. **F-7** — start after F-5 merges. This is the gate for F-8 and F-9: do not merge either until F-7 CI stage is green.
7. **F-8** — requires F-7 green. Ship in sub-PRs: `auth.py` first (smallest) → `api.py` → `admin.py` + `ForceReleaseBookingUseCase`.
8. **F-9** — requires F-7 green (integration tests validate `_to_entity` VO population). Can be worked in parallel with F-8's later PRs. `ResourceFootprint` usage in `create_booking.py` and `quota_repo.py` requires care; F-7's `test_quota_concurrent_writes.py` provides the safety net.
