# Security Review & Remediation Plan

Source: security review of the project at branch `feature/134/v060-docs` (v0.6.0).
Status: **planning only — no code to be written until each item below is approved per the
CLAUDE.md bugfix/feature process.**

This document catalogs the security findings from a manual review of the auth, routing,
repository, task, and Terraform layers, then lays out a phased remediation plan. Each fix is
an independent branch (per the git workflow); security bugs get a `docs/bugfix/` doc plus a
regression test that fails before and passes after.

> Overlap note: findings #1 (credential exposure) and #5 (session cookie `Secure`) are also
> listed in `docs/code-quality-remediation-plan.md`. They are restated here with full security
> context. Whichever plan ships the fix first closes both entries — do not double-implement.

## Severity summary

| # | Finding | Severity | Phase | Branch |
|---|---------|----------|-------|--------|
| 1 | `GET /bookings` leaks every user's VM password & IP | High | 1 | `bugfix/credential-exposure-bookings-list` |
| 2 | `GET /bookings/{id}/row` has no ownership check (IDOR) | Medium | 1 | `bugfix/booking-row-ownership-check` |
| 3 | Session cookie missing `Secure` flag | Medium | 1 | `bugfix/session-cookie-secure` |
| 4 | HTML injection in admin error fragments | Low | 2 | `bugfix/admin-error-html-escape` |
| 5 | HCL/template injection in Terraform workspace files | Low | 2 | `bugfix/terraform-tfvars-json` |
| 6 | Username enumeration via login timing | Low | 2 | `bugfix/login-timing-enumeration` |
| 7 | Default admin password only warned, not enforced | Hardening | 3 | `feature/enforce-admin-password-change` |
| 8 | Vended credentials stored in plaintext at rest | Decision | 3 | (decision doc only) |

---

## Phase 1 — Access control & transport (ship first)

High-confidence, contained fixes that close real exposure. Each is an independent branch.

### 1. `bugfix/credential-exposure-bookings-list` — High

- **Root cause:** `GET /bookings`
  ([`app/presentation/routes/bookings.py:119-148`](../app/presentation/routes/bookings.py#L119-L148))
  calls `_repo.list_all(session)` with no owner scoping and serializes `vm_password`, `vm_ip`,
  and static-VM `host`/`username` for **every** booking. Any authenticated user — via session
  cookie or a valid `dp_` API key — can read every other user's provisioned VM admin password.
  The HTML pages gate credentials behind `can_see_creds = is_owner or admin`
  ([`partials/booking_row.html:69-84`](../app/presentation/templates/partials/booking_row.html#L69-L84)),
  and `release_booking` / `get_booking_audit` both check
  `booking.user_id != current_user.id and role != "admin"`. This JSON route is the one path
  with no such guard.
- **Changes:**
  - Scope the list to `current_user` (reuse `list_by_user`) unless the caller is admin.
  - Remove `vm_password` (and any static-VM secret) from the list payload entirely; secrets
    should only be retrievable on the owner-scoped single-booking view.
- **Regression test:** a non-owner calling `GET /bookings` sees only their own rows and no
  foreign passwords; an admin still sees all rows.
- **Docs:** `api-reference.md` — document the owner scoping and the removed field.

### 2. `bugfix/booking-row-ownership-check` — Medium

- **Root cause:** `GET /bookings/{id}/row`
  ([`app/presentation/routes/bookings.py:293-304`](../app/presentation/routes/bookings.py#L293-L304))
  fetches any booking by UUID with no ownership check. The template hides credentials, but
  `vm_ip`, status, owner username, and image name are still rendered to any authenticated user
  who supplies a booking UUID. Risk is bounded by UUID unguessability, but the route is
  inconsistent with the gated `DELETE` / `extend` / `audit` routes.
- **Changes:** apply the same `owner or admin` guard used by `release_booking`; return 403 (or
  404) otherwise.
- **Regression test:** a non-owner `GET /bookings/{id}/row` is rejected; owner and admin succeed.

### 3. `bugfix/session-cookie-secure` — Medium

- **Root cause:** the login cookie
  ([`app/presentation/routes/auth.py:71-76`](../app/presentation/routes/auth.py#L71-L76))
  sets `httponly=True, samesite="lax"` but not `secure=True`, so the session id can be
  transmitted over plain HTTP and captured on the wire.
- **Changes:** add `secure=True`, driven by a `SESSION_COOKIE_SECURE: bool = True` config flag
  so local HTTP development can opt out. With `SameSite=Lax` already blocking cross-site
  POST/DELETE/PUT, adding `Secure` also completes the cookie-CSRF posture.
- **Regression test:** `Set-Cookie` includes `Secure` when the flag is on; absent when off.

---

## Phase 2 — Injection & enumeration hardening

Lower severity (the dangerous inputs are currently admin-controlled or constrained), but each
is a real defect and cheap to fix defensively.

### 4. `bugfix/admin-error-html-escape` — Low

- **Root cause:** several admin handlers build error HTML with f-strings that interpolate user
  input unescaped, returned as raw `HTMLResponse`:
  [`auth.py:222`](../app/presentation/routes/auth.py#L222),
  [`admin.py:80`](../app/presentation/routes/admin.py#L80),
  [`admin.py:218`](../app/presentation/routes/admin.py#L218),
  [`admin.py:365`](../app/presentation/routes/admin.py#L365),
  [`admin.py:528`](../app/presentation/routes/admin.py#L528).
  These are admin-only routes, so it is effectively self-XSS today, but a name like
  `<img src=x onerror=...>` injects live markup into the response.
- **Changes:** escape the interpolated value with `markupsafe.escape()`, or render a small
  template fragment instead of hand-built HTML.
- **Regression test:** posting a duplicate name containing `<script>` returns an escaped body.

### 5. `bugfix/terraform-tfvars-json` — Low

- **Root cause:** `TerraformVcdAdapter._write_workspace`
  ([`app/infrastructure/terraform/vcd_adapter.py:53-127`](../app/infrastructure/terraform/vcd_adapter.py#L53-L127))
  interpolates values straight into `main.tf` / `terraform.tfvars` with f-strings. Today's
  inputs are safe (`name` is `portal-{uuid}`, `vm_password` is `[A-Za-z0-9]`), but
  `vapp_template_id` is admin free-text and the VCD settings come from env — a `"` breaks out of
  the string and injects HCL.
- **Changes:** write variable values as `terraform.tfvars.json` via `json.dump`, so values
  cannot break quoting. Keep the provider/module `main.tf` static.
- **Regression test:** a config value containing `"` round-trips through the written
  `.tfvars.json` as a literal string.

### 6. `bugfix/login-timing-enumeration` — Low

- **Root cause:** `login`
  ([`app/presentation/routes/auth.py:53-54`](../app/presentation/routes/auth.py#L53-L54))
  skips `bcrypt.checkpw` when the username does not exist, so a missing user returns measurably
  faster than a real user with a wrong password — a username-enumeration oracle.
- **Changes:** on the user-miss path, compare against a fixed dummy bcrypt hash to equalize work.
- **Regression test:** unit-level — the miss path still calls `bcrypt.checkpw` once (assert via
  patch/spy), and login outcome is unchanged.

---

## Phase 3 — Hardening & decisions (no urgent exposure)

### 7. `feature/enforce-admin-password-change` — Hardening

- **Context:** `ADMIN_PASSWORD` defaults to `changeme`
  ([`config.py:15`](../app/config.py#L15)); startup only logs a warning
  ([`main.py:49-50`](../app/main.py#L49-L50)). Acceptable for dev, dangerous if it reaches prod.
- **Options to decide:** (a) refuse to start when the seeded admin still has the default
  password and a `PRODUCTION`/`ENV` flag is set; or (b) force a password change on first admin
  login. Needs a feature doc + approval before implementing.

### 8. Vended credentials stored in plaintext — Decision (doc only)

- **Context:** `vm_password` and static-VM `password` / `ssh_key` are stored and returned in
  cleartext — inherent to a credential-vending feature, but it means DB or backup access equals
  credential access. The existing `code-quality-remediation-plan.md` finding #7 already flags
  the static-VM side.
- **Action:** write a short decision note (encrypt-at-rest with a KMS/Fernet key vs. accept the
  risk with documented DB-access controls). No code until a direction is chosen.

---

## Out of scope / verified clean

- **SQL injection:** all queries use SQLAlchemy parameter binding — no string-built SQL.
- **Password hashing:** bcrypt with per-hash salt ([`auth.py:54`](../app/presentation/routes/auth.py#L54),
  [`routes/auth.py:123`](../app/presentation/routes/auth.py#L123)).
- **API keys:** 128-bit `secrets.token_hex(16)` keys, stored as SHA-256 hashes, never echoed
  after creation ([`user_repo.py:129-146`](../app/infrastructure/repositories/user_repo.py#L129-L146)).
- **CSRF:** no token, but `SameSite=Lax` cookies block cross-site state-changing requests;
  Bearer API-key requests are not cookie-borne and so are not CSRF-able. Closing #3 completes it.
- **Authorization on mutations:** `release`, `extend`, and `audit` already enforce owner/admin.

---

## Suggested sequencing

1. **Phase 1** first and together (#1 is the only High; #2/#3 are small and related to access
   control / transport).
2. **Phase 2** as a follow-up batch — defensive, low risk, independently shippable.
3. **Phase 3** requires product decisions; open the decision doc (#8) and feature doc (#7)
   before any implementation.

Per CLAUDE.md: open each branch from fresh `main`, add the `docs/bugfix/` doc, get approval,
then implement with the regression test.
