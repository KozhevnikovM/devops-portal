# Feature: Navigation Home Link (#90)

## Goal

Make the "▶ DevOps Portal" text in the header a clickable link that returns users to `/` from
any sub-page. Add a dynamic breadcrumb suffix so users know which section they're in.

## What Changes

### `app/presentation/templates/base.html`

- Wrap the portal name span in `<a href="/">` so clicking it navigates home.
- Replace the hardcoded `/ VM Booking` span with `/ {% block breadcrumb %}VM Booking{% endblock %}`
  so each template can declare its own section label.

### Per-page breadcrumb overrides

Each template that extends `base.html` adds:

```html
{% block breadcrumb %}Section Name{% endblock %}
```

| Template | Label |
|----------|-------|
| `index.html` | `VM Booking` (default — no change needed) |
| `admin/users.html` | `Users` |
| `admin/catalog.html` | `Catalog` |
| `profile.html` | `Profile` |

## Expected Behaviour

- Clicking `▶ DevOps Portal` from any page navigates to `/`.
- The breadcrumb suffix reflects the current page section.
- Pages that don't override the block continue to show `/ VM Booking`.

## No DB / Migration / API Changes

Template-only. No new routes, no migrations, no test suite additions required.
