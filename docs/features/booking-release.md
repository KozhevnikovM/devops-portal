# Feature: Booking Release (Issue #35)

## Goal

Allow users to manually release a READY (or FAILED) booking via a "Release" button.
Releasing queues a `teardown_vm_task` Celery task that runs `terraform destroy` and
marks the booking RELEASED. TTL-expired bookings will also use this same task when
TTL enforcement (issue #36) is implemented.

---

## New Statuses

Two new values added to `BookingStatus`:

| Status | Type | Description |
|--------|------|-------------|
| `RELEASING` | non-terminal | Teardown in progress |
| `RELEASED` | terminal | VM cleanly destroyed |

No DB migration needed — `status` is already `VARCHAR(32)`.

---

## New Celery Task: `app/tasks/teardown.py`

```python
@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def teardown_vm_task(self, booking_id: str) -> None:
    # 1. Transition booking → RELEASING
    # 2. asyncio.run(terraform.destroy(f"booking-{booking_id}"))
    # 3. Transition booking → RELEASED
    # On failure: transition → FAILED, then retry
```

Uses the same `terraform` instance as `provision.py` (`StubTerraformAdapter` or
`TerraformVcdAdapter` based on `USE_STUB_TERRAFORM`). The stub `destroy()` sleeps 2s.

No token-pool locking needed for destroy — it uses the workspace ID, not a shared token.

---

## New API Endpoint

`DELETE /bookings/{booking_id}`

| Condition | Response |
|-----------|----------|
| Status is READY or FAILED | 202 — transitions to RELEASING, queues teardown task, returns updated row HTML (HTMX) or JSON |
| Status is PENDING / PROVISIONING / RETRY | 409 — cannot release in-flight booking |
| Booking not found | 404 |

Content negotiation: `Accept: application/json` returns JSON body; otherwise returns
the `partials/booking_row.html` fragment (HTMX swap).

---

## UI Changes

**`booking_row.html`:**

- READY row: add "Release" button
  ```html
  <button hx-delete="/bookings/{{ booking.id }}"
          hx-target="#booking-{{ booking.id }}"
          hx-swap="outerHTML"
          hx-confirm="Release this VM?">
    Release
  </button>
  ```
- RELEASING row: pulse indicator, no button (same pattern as PROVISIONING)
- RELEASED row: grey, static, no button (terminal)
- Update `is_terminal` check to include `RELEASED`
- Continue polling (hx-trigger every 3s) while status is RELEASING

---

## Tailwind Additions (`tailwind.input.css`)

```css
.status-RELEASING { @apply bg-orange-900 text-orange-300 border border-orange-700; }
.status-RELEASED  { @apply bg-gray-800  text-gray-400  border border-gray-600; }
```

---

## Files Changed

| File | Change |
|------|--------|
| `app/domain/enums.py` | Add `RELEASING`, `RELEASED` |
| `app/tasks/teardown.py` | New: `teardown_vm_task` |
| `app/presentation/routes/bookings.py` | Add `DELETE /bookings/{booking_id}` |
| `app/presentation/templates/partials/booking_row.html` | Release button, RELEASING pulse, RELEASED state |
| `tailwind.input.css` | RELEASING + RELEASED status styles |
| `docs/api-reference.md` | Document `DELETE /bookings/{booking_id}` |

---

## Edge Cases

- **Double-click / race**: If the booking is already RELEASING when DELETE arrives, return 409 (same as in-flight statuses).
- **Teardown failure**: task retries up to 2× then sets status → FAILED. A FAILED booking can be released again (DELETE is accepted on FAILED).
- **FAILED with no VM**: `terraform destroy` on a workspace that was never successfully applied is a no-op for the stub; the VCD adapter handles a missing workspace gracefully (Terraform returns success if there's nothing to destroy).
