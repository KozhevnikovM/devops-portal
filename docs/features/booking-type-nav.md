# Feature: Booking-Type Navigation (#120)

Part of the v0.5.0 milestone. Builds on #118 (namespace booking flow). Replaces the in-form
VM|Namespace toggle with top-level navigation: each booking type is a dedicated page.

## Goal

A header nav with **VM Booking**, **Namespace Booking**, and a disabled **Environment**
(coming soon). Each type has its own page showing that type's booking form and only that
type's bookings.

## Header (`base.html`)

Always-visible horizontal nav next to the logo (shown when authenticated):

```
▶ DevOps Portal  / VM Booking   [VM Booking] [Namespace Booking] [Environment]        ☰
```

- `VM Booking` → `/book/vm`, `Namespace Booking` → `/book/namespace`.
- `Environment` is a non-clickable greyed `<span>` (`cursor-not-allowed`, `title="Coming soon"`).
- The active page is highlighted (`active_nav` context: `vm` | `namespace`).
- The ☰ dropdown is unchanged (Users/Catalog for admins, Profile, Sign out).

## Routes (`bookings.py`)

A shared `_render_bookings_page(..., booking_type, page_path, active_nav, filter, show_released)`:

- `GET /` **and** `GET /book/vm` → VM page (`booking_type="VM"`, list filtered to VM).
- `GET /book/namespace` → Namespace page (`booking_type="NAMESPACE"`, list filtered to NAMESPACE).

`/` is kept as the VM page (home link + existing tests stay stable); `/book/vm` is an alias.
The `mine/all` + `show_released` filters operate within the active type (their `hx-get` uses
`page_path`).

## Repository

`list_all` / `list_by_user` gain `resource_type: str | None = None`; when set, filter
`BookingModel.resource_type == resource_type`.

## Form (`booking_form.html`)

Parametrized by `booking_type`: renders a hidden `<input name="resource_type">` and only that
type's fields (VM → image + hardware; Namespace → available-namespaces dropdown) + TTL +
submit. **Removes the toggle + `_toggleResourceType` JS from #118** (fields reused). The
quota/namespace error re-render passes the matching `booking_type`.

## Page template (`index.html`)

Parametrized by `booking_type` / `page_path`: breadcrumb and headings show the type; the
resource / connection column headers adapt (Image vs. Namespace, IP Address vs. API URL,
Password hidden for namespaces); filter buttons target `page_path`.

## Edge cases

- Pages other than the two booking pages (catalog, profile, admin) don't set `active_nav` —
  the nav still renders with nothing highlighted.
- Empty namespace pool → namespace page form has no submittable namespace (handled as in #118).
- `/book/vm` and `/` render identically.

## Tests (`tests/test_namespace_booking.py`, extended)

- Header nav shows both booking links + a disabled Environment item.
- `/` (VM page) renders the image field and lists with `resource_type="VM"`.
- `/book/namespace` renders the hidden `resource_type=NAMESPACE` + dropdown, omits the image
  field, and lists with `resource_type="NAMESPACE"`.

## Out of scope

The Environment page/flow itself (the nav entry is a disabled signpost only). Concept/
architecture docs sync remains the final v0.5.0 feature.
