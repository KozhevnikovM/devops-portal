# v0.7.0 Plan: Hardening — Security, Correctness & Code Quality

## Context

v0.6.0 shipped the static-VM pool and the booking queue. v0.7.0 has **no new product surface**;
it consolidates the findings from two reviews of the v0.6.0 codebase into one remediation
release:

- [`docs/security-review.md`](../security-review.md) — auth/routing/repository/task/Terraform review.
- [`docs/code-quality-remediation-plan.md`](../code-quality-remediation-plan.md) — correctness, concurrency, and design review.

**Theme:** close the credential-exposure leak and the other access-control gaps first, then
correctness/concurrency bugs, then defensive hardening, then the one real feature (drive-type
quotas) and the no-behaviour-change refactors, and finally a docs correction.

Two findings appear in **both** source docs — credential exposure and the `Secure` cookie flag.
They are **single items** here; whichever branch ships first closes both. The full per-finding
root-cause detail lives in the two source docs; this plan is the consolidated, sequenced,
branch-per-issue view. Every branch follows the CLAUDE.md flow: cut from fresh `main`, add a
`docs/bugfix/` (bugs) or `docs/features/` (features/refactors) doc, get approval, implement with
a regression test that fails before and passes after.

## Consolidated findings

| # | Finding | Type | Sev | Phase | Branch | Source |
|---|---------|------|-----|-------|--------|--------|
| 1 | `GET /bookings` leaks every user's VM password / IP / static-VM creds | Security | **High** | 1 | `bugfix/credential-exposure-bookings-list` | SEC#1 = CQ#1 (+#7) |
| 2 | `GET /bookings/{id}/row` has no ownership check (IDOR) | Security | Med | 1 | `bugfix/booking-row-ownership-check` | SEC#2 |
| 3 | Session cookie missing `Secure` | Security | Med | 1 | `bugfix/session-cookie-secure` | SEC#3 = CQ#6 |
| 4 | `PATCH /api/hardware` silently drops disk updates (`disk_mb` vs `hdd_mb`) | Bug | — | 1 | `bugfix/hardware-disk-update-noop` | CQ#2 |
| 5 | `extend_booking` checks status before ownership (state leak) | Bug | — | 1 | `bugfix/extend-ownership-check-order` | CQ#9 |
| 6 | Quota check races for default-quota users (no row → no lock) | Concurrency | — | 2 | `bugfix/quota-race-default-users` | CQ#3 |
| 7 | Provision worker holds a DB connection across the whole apply | Bug | — | 2 | `bugfix/provision-session-lifetime` | CQ#5 |
| 8 | HTML injection in admin error fragments | Security | Low | 2 | `bugfix/admin-error-html-escape` | SEC#4 |
| 9 | HCL/template injection in Terraform workspace files | Security | Low | 2 | `bugfix/terraform-tfvars-json` | SEC#5 |
| 10 | Username enumeration via login timing | Security | Low | 2 | `bugfix/login-timing-enumeration` | SEC#6 |
| 11 | SSD quota unenforced → drive-type quotas | Feature | — | 3 | `feature/drive-type-quota` | CQ#4 (supersedes CQ#2 naming) |
| 12 | Domain `PermissionError` shadows the builtin | Refactor | — | 4 | `refactor/permission-error-rename` | CQ#8 |
| 13 | `datetime(9999,…)` permanent-expiry sentinel duplicated ~5× | Refactor | — | 4 | `refactor/permanent-expiry-sentinel` | CQ#13 |
| 14 | Duplicated pooled-booking use cases + 28-field `Booking` read model | Refactor | — | 4 | `refactor/unify-pooled-booking` | CQ#11/#12 |
| 15 | Application layer imports concrete Celery tasks | Refactor | — | 4 | `refactor/task-dispatch-protocol` | CQ#10 |
| 16 | Enforce admin password change (default `changeme`) | Hardening | — | 5 | `feature/enforce-admin-password-change` | SEC#7 |
| 17 | Vended credentials stored in plaintext at rest | Decision | — | 5 | decision doc only | SEC#8 = CQ#7 |
| 18 | CLAUDE.md documents a non-existent SSE `status-stream` endpoint | Docs | — | 6 | `docs/correct-sse-architecture` | CQ#14 |

---

## Phase 1 — Access control & correctness (ship first)

High-confidence, contained fixes that close real exposure and silent data loss.

### 1. `bugfix/credential-exposure-bookings-list` — **High**
- **Root cause:** `GET /bookings` calls `_repo.list_all(session)` with no owner scoping and
  serializes `vm_password`, `vm_ip`, and static-VM `host`/`username` for **every** booking — any
  authenticated user (cookie or `dp_` API key) can read every other user's credentials. The HTML
  views gate creds behind `is_owner or admin`; this JSON route is the one path with no guard.
- **Change:** scope to `current_user` via `list_by_user` unless admin; **remove `vm_password`
  (and any static-VM secret) from the list payload** — secrets only on the owner-scoped single
  view. Folds in CQ#7's VM-password concern; the static-VM at-rest question is finding #17.
- **Test:** non-owner sees only their own rows and no foreign secrets; admin sees all.
- **Docs:** `api-reference.md` (owner scoping + removed field).

### 2. `bugfix/booking-row-ownership-check` — Med
- **Root cause:** `GET /bookings/{id}/row` fetches any booking by UUID with no ownership check;
  `vm_ip`, status, owner, image name leak to any authenticated caller who supplies a UUID.
- **Change:** apply the same `owner or admin` guard as `release_booking`; 403 otherwise.
- **Test:** non-owner rejected; owner + admin succeed. (Mind the 3 s row-poll still works for the owner.)

### 3. `bugfix/session-cookie-secure` — Med
- **Root cause:** login cookie sets `httponly` + `samesite="lax"` but not `secure` → session id
  can travel over cleartext HTTP.
- **Change:** `secure=True` driven by new `SESSION_COOKIE_SECURE: bool = True` (toggle off for
  local HTTP dev); apply to `set_cookie` **and** `delete_cookie`. Completes the cookie-CSRF posture.
- **Test:** `Set-Cookie` carries `Secure` when on, absent when off.
- **Docs:** `admin-guide.md` (TLS/deployment note + new env var).

### 4. `bugfix/hardware-disk-update-noop`
- **Root cause:** `HWConfigUpdate.disk_mb` doesn't match the model column `hdd_mb`;
  `setattr(model, "disk_mb", …)` is a stray attribute that never persists → disk edits silently no-op.
- **Change:** rename the schema field `disk_mb` → `hdd_mb`. (The Phase 3 drive-type feature renames
  it again to a generic `disk_mb`; ship this quick fix first so the endpoint stops dropping data.)
- **Test:** `PATCH /api/hardware/{id}` with a new disk value persists and is returned.
- **Docs:** `api-reference.md`.

### 5. `bugfix/extend-ownership-check-order`
- **Root cause:** `ExtendBookingUseCase.execute` checks status/ttl before ownership → a non-owner
  gets "can only extend READY bookings", leaking state.
- **Change:** run the ownership check first.
- **Test:** non-owner extend → 403 regardless of status/ttl.

---

## Phase 2 — Concurrency, injection & enumeration hardening

### 6. `bugfix/quota-race-default-users` — Concurrency
- **Root cause:** `quota_repo.get_limits_for_update` does `SELECT … FOR UPDATE`, but a
  default-quota user has no `QuotaModel` row, so `scalar_one_or_none()` locks nothing — two
  concurrent `POST /bookings` can both pass and exceed quota.
- **Decision:** **lazy-seed** the quota row from configured defaults (idempotent
  `ON CONFLICT DO NOTHING`), then re-select `FOR UPDATE` so the lock is always effective. Seeded
  values equal current defaults (no behaviour change).
- **Test:** two overlapping transactions for a default-quota user cannot exceed the limit; a row
  exists after the first booking.

### 7. `bugfix/provision-session-lifetime`
- **Root cause:** `provision_vm_task` holds one `SyncSessionLocal` open across the
  minutes-long `terraform.apply`, with progress writes committing on the same long-lived
  transaction → pins a pool connection, risks idle-in-transaction timeouts.
- **Change:** short-lived session per status/progress write; no DB transaction held during the
  terraform call. Mirror in `teardown_vm_task` if applicable.
- **Test:** status transitions still occur in order and don't depend on one wrapping transaction.

### 8. `bugfix/admin-error-html-escape` — Low
- **Root cause:** several admin handlers build error HTML via f-strings interpolating unescaped
  user input, returned as raw `HTMLResponse` (auth.py, admin.py ×4). Admin-only → effectively
  self-XSS, but `<img src=x onerror=…>` injects live markup.
- **Change:** `markupsafe.escape()` the interpolated value (or render a tiny template fragment).
- **Test:** a duplicate name containing `<script>` returns an escaped body.

### 9. `bugfix/terraform-tfvars-json` — Low
- **Root cause:** `TerraformVcdAdapter._write_workspace` interpolates values into `main.tf` /
  `terraform.tfvars` with f-strings; `vapp_template_id` (admin free-text) and VCD env settings can
  break quoting / inject HCL.
- **Change:** write variables as `terraform.tfvars.json` via `json.dump`; keep provider/module
  `main.tf` static.
- **Test:** a value containing `"` round-trips as a literal string.

### 10. `bugfix/login-timing-enumeration` — Low
- **Root cause:** `login` skips `bcrypt.checkpw` on a username miss → missing users respond
  measurably faster (enumeration oracle).
- **Change:** compare against a fixed dummy bcrypt hash on the miss path to equalize work.
- **Test:** the miss path still calls `bcrypt.checkpw` once (spy); login outcome unchanged.

---

## Phase 3 — Drive-type quotas (the one feature)

### 11. `feature/drive-type-quota` — supersedes the SSD-quota dead dimension
- **Goal:** drop the always-zero SSD quota and model disk capacity by **drive type**. Each
  hardware config has a disk with a `drive_type` (`SSD`|`HDD`); a booking's disk counts toward the
  matching drive-type quota.
- **Domain/model:** `DriveType` enum; `HWConfigModel` gains `drive_type` (default `HDD`) and
  `hdd_mb` → generic `disk_mb`; `BookingModel` snapshots `drive_type` (so accounting/history survive
  config edits); `QuotaModel` keeps `max_ssd_gb` + `max_hdd_gb`, now genuinely enforced per type.
- **Enforcement:** `count_active_resources` sums disk per drive type; the check compares the new
  booking's disk against its config's drive-type quota; remove the hardcoded `"ssd_gb": 0`.
- **Migrations (new revisions only):** `hw_config_drive_type` (add `drive_type`, rename
  `hdd_mb`→`disk_mb`), `booking_drive_type` (snapshot + backfill `HDD`).
- **UI/API:** admin hw-config form gains a Drive-type selector; quota display/admin UI relabel
  SSD/HDD as drive-type quotas; `api.py` schemas use the final `disk_mb` + `drive_type`.
- **Tests:** disk quota enforced per drive type; SSD config counts only toward SSD; migration-chain
  test green; admin can set drive type.
- **Docs:** `admin-guide.md`, `api-reference.md`.
- **Note:** largest item; sequence after the Phase 1 `disk_mb`→`hdd_mb` quick fix so the endpoint
  isn't left broken in the interim.

---

## Phase 4 — Refactors (no behaviour change)

Each gets a short `docs/features/` note. Land after the behaviour-affecting work to avoid churn.

- **12. `refactor/permission-error-rename`** — domain `PermissionError` → `BookingPermissionError`
  (stop shadowing the builtin); update raises/imports/`except` sites.
- **13. `refactor/permanent-expiry-sentinel`** — extract one `PERMANENT_EXPIRES_AT` constant/helper,
  replacing the ~5 duplicated `datetime(9999,12,31,…)` literals.
- **14. `refactor/unify-pooled-booking`** — collapse `BookNamespaceUseCase` +
  `ReserveStaticVMUseCase` (~90% identical) into one `ResourceType`-parameterized use case over the
  existing `_POOLED_RESOURCE` machinery; reduce the `Booking` entity's denormalized display fields
  via a separate read/view model (touches templates + JSON serializers — scope carefully).
- **15. `refactor/task-dispatch-protocol`** — inject a task-dispatch Protocol so the application
  layer no longer imports concrete Celery tasks and the route lazy-import goes away (restores the
  one-way dependency rule).

---

## Phase 5 — Hardening & decisions (product input needed)

- **16. `feature/enforce-admin-password-change`** — `ADMIN_PASSWORD` defaults to `changeme`,
  startup only warns. Options: (a) refuse to start when the seeded admin still has the default and
  a `PRODUCTION`/`ENV` flag is set, or (b) force a change on first admin login. Feature doc +
  approval before code.
- **17. Vended-credential encryption-at-rest — decision doc only.** `vm_password` and static-VM
  `password`/`ssh_key` are cleartext; DB/backup access = credential access. Decide: encrypt at rest
  (Fernet/KMS key) vs. accept with documented DB-access controls. No code until a direction is chosen.

---

## Phase 6 — Docs

- **18. `docs/correct-sse-architecture`** — CLAUDE.md describes a `GET /bookings/{id}/status-stream`
  SSE endpoint that doesn't exist; the UI polls `/bookings/{id}/row` (hence `_SuppressRowPolling`).
  Correct the Architecture section to describe polling (note SSE as possible future).

---

## Sequencing & dependencies

1. **Phase 1** first — independent, low-risk; closes the High credential leak, the IDOR, and the
   silent disk-update bug. (#1/#2/#3 access-control & transport batch; #4/#5 correctness.)
2. **Phase 2** next — defensive, independently shippable.
3. **Phase 3** (`drive-type-quota`) after the Phase 1 `disk_mb` quick fix; it's the biggest item
   and finalizes the disk-field naming.
4. **Phase 4** refactors after behaviour-affecting work lands. `permission-error-rename` and
   `permanent-expiry-sentinel` are trivial and can slot in anytime.
5. **Phase 5** needs product decisions — open the decision/feature docs before implementing.
6. **Phase 6** docs whenever convenient.

## Decisions

1. **v0.7.0 scope** — ✅ **confirmed: ship everything (all 6 phases)** in v0.7.0.

Still to confirm at the relevant phase:

2. **Drive-type quota (#11, Phase 3)** — keep both quota columns and enforce per drive type
   *(recommended)*, or drop `max_ssd_gb` entirely?
3. **Vended credentials (#17, Phase 5)** — encrypt at rest, or accept the risk with documented controls?
4. **Admin password (#16, Phase 5)** — refuse-to-start-in-prod vs. force-change-on-first-login?

## Out of scope

- **CSRF tokens** for cookie-auth mutations — relies on `SameSite=Lax`; closing #3 completes the
  posture. Raise separately if a token scheme is required.
- New product features (Environments, Databases, dynamic namespaces) — remain on the post-0.6.0
  roadmap, not part of this hardening release.

## Verified clean (no action)

SQL injection (parameter-bound throughout), bcrypt password hashing with per-hash salt, API-key
handling (128-bit, SHA-256 at rest, never re-echoed), and owner/admin authz on
`release`/`extend`/`audit`.
