# Feature: Responsive/layout hardening pass

## Goal

Proactive responsiveness/layout-hardening pass across the portal's Jinja2 templates
(Tailwind CSS v3.4.0, HTMX). Not a reaction to specific reported bugs — a grep across
all 21 templates for `sm:`/`md:`/`lg:`/`xl:`/`2xl:` prefixes returns zero matches
anywhere in the codebase, so this closes that gap from mobile/tablet through
ultrawide/4K, and fixes two real geometry bugs found during the audit (table
overflow, and a dropdown-clipping interaction the table fix would otherwise
introduce).

Presentation-only: everything in scope lives under
`app/presentation/templates/**/*.html`. No API, CLI, or DB changes — no Python
routes, use cases, or migrations are touched.

---

## Findings that shape the approach

- **Tables, not cards.** Every list (bookings, environments, namespaces, images, hw
  configs, static VMs, roles, blueprints, users, API keys, audit log) is a plain
  `<table>`, 5–11 columns. Only `audit_log.html` wraps its table in
  `overflow-x-auto` today — the other 10 tables don't, so they force page-level
  horizontal scroll or clip content on narrow viewports.
- **Row-action dropdowns.** 8 of those tables render a per-row `⋮` menu as
  `<details class="relative"><summary>⋮</summary><div class="absolute right-0
  bottom-full mb-1 w-44/48 ...">` (opens **upward**). Once a wrapper's
  `overflow-x-auto` is added, the CSS overflow spec forces that wrapper's
  `overflow-y` to compute as `auto` too, and the scrollable-overflow clipping
  region includes absolutely-positioned descendants — so an upward-opening menu on
  a row near the top of a tall table gets visually clipped by the new wrapper.
  This is a real geometry bug the table fix would introduce if left unaddressed.
- **Page shell** (`base.html`): single top header row (logo + 3 nav links +
  `<details>` hamburger/user-menu), no responsive collapse anywhere. `<main
  class="max-w-5xl mx-auto px-6 py-8">` is the only width constraint in the whole
  shell — static regardless of viewport, so ultrawide/4K just gets dead margins
  rather than a fluid, capped column. **`admin/catalog.html:6` adds its own nested
  `<div class="max-w-5xl">`** inside that `<main>` — this currently
  double-constrains the catalog page (the page with the *widest* tables, up to 11
  columns) and would silently cancel out any widening done to `<main>` if left
  unchanged.
- **Forms.** ~50 fixed-width (`w-20`…`w-96`) input/select/textarea sites across 11
  files rely only on `flex items-end gap-3 flex-wrap` to reflow — fields keep their
  compact fixed width and wrap onto new lines on mobile rather than filling the
  row.
- **`login.html`** doesn't extend `base.html`; its card is a fixed `w-80` (20rem)
  div — fine down to ~352px viewport width but overflows below that.
- No zebra striping on any table row (`hover:bg-gray-800/40` only) — simplifies
  reasoning about any future sticky-column work, though none is needed here.
- The existing global outside-click-closes-`<details>` handler
  (`base.html:24-28`) and the hamburger `<details>` pattern are reused as-is — no
  new JS/Alpine dependency introduced.

---

## What changes

### 1. Page shell — `app/presentation/templates/base.html`

- Collapse the 3 inline nav links (`Virtual Machines`/`Namespaces`/`Environments`)
  behind `hidden md:flex`, and duplicate them as plain rows inside the *existing*
  hamburger dropdown under a `md:hidden` block, above the current
  username/admin-links section. Reuses the one dropdown mechanism and the one
  outside-click handler already in the file.
- Header `px-6` → `px-4 sm:px-6` to reclaim gutter space at 375px, where the
  header row is just logo + hamburger.
- `<main>`: `max-w-5xl mx-auto px-6 py-8` → `max-w-5xl lg:max-w-6xl 2xl:max-w-7xl
  mx-auto px-4 sm:px-6 py-8`. Stays on Tailwind's standard scale (no arbitrary
  values) — unchanged up to 1024px (`lg`), widens modestly above that, caps at
  80rem/1280px so ultrawide/4K get a comfortably capped, centered column rather
  than an ever-growing line length.

### 2. `admin/catalog.html:6`

Change the nested `<div class="max-w-5xl">` to the same tiered scale as `<main>`
(`max-w-5xl lg:max-w-6xl 2xl:max-w-7xl`) — otherwise this page (the one with the
widest tables) is the one page that wouldn't benefit from the shell change above.

### 3. Ten un-wrapped tables — `overflow-x-auto` + dropdown-direction fix

Same one-word addition to each existing `<div class="bg-gray-900 border
border-gray-800 rounded-lg">` wrapper, matching `audit_log.html`'s existing
convention: add `overflow-x-auto` to the class list in `index.html`,
`environments.html`, and the six catalog sub-table wrappers in
`admin/catalog.html` (image/hw-config/namespace/static-vm/role/blueprint tables).
Two variants on the same pattern:
- `admin/users.html:67` currently has `overflow-hidden` on that wrapper (for its
  rounded corners) — **replace** it with `overflow-x-auto` rather than adding a
  second overflow value.
- `partials/api_key_list.html` has no bounding wrapper at all today (it sits
  inside a `border-t pt-6` div in `profile.html` alongside the add-key form) —
  wrap just the `<table>` itself inside the partial, not the surrounding block, so
  the add-key form doesn't get pulled into a scroll container.

**Dropdown-clipping fix**: in the 8 affected partials (`booking_row.html`,
`environment_row.html`, `hw_config_table.html`, `blueprint_table.html`,
`static_vm_table.html`, `namespace_table.html`, `image_table.html`,
`role_table.html`), flip the first row's menu to open *downward* instead of
upward using the loop variable already in scope (these partials are `{% include
%}`d from inside `{% for %}` loops with default Jinja context-passing — `loop`,
including `loop.first`, is visible with no Python change):
```
class="absolute right-0 {% if loop.first %}top-full mt-1{% else %}bottom-full mb-1{% endif %} w-44 ..."
```
This closes the worst case (top row, most visible/clicked) for a one-line-per-file
change. It doesn't fully solve row 2+ under the tallest menu
(`booking_row.html`'s multi-section READY/FAILED menu) — that residual edge case
is a known trade-off of staying CSS-only; a fully robust fix needs JS-based
positioning (Popper-style/Alpine `x-anchor`) and is explicitly **out of scope**
for this pass (see below).

### 4. Form inputs — reuse existing `flex-wrap`, no restructuring

Two-site mechanical change per fixed-width field, repeated across
`booking_form.html`, `environment_order_form.html`, `admin/users.html`,
`admin/catalog.html`'s five add-forms, and the inline row-edit forms in
`user_table.html`/`hw_config_table.html`/`static_vm_table.html`/
`namespace_table.html`/`image_table.html`/`role_table.html`/`blueprint_table.html`
(~50 sites total, all following the identical pattern):
- field wrapper: `class="flex flex-col gap-1.5"` → add `w-full sm:w-auto`
- the `<input>`/`<select>`/`<textarea>`: `w-NN` → `w-full sm:w-NN`

Fields already using `flex-1 min-w-48` (e.g. `blueprint_table.html`'s description
field, `namespace_table.html`'s API URL field) already grow correctly — left
untouched.

### 5. `login.html`

`<div class="w-80">` (line 10) → `<div class="w-full max-w-xs mx-4">`. `max-w-xs`
(20rem) is numerically identical to today's `w-80`, so nothing changes at
tablet/desktop width; `w-full` + `mx-4` just adds a fluid floor with safe gutters
below ~352px so the card doesn't run edge-to-edge on the narrowest phones.
Isolated from every other change — `login.html` doesn't extend `base.html`.

### Build

No `tailwind.config.js`/`tailwind.input.css` changes needed — everything above is
stock utilities and responsive variants already covered by the existing content
glob. Rebuild once implementation is done:
```
npx tailwindcss -i tailwind.input.css -o app/static/css/tailwind.css --minify
```

---

## Modified files

| File | Change |
|------|--------|
| `app/presentation/templates/base.html` | Responsive nav collapse, tiered `<main>` max-width, header padding |
| `app/presentation/templates/admin/catalog.html` | Match tiered max-width; add `overflow-x-auto` to 6 table wrappers |
| `app/presentation/templates/index.html` | Add `overflow-x-auto` to booking table wrapper |
| `app/presentation/templates/environments.html` | Add `overflow-x-auto` to environments table wrapper |
| `app/presentation/templates/admin/users.html` | Swap `overflow-hidden` → `overflow-x-auto` |
| `app/presentation/templates/login.html` | Fluid card width for sub-352px viewports |
| `app/presentation/templates/partials/booking_row.html`, `environment_row.html`, `hw_config_table.html`, `blueprint_table.html`, `static_vm_table.html`, `namespace_table.html`, `image_table.html`, `role_table.html` | `loop.first` dropdown-direction fix |
| `app/presentation/templates/partials/api_key_list.html` | Wrap table in `overflow-x-auto` |
| `app/presentation/templates/partials/booking_form.html`, `environment_order_form.html`, `user_table.html` | Responsive (`w-full sm:w-NN`) form fields |

---

## Edge cases

- **Table with a single row (or empty state)**: `overflow-x-auto` on an
  already-narrow table is a no-op — no visual change.
- **Row 2+ under a tall dropdown menu near the top of a table**: not fully solved
  by the `loop.first` fix (see above) — accepted trade-off, documented as a
  follow-up rather than silently left unmentioned.
- **`admin/users.html` rounded corners**: swapping `overflow-hidden` for
  `overflow-x-auto` must be confirmed visually to still clip the table's corners
  to the wrapper's `rounded-lg` (expected per spec, since any non-`visible`
  overflow value clips to the border-radius) — checked in verification below.
- **Very narrow login viewport (<352px)**: card now has safe gutters instead of
  overflowing edge-to-edge.

---

## Explicitly out of scope (follow-up, not this pass)

- Per-breakpoint column-hiding on any table.
- Any JS/Alpine addition, including a fully robust dropdown-flip/portal fix beyond
  the `loop.first` mitigation above.
- Backend/domain/DB changes.

---

## Verification (no new `pytest` coverage — presentation-only, no logic change)

1. Rebuild CSS (command above).
2. Drive the running app with Playwright across:
   - Pages: `/login`, `/book/vm`, `/book/namespace`, `/environments`,
     `/admin/catalog` (all 6 sub-tables), `/admin/users`, `/profile`.
   - Breakpoints: 375, 768 (exact nav-collapse boundary), 1024 (old fixed cap —
     confirm no regression), 1440, 1920, and one ultrawide/4K width (2560+).
   - At each combination: confirm `document.documentElement.scrollWidth <=
     window.innerWidth` (no page-level horizontal overflow), open the first row's
     `⋮` menu and screenshot to confirm it isn't clipped by the new
     `overflow-x-auto` wrapper, confirm form fields are full-width/stacked at 375
     and revert to their original compact widths at ≥640.
3. No `docs/admin-guide.md`/`docs/api-reference.md` updates needed — no
   endpoint/CLI/workflow change, pure layout.
