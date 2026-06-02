# Bugfix: Booking action menu (⋮) clipped behind the table

## Symptom

On the Virtual Machines / Namespaces pages, opening a booking row's `⋮` action menu
(Extend / Release / Delete) shows it **clipped at the table edge / behind surrounding
blocks** instead of floating on top.

## Root cause

The dropdown is correctly `position: absolute` with `z-50`
(`app/presentation/templates/partials/booking_row.html`), but the bookings table is wrapped
in a container with **`overflow-hidden`**:

```html
<!-- app/presentation/templates/index.html -->
<div class="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
    <table> … rows with the ⋮ menu … </table>
</div>
```

`overflow-hidden` clips any descendant that paints outside the box, regardless of
`z-index`. The menu opens upward (`bottom-full`) and extends past the table, so it is cut
off. (`overflow-hidden` was there only to clip the table's square corners to the wrapper's
`rounded-lg`.)

## Fix

Drop `overflow-hidden` from the table wrapper so the menu can overflow and float above
neighbouring sections (its `z-50` then takes effect). The rounded border is preserved; only
the table's inner corners go square, which is not visible against the dark fill.

```diff
- <div class="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
+ <div class="bg-gray-900 border border-gray-800 rounded-lg">
```

No behaviour change beyond the menu rendering on top. Applies to both booking pages (shared
`index.html`).

## Related: close on outside click

The `⋮` row menu and the `☰` nav are native `<details>` elements, which stay open until the
summary is clicked again — clicking elsewhere left them open. Added a global click handler in
`base.html` that closes any open `<details>` when the click lands outside it (also gives
one-open-at-a-time behaviour).

## Regression test

Render a bookings page containing a `READY` booking (so the `⋮` menu is present) and assert
the table wrapper no longer carries the clipping class (`rounded-lg overflow-hidden`) while
the menu's `z-50` layer is still rendered. Fails before the fix, passes after.

## Scope / branch

The clipping wrapper lives in `index.html`, which is currently being rewritten in the open
booking-UI PR (#121). To avoid a same-file conflict and because it's the same surface under
review, the fix is applied on that branch.
