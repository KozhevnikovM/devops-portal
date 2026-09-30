## Context

See proposal.md for the motivation. The current shape of the code:

- `index.html` (VM and namespace pages) and `environments.html` each hold a `<section id="bookings-section">` / `<section id="environments-section">`. The section holds the filter controls, the table, the empty state and the first-page Load more row. The filter controls do `hx-get="<page URL>?…" hx-target="#…-section" hx-select="#…-section" hx-push-url="true"` with the default `innerHTML` swap. The selected `<section>` element is therefore inserted *inside* the existing section, so every filter change nests one more `#…-section` (duplicate ids).
- `bookings._render_bookings_page` calls `_list_page(...)` (list read, projection, queue positions, Load more URL). It then reads five catalogs: images, hardware configs, available namespaces, available static VMs, and roles (admin only). `environments_page` calls `_list_for(...)` + `_list_context(...)` and then reads blueprints, available namespaces and held namespaces.
- The `/rows` endpoints (`/book/vm/rows`, `/book/namespace/rows`, `/environments/rows`) already serve later pages from the same helpers. Their Load more URLs are built from `page_path`.
- htmx is 1.9.12, with the `sse` extension. The `<tbody>` carries `hx-ext="sse" sse-connect="/events/stream"`, and rows subscribe with `sse-swap="booking-<id>"` / `environment-<id>`.

## Goals / Non-Goals

**Goals:**
- A filter change costs one list read plus rendering the list section. No catalog reads, no form rendering, no full-page layout.
- One source of truth for list-section markup, shared by the page and the fragment.
- History, reload and bookmarks always resolve to full-page URLs.

**Non-Goals:**
- Changing Load more / `/rows`, the cursor format, page sizes, label-scan bounds or the list SQL.
- Changing the environments empty-state wording. It still says "No environments yet — order one above." even under a filter, as today.
- Changing the SSE transport. The replaced section re-opens its `sse-connect`, exactly as the current `hx-select` swap already does.
- JSON API changes.

## Decisions

### D1. Dedicated fragment endpoints, selected by path, not header-based negotiation

Add `GET /book/vm/list`, `GET /book/namespace/list` and `GET /environments/list` (`include_in_schema=False`, `require_user`). They sit alongside the existing `/rows` endpoints and follow the same `<page_path>/<suffix>` convention.

*Alternative considered:* branch inside the page route on `HX-Request` (or `HX-Target == …-section`). Rejected for three reasons:
1. htmx sends `HX-Request: true` on history-cache-miss restores (with `HX-History-Restore-Request: true`), and those need the full page. The issue explicitly says a bare `HX-Request` is not enough.
2. One URL with two representations needs `Vary` handling, and it is easy to get wrong in caches and the browser's own back-forward cache.
3. A header branch adds a special case to every full-page route. A separate path keeps both routes single-purpose.

`/list` vs `/rows`: `/rows` returns a *continuation* (rows + next control, appended with `outerHTML` on the Load more `<tr>`). `/list` returns a *replacement section*. They are separate contracts, so they get separate paths.

### D2. `HX-Push-Url` response header carries the canonical page URL

The filter controls stop using `hx-push-url="true"`, which would push the fragment URL. The fragment endpoint sets the `HX-Push-Url` response header to `<page_path>?<canonical query>`, built on the server from the parsed parameters. The canonical query has `filter`, then `show_released=1` only when true, then `label` only when non-empty (`urlencode`). This is the same shape `_list_page` / `_list_context` already use for Load more URLs, so a small shared helper builds both.

Why server-side: the label `<input>` sends its current value as a request parameter. A static `hx-push-url="<url>"` attribute can't include the typed value, but the server sees it. htmx 1.9 honours `HX-Push-Url` on any swap response.

`/` and `/book/vm` render the same page. The VM fragment always pushes `/book/vm?…`, matching `page_path` today.

### D3. Swap the whole section with `outerHTML`

Filter controls use `hx-get="<page_path>/list?…" hx-target="#…-section" hx-swap="outerHTML"`, with no `hx-select`. The response's root element is the `<section id="…-section">` itself. This replaces the section in place, drops every appended Load more page and the old next-page control, and fixes the existing nested-section bug. The filter controls live inside the section, so they re-render with the right active state and the right `hx-get` URLs for the next change.

Focus: the label input is re-rendered on every keystroke-triggered swap. It gets a stable `id` (`bookings-label-filter` / `environments-label-filter`), so htmx's swap focus-and-selection restore keeps the caret in the box while the user types. The swap restores only elements with an id. Without one, focus is lost today too.

### D4. Shared section partials and a list-only render helper

- Move the `<section id="bookings-section">…</section>` block from `index.html` into `partials/booking_list_section.html`, and the environments block into `partials/environment_list_section.html`. Both pages `{% include %}` them. Their inputs are only the list context (`bookings` / `environments`, `current_user`, `active_filter`, `show_released`, `label_filter`, `load_more_url`, `searches_older`) plus `booking_type` / `page_path` for bookings.
- bookings.py: extract `_list_section_context(session, current_user, *, booking_type, page_path, filter, show_released, label)`. It wraps `_list_page` for the first page and adds `booking_type` / `page_path`. `_render_bookings_page` = that + catalogs. The new `_render_list_section` = that only, rendered with `partials/booking_list_section.html` and the `HX-Push-Url` header. The two routes share the first-page path, so "identical list section" holds by construction.
- environments.py: the same split around `_list_for` + `_list_context`.

The routes stay thin, the use-case and repository layers are untouched, and no new port is needed. This is purely a presentation-layer change.

### D5. Proving "no catalogs" and measuring cost

- **Unit/API tests (no Postgres):** patch every catalog repo method with an `AsyncMock` and assert `not called` after `GET …/list`, for a user and for an admin, on all three pages. `_role_repo.list_active` is included, since only the admin path would call it. Mirror the test with the page route to assert the catalogs *are* read. That guards against a false pass.
- **Integration measurement** (`-m integration`, real Postgres): seed a fixed dataset. Count statements with a SQLAlchemy `before_cursor_execute` listener, the same technique as `test_progress_write_amplification.py`, and measure `len(response.content)` for the same filter request against the page route ("before") and the fragment ("after"). Assert only the relative facts: fewer statements, fewer bytes, and zero statements against the catalog tables (`vm_images`, `hw_configs`, `namespaces`, `static_vms`, `roles`, `environment_blueprints`, `environment_blueprint_items`). No wall-clock thresholds. Record the absolute numbers in the code PR description and in a "Measurements" section appended to this design.md during implementation.
- **Browser behaviour** (Back/Forward, history-cache miss, focus retention, SSE after swap) can't run in the unit suite. Cover what the server contributes with tests: `HX-Push-Url` value, page route full under `HX-Request`/`HX-History-Restore-Request`, the fragment's root element is the section, and the fragment carries `sse-connect`. Then verify manually in a browser against `docker compose up`, and note the result in the PR.

## Risks / Trade-offs

- [Direct navigation to `/book/vm/list` shows an unstyled fragment] → Acceptable, since it is not a linked URL and is never pushed to history. It matches the existing `/rows` endpoints. The fragment still requires auth.
- [Stale HTML in a long-open tab keeps old `hx-select` controls after deploy] → The old controls keep working. They hit the full-page route, which is unchanged and still returns a section that `hx-select` can extract. A page load picks up the new controls. No compatibility shim is needed.
- [htmx history snapshot caches the DOM, including the SSE-connected tbody] → Same as today. On restore, htmx re-processes the content and `sse-connect` reconnects. A cache miss fetches the full page (D1).
- [Two routes could drift in list rendering] → Mitigated by D4, which gives them one context builder and one partial, and by a test asserting the page's section equals the fragment for the same inputs.

## Migration Plan

No data or config migration. Deploy is a normal app rollout. Rollback means reverting the commit. Old and new HTML both target endpoints that exist in either version, except that new HTML targets `/list`, which a rolled-back server would 404. A reload fixes that, and it only affects tabs opened during the rollback window.
