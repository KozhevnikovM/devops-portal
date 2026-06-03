# Code Quality Remediation Plan

Source: code-quality review of the project at branch `feature/134/v060-docs` (v0.6.0).
Status: **planning only — no code to be written until each item below is approved per the
CLAUDE.md bugfix/feature process.**

This document describes *all* remediation phases. Items are grouped by priority and risk,
one branch per issue (per the git workflow). Bugfixes get a `docs/bugfix/` doc + a regression
test that fails before and passes after; refactors/features get a `docs/features/` doc.

## Severity summary

| # | Finding | Type | Phase | Branch |
|---|---------|------|-------|--------|
| 1 | `GET /bookings` leaks every user's VM password & IP | Security | 1 | `bugfix/credential-exposure-bookings-list` |
| 2 | `PATCH /api/hardware` silently drops disk updates (`disk_mb` vs `hdd_mb`) | Bug | 1 | `bugfix/hardware-disk-update-noop` |
| 9 | `extend_booking` checks status before ownership (state leak) | Bug | 1 | `bugfix/extend-ownership-check-order` |
| 3 | Quota check races for users with no quota row | Concurrency | 2 | `bugfix/quota-race-default-users` |
| 4 | SSD quota unenforced → replace with drive-type quotas | Feature | 2 | `feature/drive-type-quota` |
| 5 | Provision worker holds a DB connection for the whole apply | Bug | 2 | `bugfix/provision-session-lifetime` |
| 6 | Session cookie missing `Secure` | Security | 2 | `bugfix/session-cookie-secure` |
| 7 | Static-VM credentials stored/returned in plaintext | Decision | 2 | (folded into #1 docs) |
| 8 | Domain `PermissionError` shadows the builtin | Refactor | 3 | `refactor/permission-error-rename` |
| 10 | Application layer imports concrete Celery tasks | Refactor | 3 | `refactor/task-dispatch-protocol` |
| 11 | Duplicated pooled-booking use cases | Refactor | 3 | `refactor/unify-pooled-booking` |
| 12 | `Booking` entity is a 28-field god-object read model | Refactor | 3 | (with #11) |
| 13 | `datetime(9999,…)` permanent-expiry sentinel duplicated 5× | Refactor | 3 | `refactor/permanent-expiry-sentinel` |
| 14 | CLAUDE.md documents a non-existent SSE `status-stream` endpoint | Docs | 4 | `docs/correct-sse-architecture` |

---

## Phase 1 — Security & correctness bugs (ship first)

Small, contained, high-confidence fixes. Each is an independent branch.

### 1. `bugfix/credential-exposure-bookings-list`
- **Root cause:** `GET /bookings` (`app/presentation/routes/bookings.py:119-148`) calls
  `_repo.list_all(session)` with no owner scoping and serializes `vm_password` and `vm_ip`
  for every booking. Any authenticated user (cookie or API key) can read every other user's
  VM credentials. The HTML pages default to `filter="mine"`, but this JSON route has no guard,
  unlike `release_booking` and `get_booking_audit`, which both check
  `booking.user_id != current_user.id and role != "admin"`.
- **Changes:**
  - Scope the list to `current_user` unless the caller is admin (reuse the existing owner/admin
    check pattern).
  - Remove `vm_password` from the list payload entirely; it should only be retrievable on the
    owner-scoped single-booking view. (Covers finding #7 for VM passwords — see Phase 2 for the
    static-VM credential decision.)
- **Tests:** non-owner `GET /bookings` returns only their own rows and no foreign passwords;
  admin still sees all.
- **Docs:** `api-reference.md` (document the scoping + removed field).

### 2. `bugfix/hardware-disk-update-noop`
- **Root cause:** `HWConfigUpdate.disk_mb` (`app/presentation/routes/api.py:55`) doesn't match
  the model column `hdd_mb` (`models.py:30`). `model_dump(exclude_none=True)` yields
  `{"disk_mb": …}`, and `hw_config_repo.update` does `setattr(model, "disk_mb", value)`, which
  SQLAlchemy treats as a stray Python attribute that is never persisted. Disk edits via the API
  silently no-op.
- **Changes:** rename the schema field `disk_mb` → `hdd_mb` in `VMConfigUpdate`.
  > Note: the Phase 2 drive-type feature (#4) will rename `hdd_mb` again to a generic disk field.
  > Ship this quick correctness fix first so the endpoint stops silently dropping data; the
  > redesign revisits naming.
- **Tests:** `PATCH /api/hardware/{id}` with a new disk value persists and is returned.
- **Docs:** `api-reference.md`.

### 3. `bugfix/extend-ownership-check-order`
- **Root cause:** `ExtendBookingUseCase.execute` (`app/application/use_cases/extend_booking.py:23-28`)
  checks `status` and `ttl_minutes` before ownership, so a non-owner receives
  "can only extend READY bookings" instead of a permission error — leaking booking state.
- **Changes:** reorder so the ownership check runs first.
- **Tests:** non-owner extend → 403 regardless of the booking's status/ttl.

---

## Phase 2 — Concurrency & data hardening

### 3 (race). `bugfix/quota-race-default-users`
- **Root cause:** `quota_repo.get_limits_for_update` (`quota_repo.py:79-86`) issues
  `SELECT … FOR UPDATE`, but when the user has no `QuotaModel` row (the default-quota case)
  `scalar_one_or_none()` locks nothing. Two concurrent `POST /bookings` from the same
  default-quota user can both pass the check and exceed quota. The "inside the same transaction"
  comment in `create_booking.py:43` assumes a lock that isn't held for default users.
- **Decision:** **lazy-seed the quota row.** On the booking path, upsert a quota row from the
  configured defaults (idempotent `ON CONFLICT DO NOTHING`), then re-select it `FOR UPDATE` so
  the lock is always effective.
- **Changes:**
  - Add a `quota_repo` method that ensures a row exists (seed from `_default_limits()`), called
    before `get_limits_for_update` in `CreateBookingUseCase`.
  - Keep the seeded values equal to the current defaults so behaviour is unchanged for existing
    users; admins can still override later.
- **Tests:** two concurrent bookings for a default-quota user cannot exceed the limit (simulate
  with overlapping transactions); a quota row exists after the first booking.
- **Docs:** none user-facing.

### 4. `feature/drive-type-quota` (replaces the SSD quota)
- **Goal:** remove the always-zero SSD quota dimension and model disk capacity by **drive type**.
  Each hardware config has one disk with a `drive_type` of `SSD` or `HDD`; a booking's disk size
  counts toward the matching disk quota.
- **Domain / model changes:**
  - New `DriveType` enum (`SSD`, `HDD`) in `app/domain/enums.py`.
  - `HWConfigModel`: add `drive_type` column (default `HDD` for existing rows); rename `hdd_mb`
    to a generic `disk_mb` (the size; its type is now `drive_type`). Update `HWConfig` entity and
    `_to_entity`.
  - `QuotaModel`: drop `max_ssd_gb` as a dead dimension **or** repurpose the two columns as the
    two drive-type quotas (`max_ssd_gb`, `max_hdd_gb`) that are now genuinely enforced.
    Recommended: keep both columns, enforce each against the disk of bookings whose config has the
    matching `drive_type`.
  - `BookingModel`: the booking already snapshots `hdd_mb`; snapshot the config's `drive_type`
    too (so quota accounting and history survive config edits).
- **Quota enforcement (`quota_repo` + `create_booking`):**
  - `count_active_resources` sums disk per drive type (SSD total, HDD total) from active bookings.
  - The quota check compares the new booking's disk against the quota for its config's drive type.
  - Remove the hardcoded `"ssd_gb": 0`.
- **Migrations (new revisions only — never edit an applied migration):**
  - `00NN_hw_config_drive_type` — add `drive_type` to `hw_configs` (default `HDD`), rename
    `hdd_mb` → `disk_mb`.
  - `00NN_booking_drive_type` — add `drive_type` snapshot to `bookings`, backfill `HDD`.
  - Quota column change if we drop `max_ssd_gb`.
- **UI / API:**
  - Admin hardware-config form: add a Drive type selector (SSD/HDD); the disk input stays
    (in GB, per the existing convention).
  - Booking form / quota display: show usage per drive type.
  - `api.py` schemas (`HWConfigCreate/Update/Response`): add `drive_type`, use `disk_mb`
    (subsumes the Phase 1 `disk_mb`→`hdd_mb` rename — this becomes the final name).
  - Admin quota UI/API: relabel SSD/HDD quotas as drive-type quotas.
- **Tests:** disk quota is enforced per drive type; an SSD config counts only toward the SSD
  quota; migration chain test stays green; admin can set drive type.
- **Docs:** `admin-guide.md` (hardware config + quota), `api-reference.md` (schema fields).
- **Note:** this is the largest item and supersedes findings #2 and #4. Sequence it *after* the
  Phase 1 `disk_mb` quick fix to avoid leaving the endpoint broken in the interim.

### 5. `bugfix/provision-session-lifetime`
- **Root cause:** `provision_vm_task` (`app/tasks/provision.py:77-119`) holds one
  `SyncSessionLocal` open across `asyncio.run(terraform.apply(...))` (minutes long), with
  `_on_progress` committing on the same long-lived transaction — pinning a pool connection per
  in-flight provision and risking idle-in-transaction timeouts.
- **Changes:** open short-lived sessions per status/progress write instead of one wrapping
  session; the long terraform call runs with no DB transaction held. Mirror in
  `teardown_vm_task` if the same pattern applies.
- **Tests:** provision/teardown status transitions still occur in order; add a test asserting
  writes don't depend on a single wrapping transaction.

### 6. `bugfix/session-cookie-secure`
- **Root cause:** login `set_cookie` (`auth.py:71-76`) sets `httponly` + `samesite="lax"` but
  not `secure=True`; the session id can travel over cleartext HTTP.
- **Changes:** add `secure=...` driven by a new setting `SESSION_COOKIE_SECURE` (default `True`,
  toggle off for local dev over HTTP). Apply to both `set_cookie` and `delete_cookie`.
- **Tests:** cookie carries `Secure` when the setting is on.
- **Docs:** `admin-guide.md` (TLS/deployment note).

### 7. Static-VM credential handling (decision, folded into #1 docs)
- Static-VM `password`/`ssh_key` are plaintext columns and are returned in booking JSON. After #1
  scopes the list endpoint, the remaining decision is whether to encrypt at rest and/or restrict
  which views expose them. Capture the decision in the `api-reference.md` update for #1; no code
  change planned unless we choose encryption (separate branch if so).

---

## Phase 3 — Refactors (no behaviour change)

Each gets a short `docs/features/` note rather than a bugfix doc.

### 8. `refactor/permission-error-rename`
- Rename domain `PermissionError` (`app/domain/exceptions.py:25`) → `BookingPermissionError` so it
  no longer shadows the builtin; update raises, imports, and `except` sites (notably
  `bookings.py` and `extend_booking.py`).

### 13. `refactor/permanent-expiry-sentinel`
- Extract a single `PERMANENT_EXPIRES_AT` constant / helper and replace the 5 duplicated
  `datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)` literals (create_booking,
  reserve_static_vm, book_namespace, `booking_repo._assign_resource_and_ready`, extend).

### 11 + 12. `refactor/unify-pooled-booking`
- Collapse `BookNamespaceUseCase` and `ReserveStaticVMUseCase` (≈90% identical) into one
  `ResourceType`-parameterized use case over the existing `_POOLED_RESOURCE` machinery in
  `booking_repo`. Aligns with the roadmap note on unifying pooled booking.
- As part of this, reduce the `Booking` entity's denormalized display fields (#12): consider a
  separate read/view model so static-VM/namespace fields aren't manually attached after
  `create()`. Scope carefully — touches templates and JSON serializers.

### 10. `refactor/task-dispatch-protocol`
- Introduce a task-dispatch Protocol injected into the use cases so the application layer no
  longer imports concrete Celery tasks (`create_booking.py:14`) and the route lazy-import in
  `bookings.py:319` can go away. Restores the one-way dependency rule.

---

## Phase 4 — Docs

### 14. `docs/correct-sse-architecture`
- CLAUDE.md describes a `GET /bookings/{id}/status-stream` SSE endpoint driving live row updates,
  but no such route exists — the UI polls `/bookings/{id}/row` (hence `_SuppressRowPolling` in
  `main.py`). Correct the Architecture section to describe polling, and remove the SSE claims
  (or note SSE as a possible future change).

---

## Sequencing & dependencies

1. **Phase 1** first — independent, low-risk, immediately closes the credential leak and the
   silent disk-update bug.
2. **Phase 2** next. Order within the phase: `quota-race` and `session-cookie` are small and
   independent; `provision-session-lifetime` is independent; **`feature/drive-type-quota` last**
   in the phase since it's the biggest and supersedes the #2 naming.
3. **Phase 3** refactors after the behaviour-affecting work lands, to avoid churn/merge conflicts.
   `permanent-expiry-sentinel` and `permission-error-rename` are trivial and can slot in anytime.
4. **Phase 4** docs whenever convenient.

## Out of scope / open questions
- CSRF protection for cookie-auth state-changing routes (currently relies on `SameSite=lax`).
  Not in this plan; raise separately if required.
- Encryption-at-rest for static-VM credentials (#7) — pending the decision noted above.
- Whether to drop `max_ssd_gb` entirely or repurpose both quota columns as drive-type quotas
  (#4) — recommended approach is to keep both columns and enforce per drive type.
