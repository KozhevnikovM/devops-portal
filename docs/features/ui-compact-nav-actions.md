# Feature: Compact Header Nav & VM Actions Menu

## Goal

Reduce visual clutter in two places:

1. **Header** — collapse the right-side nav items (username, Users, Catalog, Profile, Sign out)
   into a hamburger/dropdown so the header stays clean on all screen sizes.
2. **VM row actions** — hide the Extend select+button and Release button behind a `⋮` menu
   so the actions column doesn't overflow on narrow viewports.

Both changes are purely front-end (templates + Tailwind). No routes, DB changes, or
migrations needed.

---

## Change 1 — Hamburger header menu

**File:** `app/presentation/templates/base.html`

Replace the flat `<div class="ml-auto flex items-center gap-4">` block with a `<details>`
element acting as the toggle. `<details>/<summary>` requires no JavaScript and is
semantically correct.

### Before (schematic)
```
[▶ DevOps Portal / breadcrumb]          [admin  Users  Catalog  Profile  Sign out]
```

### After
```
[▶ DevOps Portal / breadcrumb]                                               [☰]
                                                                     ┌────────────┐
                                                                     │ admin      │
                                                                     │ ─────────  │
                                                                     │ Users      │
                                                                     │ Catalog    │
                                                                     │ Profile    │
                                                                     │ Sign out   │
                                                                     └────────────┘
```

Implementation:
- `<details class="relative ml-auto">` wraps the nav block
- `<summary>` renders the hamburger icon (`☰`) — `list-style: none` to hide the marker
- The menu panel is `absolute right-0 top-full mt-1 ...` positioned below the header
- Clicking anywhere outside closes it naturally (browser `<details>` behaviour)
- Username shown at the top of the open panel as a non-interactive label

---

## Change 2 — VM row actions dropdown

**File:** `app/presentation/templates/partials/booking_row.html`

Replace the `<div class="flex items-center gap-2 flex-wrap">` actions block with a
`<details>` dropdown triggered by a `⋮` button.

### After (READY row, owner)
```
[ID]  [owner]  [image/hw]  [status]  [TTL]  [expires]  [IP]  [password]  [⋮]
                                                                     ┌────────────────┐
                                                                     │ [+12h ▾] Extend│
                                                                     │ Release        │
                                                                     └────────────────┘
```

Implementation:
- `<details class="relative">` wraps the actions cell content
- `<summary>` renders `⋮` (three dots / ellipsis)
- Panel contains the existing Extend form and Release button, stacked vertically
- The Release button keeps its `hx-confirm` attribute
- HTMX swaps (`hx-target="closest tr"`, `hx-swap="outerHTML"`) work unchanged because
  the `<tr>` is still the swap target, not anything inside `<details>`

### Edge cases
- Non-READY rows (no actions): column shows `—` unchanged
- Admin viewing someone else's VM: only Release shown in the panel (no Extend)
- Permanent bookings (ttl=0): Extend option omitted from panel, same as today

---

## Files changed

| File | Change |
|------|--------|
| `app/presentation/templates/base.html` | Replace flat nav div with `<details>` hamburger |
| `app/presentation/templates/partials/booking_row.html` | Replace actions flex row with `<details>` dropdown |

---

## Tests

No new tests required — existing route tests cover rendered HTML and are not affected
by the wrapper element change. Manual verification: open the UI, confirm hamburger
opens/closes, confirm VM actions dropdown works and HTMX swaps still function.
