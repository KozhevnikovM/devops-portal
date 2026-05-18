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

  "max_hdd_gb": 1000
}
```

**Response:** `200`:
```json
{
  "user_id": "uuid",
  "max_cpus": 32,
  "max_memory_gb": 64,

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
    "vm_ip": "10.0.0.1"
  }
]
```

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

## Admin — VM Images

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
    "ssd_mb": 0,
    "hdd_mb": 26624,
    "is_active": true,
    "created_at": "2026-05-14T00:00:00+00:00"
  }
]
```

---

### `POST /api/hardware`

Create a new hardware configuration. Specify `ssd_mb`, `hdd_mb`, or both; unused storage
type can be omitted or set to `0`.

**Request body:**
```json
{ "name": "xlarge", "cpus": 8, "memory_mb": 16384, "ssd_mb": 51200, "hdd_mb": 102400 }
```

**Response:** `201` — created hardware config object.

---

### `PATCH /api/hardware/{hw_config_id}`

Update an existing hardware configuration.

**Request body** (all fields optional):
```json
{ "cpus": 4, "memory_mb": 8192, "ssd_mb": 25600, "hdd_mb": 51200 }
```

**Response:** `200` — updated hardware config object.

**Errors:** `404` if config not found, `422` if body is empty.

---

### `DELETE /api/hardware/{hw_config_id}`

Deactivate a hardware configuration. It will no longer appear in the booking form.

**Response:** `204` No Content.

**Errors:** `404` if config not found.
