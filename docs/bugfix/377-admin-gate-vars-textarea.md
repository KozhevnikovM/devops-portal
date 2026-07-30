# Bugfix: admin-gate the Ansible variables textarea on the VM booking form (#377 follow-up)

## Root cause

F-6 (#377, PR #381) hid the **Ansible roles** checkbox/dropdown from non-admins on the VM
booking form, by only passing a non-empty `roles` list to admins and relying on the
template's `{% if roles %}` gate. That gate wraps *only* the roles picker
(`app/presentation/templates/partials/booking_form.html:84-105`) — the **Ansible variables**
textarea sits in its own unconditional `<div>` immediately after (lines 106-116), so it kept
rendering for every user regardless of role. This was a deliberate, documented scope call in
the original F-6 design ("the issue names roles specifically, so vars stays as-is unless told
otherwise"), but the issue author (`docs`/issue #377 comment) has since flagged it as
inconsistent in practice: a non-admin now sees a picker with roles hidden but a vars box that
still visibly lets them inject arbitrary `key: value` pairs, which reads as an incomplete gate
rather than an intentional one.

## What changes

- `app/presentation/routes/bookings.py`: reuse the same `current_user.role == "admin"` gate
  already computed for `roles` to also gate what's passed for `vars_yaml`/the vars-textarea
  visibility — non-admins get an empty/absent vars field the same way they get an empty roles
  list. Concretely: extend the existing admin check so the template only renders the vars
  textarea when `current_user.role == "admin"`, mirroring the roles gate exactly (one
  conditional controls both fields, since they're the same "admin-only advanced config"
  concept).
- `app/presentation/templates/partials/booking_form.html`: wrap the vars textarea's `<div>` in
  the same `{% if roles %}` conditional as the roles picker (renaming/reusing that context
  variable, or introducing a single `is_admin`/`show_advanced_config`-style flag — implementation
  detail, but the two fields must share one gate so they can never disagree again) rather than
  leaving it as a second, independently-unwrapped block.
- `create_booking` (`POST /bookings`): apply the same server-side defense-in-depth already in
  place for `roles` (silently ignored for non-admins) to `vars_yaml`/`extra_vars` — a direct
  POST from a non-admin bypassing the hidden UI should have its vars ignored, not applied.
- `POST /api/bookings` remains **entirely unaffected** — same as the original F-6 scope. This
  is a browser-form-only restriction; API/CI callers can still submit both `roles` and `vars`
  directly, unchanged.

## Expected behaviour after the fix

- Non-admin: booking form shows **neither** the roles picker **nor** the vars textarea — the
  whole "advanced config" block is gone, consistent instead of half-hidden.
- Admin: booking form is unchanged — both roles picker and vars textarea render exactly as
  before.
- A non-admin `POST /bookings` with `vars_yaml` form data creates the booking with empty
  `extra_vars` (silently ignored), not an error — mirroring how `roles` is already handled.
- `POST /api/bookings` is byte-for-byte unchanged for both `roles` and `vars` — no behavior
  change for API/CI callers regardless of the submitting user's role.
- `docs/admin-guide.md`'s existing note ("The **Ansible variables** textarea ... stays
  available to every user") gets corrected to reflect the new admin-only scope.

## Regression test

New/updated tests confirming: non-admin GET renders neither field; admin GET renders both;
non-admin `POST /bookings` with `vars_yaml` produces `extra_vars == {}`; existing
`POST /api/bookings` vars-forwarding tests for non-admin users continue to pass unmodified.
