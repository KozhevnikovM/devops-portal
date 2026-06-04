# Feature: drive-type quotas (#147)

**Type: Feature** · Source: CQ#4 (supersedes CQ#2 naming) · Phase 3, item #11

## Goal

Today the SSD quota (`max_ssd_gb`) is dead: `count_active_resources` always reports `ssd_gb = 0`
and every booking's disk counts only toward HDD. This feature makes disk capacity **drive-type
aware**: each hardware config has a disk of a given **drive type** (`SSD` | `HDD`), and a booking's
disk counts toward the **matching** drive-type quota.

**Decision (confirmed):** keep both quota columns (`max_ssd_gb` + `max_hdd_gb`) and enforce per
drive type — *not* drop `max_ssd_gb`.

## What changes

### Domain
- New `DriveType` enum (`SSD`, `HDD`).
- `HWConfig` gains `drive_type` (default `HDD`); the disk field `hdd_mb` is renamed to the generic
  `disk_mb` (it can now hold an SSD size).
- `Booking` gains `drive_type` and renames `hdd_mb` → `disk_mb` — the booking **snapshots** the
  config's drive type and disk size at creation, so quota accounting/history survives later config
  edits.

### Persistence
- `HWConfigModel`: `hdd_mb` → `disk_mb`; add `drive_type` (`String`, default/server_default `HDD`).
- `BookingModel`: `hdd_mb` → `disk_mb`; add `drive_type` (default/server_default `HDD`).
- `QuotaModel` is unchanged — `max_ssd_gb` + `max_hdd_gb` stay, now genuinely enforced.
- **Migrations (new revisions only):**
  - `0015_hw_config_drive_type` — add `drive_type` (default `HDD`), rename `hdd_mb` → `disk_mb`.
  - `0016_booking_drive_type` — add `drive_type` (default `HDD`, backfills existing rows), rename
    `hdd_mb` → `disk_mb`.

### Enforcement
- `count_active_resources` sums disk **per drive type** (`ssd_gb` / `hdd_gb`) instead of hardcoding
  `ssd_gb = 0`.
- `CreateBookingUseCase` compares the new booking's disk against **its config's drive-type quota**
  (`max_ssd_gb` for an SSD config, `max_hdd_gb` for HDD).

### API / UI
- `api.py` `HWConfig*` schemas use `disk_mb` + `drive_type`.
- Admin hardware form (create + inline edit) gains a **Drive type** selector and a generic
  **Disk (GB)** field (form field `disk_gb`).
- The hardware table shows a **Type** column; the booking form/defaults show the config's disk with
  its drive type (e.g. `100 GB SSD`).
- Quota admin UI already shows both SSD and HDD limits — now meaningful for both.

## Expected behaviour

- A booking using an **SSD** config counts toward `max_ssd_gb`; an **HDD** config toward
  `max_hdd_gb`. Exceeding the matching limit is rejected with a per-type message
  (`SSD disk (… / … GB)`).
- Existing hardware configs and bookings backfill to `HDD` (behaviour-preserving for current data).
- Admins can set the drive type when creating/editing a hardware config.

## Tests

- Disk quota is enforced per drive type: an SSD config's disk counts only toward the SSD quota and
  is rejected when it would exceed `max_ssd_gb`; an HDD config likewise.
- `count_active_resources` reports non-zero `ssd_gb` once an SSD booking exists.
- Admin can create a config with `drive_type=SSD`; the value persists and renders.
- Migration-chain test stays green.

## Docs

`admin-guide.md` (drive-type on hardware configs + per-type quota), `api-reference.md` (`disk_mb` +
`drive_type` on the hardware schemas).
