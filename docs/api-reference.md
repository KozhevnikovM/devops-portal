# API Reference

All endpoints require authentication. Browser users are redirected to `/auth/login`
when unauthenticated. API clients must pass `Authorization: Bearer <api_key>` on every request.

The interactive docs at `/docs` (and `/openapi.json`) list only the JSON API endpoints below.
The server-rendered HTML pages and HTMX fragments are intentionally excluded from the schema.

---

## Authentication

### `GET /auth/login`

Renders the HTML login form.

---

### `POST /auth/login`

Authenticate with username and password. Sets a `session_id` cookie on success.

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `username` | string | Username |
| `password` | string | Password |

**Responses:**

- `302` redirect to `/` on success (cookie set)
- `401` rendered login form with error message on failure

---

### `POST /auth/logout`

Invalidates the current session and clears the `session_id` cookie. Redirects to `/auth/login`.

---

## User Management (admin only)

### `GET /admin/users`

Renders the admin user management page. Lists all users and provides a form to create new ones.

**Auth:** admin only. Non-admin users receive `403 Forbidden`.

---

### `POST /admin/users`

Create a new user from the HTML form. Used by the admin UI (HTMX).

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `username` | string | Must be unique |
| `password` | string | Plain text — hashed server-side with bcrypt |
| `role` | string | `"user"` or `"admin"` |

**Responses:**

- `200` — returns updated user table HTML fragment (HTMX swap)
- `200` with `HX-Retarget: #user-create-error` — username already taken; error message injected into the form

---

### `DELETE /admin/users/{user_id}`

Delete a user account. Admin only.

**Guards (409 Conflict):**
- Cannot delete your own account
- Cannot delete the last remaining admin

**Responses:**

- `200` — returns updated user table HTML fragment (HTMX swap)
- `404 Not Found` — user does not exist
- `409 Conflict` — self-deletion or last admin

On success, the user's API keys and quota row are deleted. Existing bookings are
retained with the original `user_id` intact; the owner column will display `—`.

---

### `GET /api/users`

List all users. Password hashes are never returned.

**Response:** `200` JSON array:

```json
[
  { "id": "uuid", "username": "admin", "role": "admin", "is_active": true },
  { "id": "uuid", "username": "jenkins", "role": "user", "is_active": true }
]
```

**Example:**
```bash
curl -s http://localhost:8000/api/users \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `POST /api/users`

Create a new user.

**Request body:**
```json
{ "username": "jenkins", "password": "s3cret", "role": "user" }
```

Valid `role` values: `"admin"`, `"user"`.

**Response:** `201` — created user object (no password hash).

---

### `POST /api/users/{user_id}/api-keys`

Create an API key for a user. The raw key is returned **once** — it cannot be retrieved again.

**Auth:** admin, or the owner of the target user account.

**Request body** (optional):
```json
{ "description": "Jenkins CI" }
```

**Response:** `201`:
```json
{
  "id": "uuid",
  "key": "dp_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "description": "Jenkins CI"
}
```

Store the returned `key` securely — it is the only time it will be shown.

---

### `DELETE /api/users/{user_id}/api-keys/{key_id}`

Revoke an API key. The key is deactivated immediately.

**Auth:** admin, or the owner of the target user account.

**Responses:**

- `204 No Content` — key revoked
- `403 Forbidden` — caller is not the owner or admin
- `404 Not Found` — key does not exist

---

### `PATCH /api/users/{user_id}/quota`

Set resource quota limits for a user. All fields are optional; omitted fields keep their
current value (falling back to the global default if no per-user row exists yet).

**Auth:** admin only.

**Request body** (all fields optional):
```json
{
  "max_cpus": 32,
  "max_memory_gb": 64,
  "max_ssd_gb": 500,
  "max_hdd_gb": 1000
}
```

**Response:** `200`:
```json
{
  "user_id": "uuid",
  "max_cpus": 32,
  "max_memory_gb": 64,
  "max_ssd_gb": 500,
  "max_hdd_gb": 1000
}
```

**Example:**
```bash
curl -s -X PATCH http://localhost:8000/api/users/<user-id>/quota \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"max_cpus": 32}' | python3 -m json.tool
```

---

### `GET /admin/users/table`

Returns the user table partial (no editing state). Used internally by the Cancel button in
the quota inline edit form.

**Auth:** admin only (browser session). **Response:** `200 text/html`.

---

### `GET /admin/users/{user_id}/quota/edit`

Returns the user table partial with the quota inline edit form open for the specified user.

**Auth:** admin only (browser session). **Response:** `200 text/html`.

---

### `PATCH /admin/users/{user_id}/quota`

HTML form handler for the quota inline editor. Accepts form-encoded fields and returns the
updated user table partial.

**Auth:** admin only (browser session).

**Form fields** (all required when submitted from the UI):

| Field | Type | Description |
|-------|------|-------------|
| `max_cpus` | integer ≥ 1 | Maximum CPU cores |
| `max_memory_gb` | integer ≥ 1 | Maximum RAM in GB |
| `max_ssd_gb` | integer ≥ 1 | Maximum SSD storage in GB |
| `max_hdd_gb` | integer ≥ 1 | Maximum HDD storage in GB |

**Response:** `200 text/html` — updated `#user-table` partial.

---

## User Profile

### `GET /profile`

Renders the user profile page with a timezone selector and a **Booking defaults**
section for choosing a preferred VM image and hardware config.

**Auth:** any authenticated user.

---

### `POST /profile`

Save the user's preferred timezone. Redirects to `/profile?saved=1` on success.

**Auth:** any authenticated user.

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `timezone` | string | IANA timezone name (e.g. `Europe/London`, `America/New_York`) |

**Responses:**

- `302` redirect to `/profile?saved=1` on success
- `400` rendered profile form with error if the timezone value is not a valid IANA name

All booking expiry timestamps in the UI are displayed in the user's chosen timezone.
The stored value and all API responses remain UTC.

---

### `PATCH /profile/defaults`

Save the user's preferred default VM image and hardware config. The booking form
pre-selects these values so repeat bookings require fewer clicks. Returns the
re-rendered **Booking defaults** section as an HTMX fragment.

**Auth:** any authenticated user.

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `default_image_id` | string (UUID) | Preferred VM image id. Empty string clears the preference. |
| `default_hw_config_id` | string (UUID) | Preferred hardware config id. Empty string clears the preference. |

**Responses:**

- `200` rendered Booking defaults fragment on success
- `400` if either id does not match a currently active image / hardware config

A `NULL`/cleared preference means no default — the booking form falls back to the
first active option.

---

## Bookings

### `GET /`

Returns the main HTML page with the booking form and active bookings table.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `filter` | `mine` \| `all` | `mine` | `mine` shows only the current user's bookings; `all` shows everyone's |
| `show_released` | bool | `false` | When omitted/false, RELEASED bookings are hidden. Pass `show_released=1` to include them. |

The two filters are independent and compose, e.g. `/?filter=all&show_released=1`.

---

> **Bookings: API vs browser.** The programmatic booking API lives under **`/api/bookings`** and
> speaks JSON (request bodies and responses). The browser UI uses separate root HTMX routes
> (`POST /bookings`, `DELETE /bookings/{id}`, `PUT /bookings/{id}/extend`, `GET /bookings/{id}/row`)
> that return HTML fragments; those are not part of this API and are omitted from `/docs`. API
> clients should always use `/api/bookings`.

### `GET /api/bookings`

List bookings.

**Auth:** any authenticated user. **Owner-scoped:** a regular user sees **only their own**
bookings; an **admin** sees **all** bookings.

**Response:** `200` JSON array:

```json
[
  {
    "id": "uuid",
    "user_id": "uuid",
    "status": "READY",
    "ttl_minutes": 240,
    "expires_at": "2026-05-15T14:00:00+00:00",
    "created_at": "2026-05-15T10:00:00+00:00",
    "image_id": "uuid",
    "image_name": "Ubuntu 22.04",
    "hw_config_id": "uuid",
    "hw_config_name": "medium",
    "vm_ip": "10.0.0.1",
    "namespace": null,
    "cluster": null,
    "api_url": null,
    "static_vm": null,
    "host": null,
    "username": null
  }
]
```

Each row carries the fields for every resource type; the ones that don't apply are `null`.
`namespace`/`cluster`/`api_url` are populated for namespace bookings;
`static_vm`/`host`/`username` for static-VM bookings. `QUEUED` bookings have no resource fields set.

> **No secrets in the list.** `vm_password` (and static-VM credentials) are **not** included in
> `GET /api/bookings`. The VM password is returned only on the owner-scoped creation response
> (`POST /api/bookings`) and the owner/admin-gated single-row view (`GET /bookings/{id}/row`).

**Example:**
```bash
curl -s http://localhost:8000/api/bookings \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `POST /api/bookings`

Create a new booking. A booking is one of:
- **VM** (`VM`) — provisioned via Terraform.
- **Static VM** (`STATIC_VM`) — reserved from the pre-existing pool.
- **Namespace** (`NAMESPACE`) — reserved from the pre-created pool.

`STATIC_VM` and `NAMESPACE` are **pooled**: omit the specific id to take the next free one
("Any available"), or pass it to reserve a specific one. When the pool is empty, an
"Any available" request is **queued** (see the queued response below) rather than rejected.

**Auth:** any authenticated user. The booking is created under the caller's identity.

**Content-Type:** `application/json`

**JSON body fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `resource_type` | string | No | `VM` (default), `STATIC_VM`, or `NAMESPACE` |
| `ttl_minutes` | integer | Yes | Booking duration in minutes; `0` = no expiry |
| `image_id` | UUID | VM | VM image to deploy (by id) |
| `image_name` | string | VM | VM image by name (alternative to `image_id`) |
| `hw_config_id` | UUID | VM | Hardware configuration (by id) |
| `hw_config_name` | string | VM | Hardware configuration by name (alternative to `hw_config_id`) |
| `startup_script` | string | No | Bash script run on the VM over SSH after provisioning (VM only); see below |
| `roles` | string[] | No | Ansible role **names** applied to the VM after the startup script (VM only); see below |
| `namespace_id` | UUID | No | A specific namespace by id; omit for "Any available" |
| `namespace_name` | string | No | A specific namespace by name (with `cluster_name`); see below |
| `cluster_name` | string | No | The cluster the named namespace lives on |
| `static_vm_id` | UUID | No | A specific static VM by id; omit for "Any available" |
| `static_vm_name` | string | No | A specific static VM by name (alternative to `static_vm_id`) |
| `on_behalf_of` | string | No | **Dispatcher/admin only.** Order for this user (username); the booking is owned by them. See *Ordering on behalf of a user* below. |

> **Ordering by name instead of id.** VM image, hardware-config, and static-VM names are unique, so
> you can order by name and skip the id lookup: a **VM** needs an image **and** a hardware config,
> each given by id **or** name; a **static VM** can be picked by `static_vm_name`. The explicit
> `*_id` wins if both are given. A name that matches no active catalog entry → `400`; for a VM,
> missing both id and name for the image or the hardware config → `400`. Discover valid names with
> `GET /api/images`, `GET /api/hardware`, and `GET /api/static-vms`.

> **Ordering a namespace by (name, cluster).** A namespace name is unique **per cluster**, so the
> `(namespace_name, cluster_name)` pair identifies one. Pass **both** to order it without looking up
> its id. `namespace_id` takes precedence if also given; omitting all three takes "Any available".
> Supplying only one of the pair → `400`; an unknown/inactive/already-booked pair → `409`.

> **Startup script (VM).** Provide a `startup_script` to run a bash script on the VM after it's
> provisioned. In state `CONFIGURING` the worker waits for the VM to become reachable (retrying SSH)
> and runs the script via `bash -s`. Outcomes: an **unreachable** VM → `FAILED`; a **reachable VM
> whose script fails** → `READY` but flagged `config_failed` (the VM is usable; see the booking's
> audit log); a clean run → `READY`. **The script must be idempotent** — a provisioning retry
> re-runs it. It executes on your own VM. Requires the VM template to allow SSH for the configured
> `VM_SSH_USER`; see the admin guide.

> **Roles (VM).** Provide `roles` (a list of catalog **names**, discover via `GET /api/roles`) to
> apply Ansible roles to the VM in the `CONFIGURING` step, **after** the startup script. The selected
> roles are snapshotted onto the booking at order time (later catalog edits don't affect a running
> VM). An unknown/inactive role name → `400`. A role run that fails (VM reachable) → `READY` flagged
> `config_failed`, like a failed script; an unreachable VM → `FAILED`. Roles must be idempotent.
> The applied role names appear in the `roles` field of `GET /api/bookings`.

> **Ordering on behalf of a user (dispatcher).** A caller whose role is `dispatcher` or `admin` may
> add **`on_behalf_of`** (a target **username**) to order a resource *for that user*: the booking's
> owner (`user_id`) becomes the target and it counts against **their** quota, while the acting
> dispatcher is recorded in **`created_by`**. The create response (including one-time credentials) is
> returned to the dispatcher so a pipeline can hand them to the user. The target must be an
> **existing, active** user → otherwise `400 no such active user '<name>'`; a non-dispatcher supplying
> `on_behalf_of` → `403`. Omitting it orders for yourself (`created_by` is `null`). Applies to both
> `POST /api/bookings` and `POST /api/environments`. (A dispatcher token is created like any API key,
> on a user whose role is `dispatcher` — see the admin guide.)
>
> **Visibility & management.** A dispatcher keeps sight of what it dispatched: `GET /api/bookings`
> and `GET /api/environments` return its **own** resources **plus** any it created for others
> (`created_by` = the dispatcher), and it may **release / extend / read the audit of** those same
> resources. The **owner** retains full control of their resource regardless of who created it, and
> an **admin** can manage everything. An unrelated user can neither see nor manage a resource → `403`.

**Permission rules (bookings & environments).** "Creating dispatcher" = the dispatcher whose id is in
the resource's `created_by`.

| Action | Owner | Creating dispatcher | Admin | Anyone else |
|--------|:-----:|:-------------------:|:-----:|:-----------:|
| List (resource appears in `GET`) | ✅ | ✅ | ✅ (all) | ❌ |
| Read / audit (`GET …/{id}`) | ✅ | ✅ | ✅ | ❌ `403` |
| Release (`DELETE …/{id}`) | ✅ | ✅ | ✅ | ❌ `403` |
| Extend (`PUT …/{id}/extend`) | ✅ | ✅ | ✅ | ❌ `403` |

**VM response:** `201`

```json
{
  "id": "uuid",
  "status": "PENDING",
  "resource_type": "VM",
  "ttl_minutes": 240,
  "expires_at": "2026-05-14T12:00:00+00:00",
  "created_at": "2026-05-14T08:00:00+00:00",
  "image_id": "uuid",
  "image_name": "Ubuntu 22.04",
  "hw_config_id": "uuid",
  "hw_config_name": "medium"
}
```

- `409 Conflict` — resource quota exceeded:

```json
{ "detail": "Quota exceeded: CPU (18/16 cores), memory (36/32 GB)" }
```

**Namespace response:**

A namespace booking is allocated synchronously and is `READY` immediately (no provisioning,
no credentials issued). `201`:

```json
{
  "id": "uuid",
  "status": "READY",
  "resource_type": "NAMESPACE",
  "ttl_minutes": 240,
  "expires_at": "2026-05-14T12:00:00+00:00",
  "created_at": "2026-05-14T08:00:00+00:00",
  "namespace": "team-a-dev",
  "cluster": "prod-cluster",
  "api_url": "https://api.cluster:6443"
}
```

- `409 Conflict` — the chosen namespace is inactive or already booked (e.g. lost a race).
- Releasing a namespace booking (or its TTL expiring) returns it to the pool.

**Static VM response:**

Reserved synchronously and `READY` immediately, returning the VM's host + credentials
(password and/or SSH key — whichever the admin registered). `201`:

```json
{
  "id": "uuid",
  "status": "READY",
  "resource_type": "STATIC_VM",
  "ttl_minutes": 240,
  "expires_at": "2026-06-03T12:00:00+00:00",
  "created_at": "2026-06-03T08:00:00+00:00",
  "static_vm": "build-agent-1",
  "host": "10.0.0.12",
  "username": "ubuntu",
  "password": "s3cret",
  "ssh_key": null,
  "queue_position": null
}
```

- `409 Conflict` — a **specific** static VM that's inactive or already booked. (Picking
  "Any available" never 409s — it queues instead.)

**Queued response (pooled, pool empty):**

When no resource of the requested pooled type is free, an "Any available" request is created
as `QUEUED` with no resource assigned and a FIFO `queue_position`. Its TTL starts only when it
is promoted to `READY` (the moment one frees). Poll `GET /api/bookings` (JSON) — or
`GET /bookings/{id}/row` in the browser — to observe the promotion. Example `201`:

```json
{
  "id": "uuid",
  "status": "QUEUED",
  "resource_type": "STATIC_VM",
  "ttl_minutes": 240,
  "expires_at": "2026-06-03T08:00:00+00:00",
  "created_at": "2026-06-03T08:00:00+00:00",
  "static_vm": null,
  "host": null,
  "username": null,
  "password": null,
  "ssh_key": null,
  "queue_position": 1
}
```

Cancel a queued booking with `DELETE /api/bookings/{id}` (it holds no resource, so it just leaves
the queue).

**Example (Jenkins/CI):**
```bash
# VM by names (discover them with GET /api/images and /api/hardware) — no id lookup needed
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "VM", "ttl_minutes": 240, "image_name": "Ubuntu 22.04", "hw_config_name": "medium"}' | python3 -m json.tool

# VM by ids
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "VM", "ttl_minutes": 240, "image_id": "<image-uuid>", "hw_config_id": "<hw-config-uuid>"}' | python3 -m json.tool

# Namespace by (name, cluster) pair — no id lookup needed
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "NAMESPACE", "ttl_minutes": 240, "namespace_name": "team-a-dev", "cluster_name": "prod-cluster"}' | python3 -m json.tool

# Namespace by id (or omit namespace_id for "Any available")
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "NAMESPACE", "ttl_minutes": 240, "namespace_id": "<namespace-uuid>"}' | python3 -m json.tool

# Static VM — "Any available" (queues if the pool is empty)
curl -s -X POST http://localhost:8000/api/bookings \
     -H "Content-Type: application/json" -H "Authorization: Bearer dp_<api_key>" \
     -d '{"resource_type": "STATIC_VM", "ttl_minutes": 240}' | python3 -m json.tool
```

---

### `PUT /api/bookings/{booking_id}/extend`

Extend the TTL of a `READY` booking. Only the booking owner may extend; admins have no override here.
Permanent bookings (`ttl_minutes == 0`) cannot be extended.

**Auth:** booking owner only.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `booking_id` | UUID | ID of the booking to extend |

**Content-Type:** `application/json`

**JSON body fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `extend_minutes` | integer | Yes | Minutes to add to the current TTL (must be > 0) |

**Responses:**

- `200 OK` — TTL extended. Returns the updated booking JSON.
- `403 Forbidden` — caller is not the booking owner.
- `404 Not Found` — booking does not exist.
- `409 Conflict` — booking is not `READY`, or booking is permanent (`ttl_minutes == 0`).

**JSON response body (200):**
```json
{
  "id": "uuid",
  "status": "READY",
  "ttl_minutes": 480,
  "expires_at": "2026-05-15T20:00:00+00:00"
}
```

**Example:**
```bash
curl -s -X PUT http://localhost:8000/api/bookings/<booking-id>/extend \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d '{"extend_minutes": 60}' | python3 -m json.tool
```

---

### `DELETE /api/bookings/{booking_id}`

Release a VM booking. Only the booking owner or an admin may release.

Transitions the booking to `RELEASING` and queues `teardown_vm_task` which runs
`terraform destroy`. The booking reaches `RELEASED` once teardown completes. (Pooled
namespace/static-VM bookings go straight to `RELEASED` and return to the pool.)

**Auth:** booking owner or admin.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `booking_id` | UUID | ID of the booking to release |

**Responses:**

- `202 Accepted` — teardown queued; booking status is now `RELEASING`. Returns the booking JSON.
- `403 Forbidden` — caller is not the booking owner or admin.
- `404 Not Found` — booking does not exist.
- `409 Conflict` — booking is in-flight (`PENDING`, `PROVISIONING`, `RETRY`, or already `RELEASING`) or already `RELEASED`.

**Releasable statuses:** `READY`, `FAILED`

**JSON response body (202):**
```json
{ "id": "uuid", "status": "RELEASING" }
```

**Example:**
```bash
curl -s -X DELETE http://localhost:8000/api/bookings/<booking-id> \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `GET /api/bookings/{booking_id}/audit`

Returns the full audit trail for a booking in chronological order.

**Auth:** booking owner or admin.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `booking_id` | UUID | ID of the booking |

**Responses:**

- `200 OK` — JSON array of audit entries.
- `403 Forbidden` — caller is not the booking owner or admin.
- `404 Not Found` — booking does not exist.

**Response body:**
```json
[
  {
    "id": "uuid",
    "booking_id": "uuid",
    "action": "CREATED",
    "old_status": null,
    "new_status": null,
    "actor_id": "uuid",
    "metadata": null,
    "created_at": "2026-05-15T10:00:00+00:00"
  },
  {
    "id": "uuid",
    "booking_id": "uuid",
    "action": "STATUS_CHANGED",
    "old_status": "PROVISIONING",
    "new_status": "READY",
    "actor_id": "system",
    "metadata": {"vm_ip": "10.0.0.1"},
    "created_at": "2026-05-15T10:01:30+00:00"
  }
]
```

**Example:**
```bash
curl -s http://localhost:8000/api/bookings/<booking-id>/audit \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `GET /bookings/{booking_id}/row`

Returns an HTML fragment for a single booking row. Used by **HTMX polling in the browser** — this
is a presentation route, not part of the JSON API (and is omitted from `/docs`).

**Auth:** the booking **owner** or an **admin**. A non-owner gets `403`; an unknown id gets `404`.

---

### `GET /bookings/{booking_id}/audit`

Renders the **HTML audit-log page** for a booking (timeline of status transitions, actors, and
metadata). Linked from the **Audit log** item in a FAILED booking's ⋮ menu. This is a browser
presentation route (omitted from `/docs`); the machine-readable trail is `GET /api/bookings/{id}/audit`.

**Auth:** the booking **owner** or an **admin**. A non-owner gets `403`; an unknown id gets `404`.

---

## Admin — Catalog UI

### `GET /admin/catalog`

Renders the catalog management page with three panels: VM Images, Hardware Configs, and
Kubernetes Namespaces.

**Auth:** admin only.

---

### `POST /admin/catalog/images`

Create a new VM image from the HTML form.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique display name |
| `vapp_template_id` | string | VCD vApp template URN |

**Responses:** `200` updated image table fragment; `200` with `HX-Retarget: #image-create-error` on duplicate name.

---

### `GET /admin/catalog/images/{image_id}/edit`

Returns the image table with the specified row in inline edit mode.

---

### `PATCH /admin/catalog/images/{image_id}`

Update a VM image from the inline edit form.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | New name |
| `vapp_template_id` | string | New vApp template URN |

**Responses:** `200` updated image table; `404` if image not found.

---

### `DELETE /admin/catalog/images/{image_id}`

Deactivate a VM image. It will no longer appear in the booking form.

**Responses:** `200` updated image table; `404` if image not found.

---

### `POST /admin/catalog/images/{image_id}/activate`

Re-activate a previously deactivated VM image. It will reappear in the booking form.

**Responses:** `200` updated image table; `404` if image not found.

---

### `DELETE /admin/catalog/images/{image_id}/permanent`

Permanently delete a VM image from the database.

**Responses:** `200` updated image table; `404` if not found; `200` with `HX-Retarget: #image-delete-error-{id}` if bookings reference this image.

---

### `POST /admin/catalog/hardware`

Create a new hardware config from the HTML form.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique display name |
| `cpus` | integer | CPU count |
| `memory_mb` | integer | RAM in MB |
| `hdd_mb` | integer | HDD in MB |

**Responses:** `200` updated hardware table fragment; `200` with `HX-Retarget: #hw-create-error` on duplicate name.

---

### `GET /admin/catalog/hardware/{hw_config_id}/edit`

Returns the hardware config table with the specified row in inline edit mode.

---

### `PATCH /admin/catalog/hardware/{hw_config_id}`

Update a hardware config from the inline edit form.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | New name |
| `cpus` | integer | New CPU count |
| `memory_mb` | integer | New RAM in MB |
| `hdd_mb` | integer | New HDD in MB |

**Responses:** `200` updated hardware table; `404` if config not found.

---

### `DELETE /admin/catalog/hardware/{hw_config_id}`

Deactivate a hardware config. It will no longer appear in the booking form.

**Responses:** `200` updated hardware table; `404` if config not found.

---

### `POST /admin/catalog/hardware/{hw_config_id}/activate`

Re-activate a previously deactivated hardware config. It will reappear in the booking form.

**Responses:** `200` updated hardware table; `404` if config not found.

---

### `DELETE /admin/catalog/hardware/{hw_config_id}/permanent`

Permanently delete a hardware config from the database.

**Responses:** `200` updated hardware table; `404` if not found; `200` with `HX-Retarget: #hw-delete-error-{id}` if bookings reference this config.

---

### `POST /admin/catalog/namespaces`

Register a pre-created Kubernetes namespace in the bookable pool.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique namespace name (RFC-1123 label) |
| `cluster_name` | string | Cluster the namespace lives on |
| `api_url` | string | Optional cluster API server URL (display only) |

**Responses:** `200` updated namespace table fragment; `200` with `HX-Retarget: #namespace-create-error` on duplicate name.

---

### `GET /admin/catalog/namespaces/{namespace_id}/edit`

Returns the namespace table with the specified row in inline edit mode.

---

### `PATCH /admin/catalog/namespaces/{namespace_id}`

Update a namespace (`name`, `cluster_name`, `api_url`) from the inline edit form.

**Responses:** `200` updated namespace table; `404` if not found; `200` with `HX-Retarget: #namespace-create-error` on duplicate name.

---

### `DELETE /admin/catalog/namespaces/{namespace_id}`

Deactivate a namespace. It will no longer be offered for new bookings; any existing booking
that holds it is unaffected.

**Responses:** `200` updated namespace table; `404` if not found.

---

### `POST /admin/catalog/namespaces/{namespace_id}/activate`

Re-activate a previously deactivated namespace.

**Responses:** `200` updated namespace table; `404` if not found.

---

### `DELETE /admin/catalog/namespaces/{namespace_id}/permanent`

Permanently delete a namespace from the catalog.

**Responses:** `200` updated namespace table; `404` if not found; `200` with `HX-Retarget: #namespace-delete-error-{id}` if bookings reference this namespace.

---

### `POST /admin/catalog/static-vms`

Register a pre-existing VM (created outside the portal) in the bookable static pool.

**Content-Type:** `application/x-www-form-urlencoded`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique label |
| `host` | string | IP or hostname handed to the reserver |
| `username` | string | Login handed to the reserver |
| `password` | string | Password credential (optional) |
| `ssh_key` | string | SSH key credential (optional) |
| `cpus` | integer | Optional, display only |
| `memory_gb` | integer | Optional; entered in GB, stored as MB |

At least one of `password` / `ssh_key` is required (DB `CHECK` + inline validation).

**Responses:** `200` updated static-VM table fragment; `200` with `HX-Retarget: #static-vm-create-error` on duplicate name or when neither credential is provided.

---

### `GET /admin/catalog/static-vms/{static_vm_id}/edit`

Returns the static-VM table with the specified row in inline edit mode.

---

### `PATCH /admin/catalog/static-vms/{static_vm_id}`

Update a static VM (`name`, `host`, `username`, `password`, `ssh_key`, `cpus`, `memory_gb`) from the inline edit form.

**Responses:** `200` updated static-VM table; `404` if not found; `200` with `HX-Retarget: #static-vm-create-error` on duplicate name / missing credential.

---

### `DELETE /admin/catalog/static-vms/{static_vm_id}`

Deactivate a static VM. It will no longer be offered for new reservations; any existing booking that holds it is unaffected.

**Responses:** `200` updated static-VM table; `404` if not found.

---

### `POST /admin/catalog/static-vms/{static_vm_id}/activate`

Re-activate a previously deactivated static VM.

**Responses:** `200` updated static-VM table; `404` if not found.

---

### `DELETE /admin/catalog/static-vms/{static_vm_id}/permanent`

Permanently delete a static VM from the catalog.

**Responses:** `200` updated static-VM table; `404` if not found; `200` with `HX-Retarget: #static-vm-delete-error-{id}` if bookings reference this static VM.

---

## VM Images & Hardware (JSON API)

**Listing** (`GET /api/images`, `GET /api/hardware`, `GET /api/static-vms`) is available to **any
authenticated user** — read-only catalog discovery so you can order by name. **Creating, updating,
and deleting** catalog entries still require **admin** role.

### `GET /api/images`

List all VM images (active and inactive). **Auth:** any authenticated user.

**Response:** `200` array of image objects:

```json
[
  {
    "id": "uuid",
    "name": "Ubuntu 22.04",
    "vapp_template_id": "urn:vcloud:vapptemplate:...",
    "is_active": true,
    "created_at": "2026-05-14T00:00:00+00:00"
  }
]
```

---

### `POST /api/images`

Create a new VM image.

**Request body:**
```json
{ "name": "Debian 12", "vapp_template_id": "urn:vcloud:vapptemplate:..." }
```

**Response:** `201` — created image object.

**Errors:** `422` if fields are missing.

---

### `PATCH /api/images/{image_id}`

Update an existing VM image (e.g. to set the real `vapp_template_id` after migration).

**Request body** (all fields optional):
```json
{ "vapp_template_id": "urn:vcloud:vapptemplate:real-id" }
```

**Response:** `200` — updated image object.

**Errors:** `404` if image not found, `422` if body is empty.

---

### `DELETE /api/images/{image_id}`

Deactivate a VM image. It will no longer appear in the booking form.
Existing bookings referencing this image are unaffected.

**Response:** `204` No Content.

**Errors:** `404` if image not found.

---

## Admin — Hardware Configs

### `GET /api/hardware`

List all hardware configurations (active and inactive). **Auth:** any authenticated user.

**Response:** `200` array of hardware config objects:

```json
[
  {
    "id": "uuid",
    "name": "medium",
    "cpus": 2,
    "memory_mb": 4096,
    "disk_mb": 26624,
    "drive_type": "HDD",
    "is_active": true,
    "created_at": "2026-05-14T00:00:00+00:00"
  }
]
```

`drive_type` is `"SSD"` or `"HDD"` (default `"HDD"`). A config's disk counts toward the matching
drive-type quota (`max_ssd_gb` / `max_hdd_gb`).

---

### `POST /api/environments`

Order an environment blueprint — creates one parent **Environment** plus its child bookings
(VMs provision, namespaces/static VMs reserve), all under one shared TTL. **Auth:** any
authenticated user.

**Request body:** `{ "blueprint_name": "dev-stack", "ttl_minutes": 240 }`. A **dispatcher/admin** may
add **`on_behalf_of`** (username) to order the environment for another user — see *Ordering on behalf
of a user* under `POST /api/bookings`.

Blueprint item names are resolved up front, so a bad name creates nothing. A child quota failure
rolls the whole environment back. **Responses:** `201` (the environment + its children); `404`
unknown blueprint; `400` a blueprint item references an unknown catalog entry; `409` quota exceeded
or a specific pooled resource unavailable.

```json
{
  "id": "uuid", "name": "dev-stack", "blueprint_name": "dev-stack",
  "status": "PROVISIONING", "ttl_minutes": 240,
  "expires_at": "…", "created_at": "…",
  "bookings": [
    { "id": "uuid", "label": "ns", "resource_type": "NAMESPACE", "status": "READY", "namespace": "team-a-dev", "roles": [] },
    { "id": "uuid", "label": "web", "resource_type": "VM", "status": "PROVISIONING", "image_name": "Ubuntu 22.04",
      "hw_config_name": "medium", "roles": ["docker-machine"], "config_failed": false }
  ]
}
```

Each child's `label` is the blueprint item's label (e.g. `web`); it is `null` for an item with no
label. The environment `status` is **derived** from its children: any `FAILED` child → `FAILED`; any
in-flight child → `PROVISIONING`; all `READY` → `READY`; all `RELEASED` → `RELEASED`. Child bookings
also appear in `GET /api/bookings`, carrying their `environment_id`.

### `GET /api/environments` and `GET /api/environments/{id}`

List environments (owner-scoped; admins see all) / fetch one (owner or admin; `403`/`404` otherwise),
each with the derived status + child summaries.

### `GET /api/environments/by-namespace/{namespace_name}`

Locate the **live environment whose namespace child is named `namespace_name`** — so a pipeline can
find the stack it owns by namespace instead of environment id. Optional query param **`cluster`**
disambiguates a name reused across clusters (namespace names are unique only *per cluster*).

**Auth & responses:**

| Outcome | Status |
|---------|--------|
| Caller owns / dispatched the environment, or is admin | `200` — same body as `GET …/{id}` |
| It belongs to another user | `409` `namespace '<name>' is in use by another user's environment` (the owner is **not** disclosed) |
| No active environment holds that namespace (unknown, free, or a standalone non-environment namespace booking) | `404` |
| The name is held on **multiple clusters** and no `cluster` given | `400` — specify `?cluster=` |

This is a **read-only lookup** — it does not reserve, lock, or claim the environment.

```bash
curl -s "http://localhost:8000/api/environments/by-namespace/dev1?cluster=prod-cluster" \
     -H "Authorization: Bearer dp_<key>"
```

### `GET /api/environments/by-namespace/{namespace_name}/allowed-to-user`

Check whether the live environment holding a namespace belongs to a **named user** — a one-call
yes/no, e.g. a dispatcher verifying "can `john` use the environment on namespace `dev1`?". Required
query param **`user`** (username); optional **`cluster`** disambiguates a name across clusters.

| Outcome | Status |
|---------|--------|
| The namespace's environment is owned by `user` | `202` — body `{ "namespace": "...", "user": "...", "match": true }` |
| Owned by someone else, **or** no active environment holds the namespace | `423 Locked` (the real owner is **not** disclosed) |
| `user` omitted | `422` |
| Name held on **multiple clusters** with no `cluster` | `400` — specify `?cluster=` |

**Auth:** any authenticated user. Read-only equality check — it reveals only `true`/`false` for the
(namespace, user) pair you name and never vends the environment or its secrets.

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/api/environments/by-namespace/dev1/allowed-to-user?user=john" \
  -H "Authorization: Bearer <key>"
# 202 → dev1's environment is john's   |   423 → it isn't (or dev1 isn't in any environment)
```

### `DELETE /api/environments/{id}`

Release a whole environment — tears down **all** its child resources together (provisioned VMs →
`RELEASING` + teardown, pooled → back to the pool, queued → cancelled), including in-flight children.
**Auth:** owner or admin (`403`/`404` otherwise). **Response:** `202` with the environment (children
now `RELEASING`/`RELEASED`); idempotent if already released. The environment's TTL expiring triggers
the same grouped teardown automatically.

> **Browser UI:** the **Environments** page (`GET /environments`, in the top nav) lets users order a
> blueprint, watch the stack come up (HTMX polling), and release it — the same operations as the JSON
> API above. Those `/environments*` routes return HTML fragments and are intentionally absent from
> the schema.

---

### `GET /api/environment-blueprints`

List environment blueprints — admin-defined templates bundling several resources into one stack.
**Auth:** any authenticated user (read-only discovery); create/update/delete require **admin**.

**Response:** `200` array (each blueprint includes its ordered `items`):

```json
[
  {
    "id": "uuid",
    "name": "dev-stack",
    "description": "namespace + web + db",
    "is_active": true,
    "created_at": "2026-06-08T00:00:00+00:00",
    "items": [
      { "id": "uuid", "resource_type": "NAMESPACE", "position": 0, "label": "ns", "spec": {} },
      { "id": "uuid", "resource_type": "VM", "position": 1, "label": "web",
        "spec": { "image_name": "Ubuntu 22.04", "hw_config_name": "medium", "roles": ["docker-machine"] } }
    ]
  }
]
```

Each item's `spec` carries the per-type fields (catalog entries **by name**): VM →
`image_name`/`hw_config_name`/`roles`/`startup_script`; STATIC_VM → `static_vm_name` (null = any);
NAMESPACE → `namespace_name`/`cluster_name` (null = any). Names are **not** resolved at create time
(a blueprint may reference a catalog entry added later) — they're resolved when the blueprint is
*ordered* (a later 0.8.0 item).

**Admin write endpoints:** `POST` / `PATCH /{id}` (replaces the item set) / `DELETE /{id}`
(deactivate). A VM item needs `image_name` + `hw_config_name`; bad `resource_type` → `400`;
duplicate `name` → `409`.

---

### `GET /api/roles`

List Ansible roles in the catalog. **Auth:** any authenticated user (read-only discovery);
creating/updating/deleting roles requires **admin**.

**Response:** `200` array:

```json
[
  {
    "id": "uuid",
    "name": "docker-machine",
    "description": "Install Docker Engine",
    "ansible_role": "docker_machine",
    "default_vars": { "version": "latest" },
    "is_active": true,
    "created_at": "2026-06-08T00:00:00+00:00"
  }
]
```

A role pairs a catalog `name` with an Ansible role directory (`ansible_role`, under
`ansible/roles/`) and admin-set `default_vars`. Roles will be applied to a VM during configuration
(a later 0.8.0 item lets you order a VM with `roles: [...]`).

**Admin write endpoints:** `POST /api/roles` (201), `PATCH /api/roles/{id}`,
`DELETE /api/roles/{id}` (deactivate). `default_vars` must be a JSON object; duplicate `name` → `409`.

---

### `GET /api/static-vms`

List active static VMs so their names are discoverable for ordering (`static_vm_name` on
`POST /api/bookings`). **Auth:** any authenticated user. **Credentials (`password`, `ssh_key`) are
never returned here** — they're vended only on the owner's booking-creation response.

**Response:** `200` array:

```json
[
  {
    "id": "uuid",
    "name": "build-agent-1",
    "host": "10.0.0.12",
    "cpus": 4,
    "memory_mb": 8192,
    "is_active": true,
    "available": true
  }
]
```

`available` is `false` while a live booking currently holds the static VM.

**Example:**
```bash
curl -s http://localhost:8000/api/static-vms \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `POST /api/hardware`

Create a new hardware configuration.

**Request body** (`drive_type` optional, defaults to `"HDD"`):
```json
{ "name": "xlarge", "cpus": 8, "memory_mb": 16384, "disk_mb": 102400, "drive_type": "SSD" }
```

**Response:** `201` — created hardware config object.

---

### `PATCH /api/hardware/{hw_config_id}`

Update an existing hardware configuration.

**Request body** (all fields optional):
```json
{ "cpus": 4, "memory_mb": 8192, "disk_mb": 51200, "drive_type": "SSD" }
```

**Response:** `200` — updated hardware config object.

**Errors:** `404` if config not found, `422` if body is empty.

---

### `DELETE /api/hardware/{hw_config_id}`

Deactivate a hardware configuration. It will no longer appear in the booking form.

**Response:** `204` No Content.

**Errors:** `404` if config not found.
