# Feature: view a booking's audit log in the UI (link from a failed booking)

## Goal

When a provisioning job fails, let the owner/admin open a **human-readable audit log** for that
booking from the UI, instead of only the JSON API (`GET /api/bookings/{id}/audit`). A FAILED
booking row gets an **Audit log** link that opens the booking's audit timeline.

## What changes

### 1. Audit log page (HTML)

New presentation route **`GET /bookings/{booking_id}/audit`** in `bookings.py` (HTML, so #186's
filter keeps it out of the OpenAPI schema; the JSON audit lives separately at
`/api/bookings/{id}/audit`). It is **owner/admin-gated** exactly like the row + JSON audit:
`403` for a non-owner, `404` for an unknown id.

It renders a new template `app/presentation/templates/audit_log.html` (extends `base.html`)
showing, newest-or-oldest-first (chronological), each audit entry:
- timestamp (UTC, in the user's timezone like the rest of the UI),
- action (`CREATED`, `STATUS_CHANGED`, `EXTENDED`, …),
- status transition `old_status → new_status` when present,
- actor (`system` or the acting user id),
- metadata (e.g. `vm_ip`, failure detail) rendered compactly.

The page header shows the booking's resource/status and a **← Back to bookings** link.

### 2. "Audit log" link on a failed booking row

In `partials/booking_row.html`, the `⋮` action menu for a **FAILED** booking gains an
**Audit log** link:

```html
<a href="/bookings/{{ booking.id }}/audit" class="block px-3 py-2 text-sm text-gray-300 hover:bg-gray-800">Audit log</a>
```

`/bookings/...` is already covered by the reverse-proxy `="/book"` `sub_filter` rule, so the link
works behind the `/dp` subpath too. Scope is **FAILED** per the request; the same link is trivial
to extend to other statuses later if wanted.

### Files

- `app/presentation/routes/bookings.py` — new `GET /bookings/{booking_id}/audit` HTML route.
- `app/presentation/templates/audit_log.html` — new page.
- `app/presentation/templates/partials/booking_row.html` — Audit log link in the FAILED menu.
- `docs/api-reference.md` (note the browser audit view), `docs/admin-guide.md` (failure
  troubleshooting mentions the link).

No API/JSON change (the existing `/api/bookings/{id}/audit` is untouched), no DB migration.

## Expected behaviour

- A FAILED booking shows an **Audit log** item in its `⋮` menu; clicking it opens
  `/bookings/{id}/audit` with the full timeline and a back link.
- Owner or admin only — a non-owner gets `403`, an unknown booking `404`.
- Works locally, on a subdomain deploy, and behind the `/dp` proxy.

## Tests

- `GET /bookings/{id}/audit` returns `200 text/html` listing the entries for owner and admin;
  `403` for a non-owner; `404` for a missing booking.
- `partials/booking_row.html`: a FAILED row renders the `/bookings/{id}/audit` link; a READY row
  does not.
- `test_openapi_hides_html.py`: `/bookings/{booking_id}/audit` is absent from the schema (HTML
  route), while `/api/bookings/{booking_id}/audit` remains.
