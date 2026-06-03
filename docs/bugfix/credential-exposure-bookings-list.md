# Bugfix: `GET /bookings` leaks every user's credentials (#137)

**Severity: High** · Source: SEC#1 = CQ#1 (+#7) · Phase 1, item #1

## Root cause

`GET /bookings` ([`app/presentation/routes/bookings.py`](../../app/presentation/routes/bookings.py))
calls `_repo.list_all(session)` with **no owner scoping** and serializes secret fields for
**every** booking in the system:

- `vm_password` — the provisioned-VM root password
- `vm_ip`, static-VM `host` / `username` — connection details

Any authenticated principal (browser cookie **or** a `dp_` API key) can `GET /bookings` and read
every other user's VM passwords and connection details. This is the one booking path with no
authorization guard: the HTML views (`booking_row.html`) gate credentials behind
`is_owner or admin`, and `release` / `extend` / `audit` all check ownership — but this JSON list
endpoint returns everything to everyone.

## Change

Two independent problems, both fixed:

1. **Owner scoping.** Non-admins get only their own bookings (`list_by_user`); admins keep the
   full list (`list_all`). Mirrors the existing `release_booking` / `get_booking_audit` guard
   (`booking.user_id != current_user.id and current_user.role != "admin"`).

2. **Drop secrets from the list payload.** Remove `vm_password` (and the static-VM `password` /
   `ssh_key` were never in this payload, but neither should `host` / `username` carry a secret
   surface here) from the list serialization entirely. **Secrets are returned only on the
   owner-scoped creation response and the owner/admin-gated row view** — a list endpoint has no
   need to vend them. Non-secret display fields (`vm_ip`, `host`, `username`, `status`,
   `image_name`, …) stay, since after owner-scoping they only describe the caller's own bookings;
   `vm_password` is the one field removed for everyone.

This folds in CQ#7's VM-password concern. The broader static-VM **at-rest** encryption question is
tracked separately as finding #17 (Phase 5, decision doc).

### Resulting `GET /bookings` payload

Per row (owner-scoped; admin sees all rows): `id`, `user_id`, `status`, `resource_type`,
`ttl_minutes`, `expires_at`, `created_at`, `image_id`, `image_name`, `hw_config_id`,
`hw_config_name`, `vm_ip`, `namespace`, `cluster`, `api_url`, `static_vm`, `host`, `username`.
**`vm_password` is removed.**

## Expected behaviour after the fix

- A non-admin calling `GET /bookings` sees **only their own** bookings; no foreign rows, and no
  `vm_password` on any row.
- An admin calling `GET /bookings` still sees **all** bookings, also with no `vm_password`.
- The owner can still retrieve their VM password via the creation response and the
  owner/admin-gated single-row view (`GET /bookings/{id}/row`, which #138 further locks down).

## Test

`tests/test_bookings_list_authz.py`:
- two users each own a booking; user A's `GET /bookings` returns only A's row, not B's.
- the JSON payload contains no `vm_password` key for any caller.
- an admin's `GET /bookings` returns both rows.

## Docs

`docs/api-reference.md` — document owner scoping (admin sees all) and the removed `vm_password`
field on `GET /bookings`.
