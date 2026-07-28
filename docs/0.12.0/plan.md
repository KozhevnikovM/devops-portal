# v0.12.0 Plan

## Goal

v0.12.0 ships one presentation-only hardening pass that's already fully speced and
approved (#353's responsive UI plan) and one API gap closed for CI/CD-driven environment
ordering (#352). It also picks up the highest-value structural item explicitly deferred
from v0.11.0 — the `SyncBookingRepositoryPort` — now that F-7's Postgres integration
tier (merged in v0.11.0) exists to verify it. A fourth item closes a UX gap on the
booking form itself: Ansible role selection, already possible via the API, is now also
possible from the browser (#357). Everything else deferred from v0.11.0 or sitting in
the open-issue backlog is listed below with a reason it isn't in this release, so nothing
is silently dropped.

---

## Scope

### In scope

**F-1: Responsive UI hardening pass** — presentation-only, fully speced in
`docs/features/responsive-ui-hardening.md` (approved via #353). No API/DB changes.

**F-2: Order-time Ansible variables for environments** (#352) — closes the gap where a
standalone VM booking accepts `vars` but an environment order doesn't, forcing a
separate blueprint per variant (e.g. a custom git branch).

**F-3: `SyncBookingRepositoryPort`** (carried forward from v0.11.0's P1-C) — the Celery
worker side (`sync_get`, `sync_update_status`, etc. in `booking_repo.py`) has no
`Protocol` today, unlike the async side (`BookingRepositoryPort` in `app/application/ports.py`).
v0.11.0 deferred this specifically until the Postgres integration tier existed to verify
it under real transaction semantics — that tier shipped in F-7, so this is now unblocked.

**F-4: Ansible role and variable input on the VM booking form** (#357) — fully speced in
`docs/features/vm-booking-role-picker.md`. `POST /api/bookings` already accepts `roles`
and `vars` fields; the browser **Virtual Machines** tab has no way to submit either.
Adds a checkbox-list role picker and a YAML vars textarea (reusing the existing
`default_vars` textarea pattern from the admin catalog) to `partials/booking_form.html`
(VM/provisioned bookings only). Also de-duplicates two existing pieces of logic that
`api_bookings.py` already has inline: role-name resolution, extracted into a shared
helper both routes call, and var-name validation, extracted into a pure
`app/domain/validation.py` function that `order_environment.py`'s `_validate_extra_vars`
also switches to (currently duplicated between the two call sites).

### Out of scope (deferred, with reason)

- **`OrderEnvironmentUseCase` → Process Manager decomposition** (v0.11.0's P2-A) — still
  XL effort; no new pressure forcing it this release. Revisit once F-2 (order-time vars)
  has landed and the use case's shape has settled again.
- **Domain events for queue-promotion / audit-write** (v0.11.0's P2-B) — still correct
  direction, still not urgent; no consumer needs it yet.
- **`RecoverStuckBookingsUseCase` extraction from `main.py` lifespan** — small, valid
  finding, low blast radius; bumped again in favor of F-1/F-2/F-3, not because it's hard.
- **Fernet encryption of `vm_password` / `static_vms.password`** (v0.11.0's S3) — needs a
  migration and a secret-management decision doc of its own; still not scoped.
- **C4 context/component diagrams** — documentation-only, no code dependency; kept out
  so it doesn't compete for review bandwidth with F-1/F-2/F-3.
- **Formal `UserRole`/Policy VO** — not blocking anything concrete yet.
- **#77 (permanent-booking keep-alive), #78 (user email), #79 (password reset via
  email)** — all three are old backlog items (originally scoped "part of v0.3.0") and
  chain together (#79 depends on #78). Each is a full feature (new migration, new
  routes, new beat tasks for #77; SMTP integration for #79). Bundling all three into
  v0.12.0 alongside F-1–F-3 would make this a much larger release than the last one;
  flagging as the leading candidate for **v0.13.0** rather than folding in now.
- **#73 (upload custom Terraform modules) / #85 (custom admin password in Terraform)**
  — neither is scoped enough to plan yet. #73's own issue body says "TODO: write
  terraform example first"; #85 is an example snippet, not a stated requirement. Needs
  a conversation with the reporter before a feature doc can be written.

---

## Features

### F-1: Responsive UI Hardening Pass

**Review ref:** #353

**What:** See `docs/features/responsive-ui-hardening.md` for the full plan — already
written and approved. Summary: add `sm:`/`md:`/`lg:`/`xl:`/`2xl:` Tailwind variants across
all 21 templates (currently zero anywhere in the codebase); wrap the ten un-wrapped
tables in `overflow-x-auto`; fix the dropdown-clipping interaction that fix would
otherwise introduce (`loop.first` → open first row's `⋮` menu downward); widen `<main>`'s
max-width on a tiered scale for large/ultrawide viewports; make ~50 fixed-width form
fields responsive; fix `login.html`'s fixed-width card below ~352px.

**Why:** Zero responsive coverage today means the portal is usable but not well-behaved
outside a narrow desktop-width band. The table-overflow and dropdown-clipping fixes are
real geometry bugs, not speculative polish.

**Acceptance criteria:** as listed in the feature doc's own Verification section
(Playwright pass across 375/768/1024/1440/1920/2560+ widths, no page-level horizontal
overflow, dropdown menus not clipped, forms reflow correctly).

---

### F-2: Order-Time Ansible Variables for Environments

**Review ref:** #352

**What:**

1. `app/presentation/routes/api_environments.py`: add `item_vars: dict[str, dict] | None
   = None` to `OrderEnvironmentRequest` — keyed by the blueprint item's `label` (not one
   flat dict for the whole stack), since a blueprint can have multiple VM items with
   different roles that need different overrides (e.g. `{"web": {"git_branch":
   "feature-x"}}`). A blueprint item with no matching key is unaffected.

2. `app/application/use_cases/order_environment.py`: thread `item_vars` through
   `OrderEnvironmentUseCase.execute()` into `_resolve_item()`. Merge rule: **order-time
   vars override the blueprint's own `spec.vars` on key conflict** (`{**blueprint_vars,
   **item_vars.get(item.label, {})}`) — explicit caller input wins over the blueprint
   default, consistent with how other explicit inputs in this codebase (e.g. an explicit
   `image_id` winning over a name lookup in `create_booking`) take precedence over
   catalog/blueprint defaults.

3. Reuse the existing `_validate_extra_vars()` name-pattern check
   (`[a-zA-Z_][a-zA-Z0-9_]*`) against the merged dict, not just the blueprint's own vars.

4. **Not in scope for this pass:** exposing `item_vars` on the HTMX order form
   (`POST /environments`, browser UI). The motivating use case (CI/CD pipelines
   parameterizing a build) is API-driven; the browser form can pick this up later if a
   concrete manual-ordering need shows up.

**Why:** Every stack variant (e.g. "same blueprint, build from `feature/x`") currently
needs its own blueprint maintained by an admin. This lets a pipeline parameterize one
blueprint per call instead.

**Acceptance criteria:**
- `POST /api/environments` with `item_vars={"web": {"git_branch": "feature-x"}}` results
  in the `web` item's role vars containing `portal.git_branch = "feature-x"`, overriding
  any blueprint-defined `git_branch`.
- An item not named in `item_vars` behaves exactly as before (blueprint's own vars only).
- Invalid var names in `item_vars` raise the same `400` as invalid blueprint-vars do
  today.
- `docs/api-reference.md`'s `POST /api/environments` section documents `item_vars`.

---

### F-3: `SyncBookingRepositoryPort`

**Review ref:** v0.11.0 plan, P1-C

**What:** Add a `SyncBookingRepositoryPort` Protocol in `app/application/ports.py`
modeling the `sync_*` methods `BookingRepository` already implements for the Celery
worker path (`sync_get`, `sync_update_status`, `sync_set_status_message`, etc. — enumerate
from `app/tasks/provision.py` and `app/tasks/teardown.py`'s actual call sites). Add the
conformance test to `tests/test_repository_ports.py` alongside the existing async-port
checks.

**Why:** The async side has had a `Protocol` since v0.9.0 (`BookingRepositoryPort`); the
sync/Celery side never got one, so nothing enforces that `provision.py`/`teardown.py` call
only the methods the port promises. v0.11.0 deferred this specifically until the
Postgres integration tier (F-7) existed to exercise the sync path's transaction
semantics under a real DB — that tier is now in place.

**Acceptance criteria:**
- `SyncBookingRepositoryPort` is `@runtime_checkable`; `BookingRepository` structurally
  satisfies it (new `test_repo_satisfies_port` parametrization).
- `app/tasks/provision.py` and `app/tasks/teardown.py` type-hint their `repo` parameter
  against the new port instead of the concrete class.
- No behavior change — this is a typing/interface addition only.

---

### F-4: Ansible Role and Variable Input on the VM Booking Form

**Review ref:** #357 (roles); vars folded in during plan review — same gap, same form.

**What:** See `docs/features/vm-booking-role-picker.md` for the full plan. Summary: add
a checkbox-list role picker and a YAML vars textarea (reusing the admin catalog's
existing `default_vars` textarea UX) to `partials/booking_form.html`'s
`#provisioned-fields` block (VM bookings only — Static VM/Namespace unaffected).
Extract two pieces of logic `api_bookings.py` already has inline, so both routes share
one implementation:
- Role-name → `config_roles` resolution →
  `app/application/use_cases/_roles.py::resolve_config_roles`, raising the existing
  (currently unused) `RoleNotFoundError` domain exception instead of `api_bookings.py`
  raising `HTTPException` inline.
- Var-name validation (`^[a-zA-Z_][a-zA-Z0-9_]*$`) → `app/domain/validation.py
  ::validate_var_names`, a pure function with no exception coupling; also adopted by
  `order_environment.py`'s `_validate_extra_vars`, which duplicates the same regex today.

**Why:** `POST /api/bookings` has accepted `roles` and `vars` fields since they were
added for API/CI callers, but the HTML form never got either — a portal user booking a
VM through the browser has no way to attach an Ansible role or override a variable
today.

**Acceptance criteria:** as listed in the feature doc — no roles selected / no vars
entered behaves exactly as before; an unknown/stale role name, invalid YAML, or an
invalid variable name re-renders the form with an error banner (input preserved) instead
of a 500; Static VM/Namespace bookings unaffected; no DB migration, no API change.

---

## DB Migrations

None. F-2 adds a request field only (no new columns); F-3 is a typing-only change; F-1
and F-4 are templates/routes only.

---

## API Changes

- F-2: `POST /api/environments` gains an optional `item_vars` field (see above). No
  breaking change — omitting it behaves exactly as before.
- F-1, F-3, F-4: none. (F-4 wires existing `POST /api/bookings` fields into the HTML
  form; the API itself is unchanged.)

---

## Testing Plan

- F-1: Playwright verification pass (see feature doc) — no new `pytest` coverage, this
  is presentation-only per that doc's own "Verification" section.
- F-2: unit tests for `OrderEnvironmentUseCase` covering per-item override, no-override
  passthrough, and the merge-precedence rule; route-plumbing test for `item_vars` on
  `POST /api/environments`.
- F-3: `test_repository_ports.py` parametrization; no behavior tests needed since this
  is interface-only.
- F-4: unit tests for `resolve_config_roles` (found roles, unknown-name error),
  `validate_var_names`, and `_parse_vars_yaml`; route tests for `POST /bookings` with
  roles and/or vars (success + unknown-role / invalid-var error banners). Full suite run
  to confirm the `api_bookings.py`/`order_environment.py` extractions are
  behaviour-preserving.

---

## Sequencing

1. **F-1** — no dependencies, presentation-only, already fully speced. Ship first;
   lowest risk, fastest to review.
2. **F-2** — no dependencies on F-1 or F-3; can be worked in parallel with F-1.
3. **F-3** — no dependencies on F-1 or F-2; can also be worked in parallel. Gate: verify
   against F-7's integration tier (`pytest -m integration`) before merging, since that
   tier is exactly what makes this safe to do now.
4. **F-4** — no dependencies on F-1/F-2/F-3; can be worked in parallel with any of them.
