# Bugfix: Quota under-counting — floor division + CONFIGURING invisible (Issue #293)

Two independent quota bugs ship together as one small fix.

## D3 — Floor division on new-booking memory

### Root cause

`create_booking.py:66`:

```python
new_memory_gb = hw.memory_mb // 1024
```

The aggregate side (`quota_repo.py:79`) uses `math.ceil`:

```python
"memory_gb": math.ceil(int(row.memory_mb) / 1024),
```

The two sides are inconsistent. The comment claims "floor matches ceiling at the
boundary" but that is only true at exact multiples of 1024 MB. For non-aligned
configs the discrepancy causes:

| memory_mb | new_memory_gb (floor) | aggregate after booking (ceil) | result |
|-----------|-----------------------|-------------------------------|--------|
| 512       | 0                     | 1                             | booking slips past limit; first one appears to consume 0 GB |
| 1536      | 1                     | 2 on the second booking       | first 1536 booking costs "1 GB" but the aggregate shows 2 GB used |

A user with a 1 GB memory quota can book a 512 MB VM because `512 // 1024 == 0`.
The same mismatch applies to disk (`hw.disk_mb // 1024`).

### Fix

Use `math.ceil` on both sides:

```python
new_memory_gb = math.ceil(hw.memory_mb / 1024)
new_disk_gb   = math.ceil(hw.disk_mb   / 1024)
```

No migration needed. Existing bookings are unaffected — the fix applies to the
new-booking check path only.

**Files**: `app/application/use_cases/create_booking.py`

---

## D4 — CONFIGURING status invisible to the quota counter

### Root cause

`quota_repo.py:13-19` `_ACTIVE_STATUSES` omits `CONFIGURING`:

```python
_ACTIVE_STATUSES = [
    BookingStatus.PENDING.value,
    BookingStatus.PROVISIONING.value,
    BookingStatus.RETRY.value,
    BookingStatus.READY.value,
    BookingStatus.RELEASING.value,
]
```

A VM transitions to `CONFIGURING` after Terraform completes but before Ansible
finishes. The VM already exists in VCD at this point, yet during the entire
configuration window the booking is invisible to `count_active_resources`. A user
can submit additional bookings that pass the quota check, racing past their limit.

### Fix

Add `BookingStatus.CONFIGURING` to `_ACTIVE_STATUSES`:

```python
_ACTIVE_STATUSES = [
    BookingStatus.PENDING.value,
    BookingStatus.PROVISIONING.value,
    BookingStatus.CONFIGURING.value,   # ← add
    BookingStatus.RETRY.value,
    BookingStatus.READY.value,
    BookingStatus.RELEASING.value,
]
```

**Files**: `app/infrastructure/repositories/quota_repo.py`

---

## Expected behaviour after fix

- A 512 MB VM config costs 1 GB of memory quota (not 0).
- A 1536 MB VM config costs 2 GB of memory quota on creation (consistent with aggregate).
- A CONFIGURING booking is visible to the quota counter; racing a second booking
  while one is configuring correctly reflects both in the used total.

## Test (regression)

`tests/test_quota_undercounting.py`:

1. Book a 512 MB config — assert `QuotaExceededError` when user has ≤ 0 GB free after ceiling.
2. Book a 1536 MB config — assert it costs 2 GB (ceil), not 1 GB (floor).
3. Mock quota counter to return a CONFIGURING booking — assert it is counted in the used total.
4. Race scenario: two concurrent bookings with CONFIGURING in between — assert second is rejected
   when quota is tight.
