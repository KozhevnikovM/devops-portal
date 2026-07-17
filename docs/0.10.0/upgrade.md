# Upgrading to v0.10.0

## Before you begin

Read the **breaking changes** below before pulling the new image. Two of them require
operator action before restarting the stack or users will not be able to log in.

---

## Breaking changes

### 1 — `BASE_URL` must be set when serving behind a reverse proxy

v0.10.0 activates a CSRF origin check on every mutating request (POST / PUT / DELETE).
The middleware compares the browser's `Origin` header against `BASE_URL`. If `BASE_URL`
is still the default (`http://localhost:8000`) but the portal is reached at a real
hostname, every form submission — including login — returns **403 Forbidden**.

**Action required:** Add `BASE_URL` to your `.env` (or `docker-compose.override.yml`)
and set it to the exact origin the browser uses, with no trailing slash:

```env
# .env
BASE_URL=https://dp.my-domain.com      # subdomain deployment
# or
BASE_URL=https://my-domain.com         # subpath deployment (https://my-domain.com/dp)
```

Local development over `http://localhost:8000` is unaffected (default matches).

### 2 — Default `changeme` password is now rejected at login

The initial `admin / changeme` credentials are now blocked at the login step: a user
whose stored password hash matches the literal string `changeme` is redirected to the
change-password page and cannot proceed until a new password is set.

**Action required:** If any accounts still use `changeme` as their password, an admin
must reset them **before** the upgrade, or those users will be forced through the
password-change flow on their next login.

Reset a password via the API (requires an admin session or API key):

```bash
curl -s -X POST https://dp.my-domain.com/api/users/<user-id>/password \
  -H "X-API-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"password": "new-secure-password"}'
```

Or use **Admin → Users → ⋮ → Reset password** in the UI.

---

## Upgrade procedure

### 1. Pull the new image and restart

```bash
# On the server where the stack runs
git pull                        # or docker pull if using a registry
docker compose pull             # fetch new image layers
docker compose up -d            # recreate containers; init runs migrations automatically
```

The `init` container applies all pending Alembic migrations before the `app` starts.
No manual migration step is needed.

### 2. Verify the stack is healthy

```bash
docker compose ps
# All services should show (healthy) within ~60 s of startup.

# Quick smoke test
curl -f https://dp.my-domain.com/health
# → {"status": "ok"}
```

### 3. Confirm login works

Open the portal in a browser and log in. If you see a 403 on form submit, recheck
`BASE_URL` (see [Breaking change #1](#1--base_url-must-be-set-when-serving-behind-a-reverse-proxy) above).

---

## Database migrations

One new migration ships with v0.10.0:

| Revision | Change |
|----------|--------|
| `0030` | Adds `label VARCHAR(128) NULL` column to the `bookings` table |

The `init` container runs `alembic upgrade head` automatically. To apply manually:

```bash
docker compose run --rm init alembic upgrade head
```

No data is modified — the new column defaults to `NULL` for all existing rows.

---

## What's new

### Owner column on the Environments table

The environments list now shows an **Owner** column so admins can see at a glance who
holds each environment. If the environment was ordered by a dispatcher on behalf of
someone else, a smaller "via \<dispatcher>" line appears below the owner name.

### Booking label (optional)

Users can attach a short free-text label (up to 128 characters) to a VM booking — useful
for distinguishing otherwise-identical bookings (e.g. "PR #42 perf test").

- **UI:** A **Label** field appears in the booking form above the Duration field.
- **API:** `POST /api/bookings` accepts an optional `"label"` string field. The label is
  returned in `GET /api/bookings` and `GET /api/bookings/{id}` responses.

### Ansible role vars: YAML editor

The **Default vars** field on Ansible role cards in the catalog is now a YAML textarea
instead of a single-line JSON input.

- Existing roles with JSON-stored vars are displayed correctly — JSON is valid YAML.
- New and edited vars are accepted as YAML (mappings only; lists and bare scalars are
  rejected with a clear error message).

### Self-service password change

Users can change their own password from the **Profile** page (top-right menu → Profile).
Admins can reset any user's password from **Admin → Users → ⋮ → Reset password** or via
the API (`POST /api/users/{id}/password`).

All active sessions for the affected user are invalidated on password change.

### Admin force-release for FAILED bookings

Admins can now release a booking that is stuck in the `FAILED` state via the bookings
list (**⋮ → Force release**). Previously, FAILED bookings had to be cleaned up directly
in the database.

### Ansible roles: encrypted secret vars

A new **Secret vars** field on Ansible role cards accepts sensitive values (tokens,
passwords) that are stored encrypted at rest (Fernet) and injected into Ansible via a
temporary file that is removed immediately after the play. Secret var keys appear in logs;
values are never logged.

### Ansible collections: offline/air-gapped install

Role definitions now accept a `collections_tarball` path. When set, the Ansible task
runner installs collections from the local tarball instead of reaching out to Ansible
Galaxy. Set `ANSIBLE_GALAXY_ONLINE=false` to enforce offline-only mode across all roles.

### Environment filters (Mine / All / Released)

The Environments page now has **Mine** and **All** toggle buttons to filter the list, plus
a **Show released** toggle. The active filter is preserved in the URL so it survives
page refreshes and can be bookmarked.

---

## Rollback

If you need to roll back to v0.9.0:

```bash
# 1. Downgrade the single new migration
docker compose run --rm init alembic downgrade -1

# 2. Check out the previous release tag and restart
git checkout v0.9.0
docker compose up -d
```

The `label` column downgrade is safe — the column is nullable and no existing logic
depended on it.
