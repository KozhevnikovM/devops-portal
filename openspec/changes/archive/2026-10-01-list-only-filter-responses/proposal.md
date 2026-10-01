## Why

Changing a filter on the VM, namespace or environments page (Mine/All, the label/name box, Show released) currently requests the **full page** route and uses `hx-select` to throw away everything except the list section (#494). Each keystroke in the label box runs every order-form catalog read on the server: images, hardware configs, available namespaces, available static VMs and admin roles on the bookings pages, and blueprints, available namespaces and held namespaces on the environments page. It also renders the whole page. The client then discards all of that work. Filtering is frequent and user-facing, so it should only cost the list read.

## What Changes

- Add a dedicated **list-section fragment** endpoint per list page: `GET /book/vm/list`, `GET /book/namespace/list` and `GET /environments/list`. It takes the same `filter` / `show_released` / `label` parameters as the page. It returns the complete replacement list section: heading and filter controls, rows, empty state, and the first-page Load more / Search older bookings control. It reuses the existing list/projection/pagination path (`_list_page` / `_list_for`). It never reads an order-form catalog.
- The filter controls target the new endpoint instead of the full page, and replace the whole section (`outerHTML`). They no longer use `hx-select`.
- The fragment response sets `HX-Push-Url` to a query-only relative URL (`?filter=…`) for the new filters. The browser resolves it against the page the user is on, keeping any reverse-proxy subpath prefix. The address bar, bookmarks, reload and Back/Forward therefore always point at the full page, never at the fragment.
- Full-page routes (`/`, `/book/vm`, `/book/namespace`, `/environments`) always return the full page, whatever request headers are present. The choice between fragment and page depends only on the URL, never on `HX-Request`.
- The list section markup moves into shared partials. The full page includes them, and the fragment endpoint renders them. The two cannot drift.
- Load more (`…/rows`) is unchanged. It still appends later pages and carries the filters in effect.
- Record query count and response bytes for the same filter request, before and after the change.

No change to the JSON API, row markup, SSE event names, credential authorization or pagination semantics.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `booking-listing`: add the requirement that filter changes on the bookings pages are served by a list-only fragment that loads no order-form catalogs, returns a complete replacement section and keeps the full-page URL in history. Full-page routes keep returning the full page.
- `environment-listing`: the same requirement for the environments page.

## Impact

- **Code**: `app/presentation/routes/bookings.py` gets the `/book/vm/list` and `/book/namespace/list` routes and a list-section renderer shared with `_render_bookings_page`. `app/presentation/routes/environments.py` gets `/environments/list`. `index.html` and `environments.html` move their list sections into new partials, `partials/booking_list_section.html` and `partials/environment_list_section.html`.
- **HTTP surface**: three new HTML-only GET routes (`include_in_schema=False`) that require an authenticated user, like the pages. Existing URLs keep their behaviour.
- **Tests**: new unit/API tests for all three page types. They cover catalog repos never being called on a fragment request, section completeness (rows, empty state, filters, cursor), `HX-Push-Url`, full pages under HTMX headers including history restore, admin/non-admin and filter-after-Load-more. An integration measurement records query count and response bytes.
- **Docs**: `docs/api-reference.md` documents the `/rows` HTML fragments; add the `/list` fragments beside them. Nothing changes for admins.
