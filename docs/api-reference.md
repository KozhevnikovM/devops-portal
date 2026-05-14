# API Reference

## Bookings

### `GET /`

Returns the main HTML page with the booking form and active bookings table.

---

### `POST /bookings`

Create a new VM booking.

**Content-Type:** `application/x-www-form-urlencoded`

**Form fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `image_id` | UUID | Yes | VM image to deploy |
| `hw_config_id` | UUID | Yes | Hardware configuration |
| `ttl_hours` | integer | Yes | Booking duration (1, 4, 8, or 24) |

**Response:**

- With `Accept: application/json` → `201` JSON body:

```json
{
  "id": "uuid",
  "status": "PENDING",
  "ttl_hours": 4,
  "expires_at": "2026-05-14T12:00:00+00:00",
  "created_at": "2026-05-14T08:00:00+00:00",
  "image_id": "uuid",
  "image_name": "Ubuntu 22.04",
  "hw_config_id": "uuid",
  "hw_config_name": "medium"
}
```

- Without `Accept: application/json` → `201` HTMX HTML fragment (booking row)

---

### `GET /bookings/{booking_id}/row`

Returns an HTML fragment for a single booking row. Used by HTMX polling.

---

## Admin — VM Images

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
    "disk_mb": 26624,
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
{ "name": "xlarge", "cpus": 8, "memory_mb": 16384, "disk_mb": 102400 }
```

**Response:** `201` — created hardware config object.

---

### `PATCH /api/hardware/{hw_config_id}`

Update an existing hardware configuration.

**Request body** (all fields optional):

```json
{ "cpus": 4, "memory_mb": 8192 }
```

**Response:** `200` — updated hardware config object.

**Errors:** `404` if config not found, `422` if body is empty.

---

### `DELETE /api/hardware/{hw_config_id}`

Deactivate a hardware configuration. It will no longer appear in the booking form.

**Response:** `204` No Content.

**Errors:** `404` if config not found.
