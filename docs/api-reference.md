# API Reference

All endpoints require authentication. Browser users are redirected to `/auth/login`
when unauthenticated. API clients must pass `Authorization: Bearer <api_key>` on every request.

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

Renders the user profile page with a timezone selector.

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

## Bookings

### `GET /`

Returns the main HTML page with the booking form and active bookings table.

---

### `GET /bookings`

List all bookings.

**Auth:** any authenticated user.

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
    "vm_password": "Abc123XyZ456qwER"
  }
]
```

`vm_password` is set when the booking reaches `READY`. It contains a 16-character alphanumeric
password generated at provisioning time.

**Example:**
```bash
curl -s http://localhost:8000/bookings \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `POST /bookings`

Create a new VM booking.

**Auth:** any authenticated user. The booking is created under the caller's identity.

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `image_id` | UUID | Yes | VM image to deploy |
| `hw_config_id` | UUID | Yes | Hardware configuration |
| `ttl_minutes` | integer | Yes | Booking duration in minutes; `0` = no expiry |

**Response:**

- With `Accept: application/json` → `201` JSON body:

```json
{
  "id": "uuid",
  "status": "PENDING",
  "ttl_minutes": 240,
  "expires_at": "2026-05-14T12:00:00+00:00",
  "created_at": "2026-05-14T08:00:00+00:00",
  "image_id": "uuid",
  "image_name": "Ubuntu 22.04",
  "hw_config_id": "uuid",
  "hw_config_name": "medium"
}
```

- Without `Accept: application/json` → `201` HTMX HTML fragment (booking row)
- `409 Conflict` — resource quota exceeded. JSON clients receive:

```json
{ "detail": "Quota exceeded: CPU (18/16 cores), memory (36/32 GB)" }
```

Browser users see an error banner above the booking form. The error lists each violated
dimension with projected usage and the limit. Release an active VM to free resources.

**Example (Jenkins/CI):**
```bash
curl -s -X POST http://localhost:8000/bookings \
     -H "Accept: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d "ttl_minutes=240&image_id=<image-uuid>&hw_config_id=<hw-config-uuid>" | python3 -m json.tool
```

---

### `PUT /bookings/{booking_id}/extend`

Extend the TTL of a `READY` booking. Only the booking owner may extend; admins have no override here.
Permanent bookings (`ttl_minutes == 0`) cannot be extended.

**Auth:** booking owner only.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `booking_id` | UUID | ID of the booking to extend |

**Form fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `extend_minutes` | integer | Yes | Minutes to add to the current TTL (must be > 0) |

**Responses:**

- `200 OK` — TTL extended. Returns updated HTML row fragment (default) or JSON (with `Accept: application/json`).
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
curl -s -X PUT http://localhost:8000/bookings/<booking-id>/extend \
     -H "Accept: application/json" \
     -H "Authorization: Bearer dp_<api_key>" \
     -d "extend_minutes=60" | python3 -m json.tool
```

---

### `DELETE /bookings/{booking_id}`

Release a VM booking. Only the booking owner or an admin may release.

Transitions the booking to `RELEASING` and queues `teardown_vm_task` which runs
`terraform destroy`. The booking reaches `RELEASED` once teardown completes.

**Auth:** booking owner or admin.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `booking_id` | UUID | ID of the booking to release |

**Responses:**

- `202 Accepted` — teardown queued; booking status is now `RELEASING`. Returns HTML row fragment (default) or JSON (with `Accept: application/json`).
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
curl -s -X DELETE http://localhost:8000/bookings/<booking-id> \
     -H "Accept: application/json" \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `GET /bookings/{booking_id}/audit`

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
curl -s http://localhost:8000/bookings/<booking-id>/audit \
     -H "Authorization: Bearer dp_<api_key>" | python3 -m json.tool
```

---

### `GET /bookings/{booking_id}/row`

Returns an HTML fragment for a single booking row. Used by HTMX polling.

**Auth:** any authenticated user.

---

## Admin — Catalog UI

### `GET /admin/catalog`

Renders the catalog management page with two panels: VM Images and Hardware Configs.

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

## Admin — VM Images (JSON API)

All `/api/images` and `/api/hardware` endpoints require **admin** role.

### `GET /api/images`

List all VM images (active and inactive).

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

List all hardware configurations (active and inactive).

**Response:** `200` array of hardware config objects:

```json
[
  {
    "id": "uuid",
    "name": "medium",
    "cpus": 2,
    "memory_mb": 4096,
    "hdd_mb": 26624,
    "is_active": true,
    "created_at": "2026-05-14T00:00:00+00:00"
  }
]
```

---

### `POST /api/hardware`

Create a new hardware configuration.

**Request body:**
```json
{ "name": "xlarge", "cpus": 8, "memory_mb": 16384, "hdd_mb": 102400 }
```

**Response:** `201` — created hardware config object.

---

### `PATCH /api/hardware/{hw_config_id}`

Update an existing hardware configuration.

**Request body** (all fields optional):
```json
{ "cpus": 4, "memory_mb": 8192, "hdd_mb": 51200 }
```

**Response:** `200` — updated hardware config object.

**Errors:** `404` if config not found, `422` if body is empty.

---

### `DELETE /api/hardware/{hw_config_id}`

Deactivate a hardware configuration. It will no longer appear in the booking form.

**Response:** `204` No Content.

**Errors:** `404` if config not found.
