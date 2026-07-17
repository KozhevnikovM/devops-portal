# Feature: Owner column on the Environments UI tab (#272)

## Goal

Show who owns each environment on the Environments list page, so admins and dispatchers can
identify environments at a glance without opening individual rows.

## What changes

### Template only — no API, DB, or domain changes

The `owner_username` field already exists on the `Environment` entity and is populated by the
environment repository. It is used in `environment_row.html` today (for the `is_owner` check and
the "via dispatcher" display hint). It is just not rendered as a visible column.

**`app/presentation/templates/environments.html`**
- Add an `Owner` `<th>` column header between `Resources` and `Expires`.
- When rendered as admin/dispatcher, always show the column.
- When rendered as a regular user, the column can be omitted or show only the current user's name
  (since all visible environments belong to them — personal view is filtered). Simplest approach:
  always show the column for all roles; it is never sensitive.

**`app/presentation/templates/partials/environment_row.html`**
- Add a `<td>` that renders `environment.owner_username`.
- If `environment.created_by_username` is set (dispatcher order), render a secondary line:
  `via <dispatcher>` — same pattern as the existing inline hint, but now visible in the table.

## Expected behaviour / edge cases

- **Admin/dispatcher view**: full owner names visible across all rows.
- **Regular user view**: all rows belong to the logged-in user, so the column shows their own
  name on every row — not wrong, just redundant. Acceptable for consistency; can revisit UX
  later.
- **Dispatcher-ordered environments**: primary cell shows the beneficiary (`owner_username`);
  secondary line shows `via <dispatcher>` in muted text.
- **No API or DB change**: the field is already returned by `GET /environments` and the
  environment listing route.
- **No test required** beyond manual verification — this is a pure template change with no
  logic paths.
