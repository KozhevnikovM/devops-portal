# Feature: share a namespace with another user (read-only)

## Goal

Let a namespace owner **grant another portal user read-only visibility** into their namespace's
connection info — `namespace_name`, `cluster_name`, `api_url`. The recipient can see those details
(useful for handing off credentials to a teammate or a pipeline running under a different account)
but cannot release, extend, or take any management action. The share unit is always the
**namespace booking**; if that booking is a child of an environment the recipient also sees the
environment's basic info (name, status).

Applies to:
- **Standalone** namespace bookings (namespace booked on its own).
- **Environment children** (namespace booked as part of an environment stack).

## Data model — `namespace_shares` (migration `0026`)

New table, simple join:

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `booking_id` | UUID FK → `bookings.id` ON DELETE CASCADE | the shared namespace booking |
| `shared_with_user_id` | UUID FK → `users.id` ON DELETE CASCADE | the recipient |
| `created_at` | timestamptz | when the share was created |

Unique constraint on `(booking_id, shared_with_user_id)` — no duplicate shares. ON DELETE CASCADE
means shares are automatically cleaned up when the booking or user is deleted. No new columns on
existing tables.

## Permission model

- **Owner / admin only can share/revoke.** A normal user may only share a booking they own (`user_id`) or are admin. A dispatcher that created a booking can also share it (consistent with `can_manage`).
- **Read-only for the recipient.** A share grants `GET` access to the namespace's connection info and, if the booking has an `environment_id`, the parent environment's name and status. It never grants release, extend, or any write operation.
- **Lifecycle.** Shares live as long as the booking row exists; when the booking is released/expired the row is deleted (cascade), so there is never a dangling share pointing to a released namespace. The sharing user does not need to manually revoke on release.
- **No transitivity.** A recipient cannot re-share.

## API

### Share a namespace
`POST /api/bookings/{booking_id}/shares`
Body: `{ "username": "alice" }` — the recipient's portal username.

Validation:
- `booking_id` must be a live (`status ∉ {RELEASED, FAILED}`) NAMESPACE booking.
- Caller must pass `can_manage` for that booking (owner / admin / creating dispatcher).
- `username` must resolve to an existing, active portal user.
- Cannot share with yourself → `400`.
- Duplicate share → `409` (already shared with that user).

Response `201`: `{ "booking_id": "...", "shared_with": "alice", "created_at": "..." }`

### List shares
`GET /api/bookings/{booking_id}/shares` — owner/admin only. Returns the list of usernames the booking is currently shared with.

### Revoke a share
`DELETE /api/bookings/{booking_id}/shares/{username}` — owner/admin only. `204` on success, `404` if no such share.

### View shared-with-me
`GET /api/namespaces/shared-with-me` — returns the namespace bookings currently shared with the calling user. Each entry: `booking_id`, `namespace`, `cluster`, `api_url`, `status`, `owner_username`, and — if the booking has an `environment_id` — `environment: { id, name, status }` (derived from the booking's sibling children).

## Browser

- **Namespace booking row** (`partials/booking_row.html`) and **environment namespace child row** (`partials/environment_row.html`): add a **Share** action in the actions menu (⋮), shown only to the owner/admin. Opens a small inline form to enter a username; submits `POST /api/bookings/{id}/shares` via HTMX. A **Shared with:** pill list shows current recipients with ✕ buttons that submit `DELETE`.
- **"Shared with me" tab / section**: a dedicated section on the Namespaces page (or a new lightweight `/namespaces/shared` page) listing namespaces shared with the current user, each showing `namespace`, `cluster`, `api_url`, and a link to the parent environment if applicable. Read-only — no release/extend buttons.

## What changes

### Infrastructure
- `alembic/versions/0026_namespace_shares.py` — create `namespace_shares` table.
- `app/infrastructure/database/models.py` — `NamespaceShareModel`.
- `app/infrastructure/repositories/namespace_share_repo.py` — `NamespaceShareRepository`:
  - `create(session, booking_id, shared_with_user_id) -> NamespaceShare`
  - `get(session, booking_id, shared_with_user_id) -> NamespaceShare | None`
  - `list_by_booking(session, booking_id) -> list[NamespaceShare]`
  - `delete(session, booking_id, shared_with_user_id) -> None`
  - `list_shared_with_user(session, user_id) -> list[Booking]` — returns the namespace bookings shared with `user_id`, joined with their namespace details.

### Application
- `app/domain/entities.py` — `NamespaceShare` dataclass: `id`, `booking_id`, `shared_with_user_id`, `shared_with_username`, `created_at`.
- `app/application/use_cases/share_namespace.py` — `ShareNamespaceUseCase.execute(session, booking_id, shared_with_username, caller)`: validate booking is live NAMESPACE + caller can_manage; resolve username → user; create share → `201`.
- `app/application/use_cases/revoke_namespace_share.py` — `RevokeNamespaceShareUseCase.execute(session, booking_id, shared_with_username, caller)`: validate caller can_manage; delete share → `204`.
- `app/application/ports.py` — new `NamespaceShareRepositoryPort` Protocol.
- `app/presentation/deps.py` — wire new repo + use cases.

### Presentation
- `app/presentation/routes/api_namespaces.py` (new) — the three `/api/bookings/{id}/shares` endpoints + `GET /api/namespaces/shared-with-me`. Register in `main.py`.
- `app/presentation/routes/namespaces.py` (or extend existing bookings route) — HTMX share/revoke actions + the "Shared with me" page.
- Templates: share form partial, shared-with-me page/section.
- `docs/api-reference.md`, `docs/admin-guide.md`.

## Expected behaviour

```jsonc
// owner shares namespace booking abc-123 with alice
POST /api/bookings/abc-123/shares
{ "username": "alice" }
// → 201 { "shared_with": "alice", ... }

// alice can now see the connection details
GET /api/namespaces/shared-with-me
// → [{ "booking_id": "abc-123", "namespace": "dev1", "cluster": "prod",
//      "api_url": "https://api.cluster:6443", "status": "READY",
//      "owner_username": "bob",
//      "environment": { "id": "...", "name": "dev", "status": "READY" } }]

// alice cannot release or extend
DELETE /api/bookings/abc-123  → 403

// owner revokes
DELETE /api/bookings/abc-123/shares/alice → 204

// booking released → share row cascaded away, alice's shared-with-me is now empty
```

## Edge cases / non-goals
- **Not a quota transfer.** The shared namespace still counts against the owner's quota.
- **Not real RBAC.** Recipients cannot release, extend, see audit logs, or share further.
- **Environments: share the namespace, not the whole environment.** The recipient can see the environment's name and status (enough to understand what stack the namespace belongs to) but cannot see the VMs' IPs/passwords, release the environment, or manage any sibling child. Full environment sharing (including VM credentials) is out of scope.
- **Admin already sees everything** — a share with an admin is valid but redundant.

## Tests
- `ShareNamespaceUseCase`: valid share → created; self-share → 400; unknown user → 400; non-NAMESPACE booking → 400; released booking → 400; duplicate → 409; non-owner → 403.
- `RevokeNamespaceShareUseCase`: owner revokes → 204; non-owner → 403; non-existent share → 404.
- `list_shared_with_user`: returns only live, NAMESPACE bookings shared with the user; includes environment info when present; excludes released bookings (cascaded).
- API: full happy path + each error code.
- Browser: share form renders for owner; not for other users; shared-with-me section visible only to recipient.
