# Feature: Encrypted secret_vars for Ansible roles

## Goal

Allow admins to store sensitive Ansible variables (passwords, tokens, API keys) per-role via the
existing catalog UI. At run time the secrets are decrypted and written to a `secrets.yml` file in
the throwaway temp directory; the playbook loads them via `vars_files:` — Ansible reads secrets
from the filesystem, never from inline playbook YAML.

## What changes

### Feature flag

`SECRET_VARS_ENABLED: bool = True` in `app/config.py` (env var `SECRET_VARS_ENABLED=false` to
disable). When `False`:

- Admin UI: `secret_vars` textarea and masked key list are hidden entirely.
- API: `secret_vars` field on `RoleCreate`/`RoleUpdate` is silently ignored (no error — clean
  migration path; Vault will fill the gap without requiring API callers to change).
- Booking snapshot: `secret_vars` always written as `{}`.
- Ansible runner: decrypt/`secrets.yml` step is skipped unconditionally.
- DB column and migration are unconditional — no data is lost by toggling the flag, and
  re-enabling it restores previously stored (encrypted) secrets.

When Vault is ready: set `SECRET_VARS_ENABLED=false`, implement a Vault adapter behind the same
interface, then swap in a new flag (e.g. `VAULT_SECRETS_ENABLED`) without touching the rest of
the codebase.

### Config

`SECRETS_ENCRYPTION_KEY` — new env var (Fernet key: `cryptography.fernet.Fernet.generate_key()`).
**Fail-closed**: if `secret_vars` is non-empty and the key is absent/empty, the write is rejected
with HTTP 400 (admin UI) / 422 (API). The system never silently stores plaintext secrets.
Local dev: add a **placeholder** (not a real key) to `.env.example` with a comment showing how
to generate one. Never commit a working Fernet key to git — even a "dev" key gets copied into
real environments when people do `cp .env.example .env` without reading it.

```
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRETS_ENCRYPTION_KEY=REPLACE_ME
```

### Key rotation

`SECRETS_ENCRYPTION_KEY` is a single static Fernet key with no versioning in v1. **Rotating the
key bricks every stored secret** — the ciphertext in the DB was encrypted with the old key and
cannot be decrypted with the new one. Runbook: before changing the key, re-enter all role
`secret_vars` via the admin UI (or API) after the new key is deployed. Full key-versioning
(envelope encryption, key-ID prefix on ciphertext) is out of scope for v1 but should be
revisited if rotation frequency increases.

### DB migration

`secret_vars JSONB NOT NULL DEFAULT '{}'` column on the `roles` table. Values are per-key
Fernet-encrypted before storage: keys remain readable, values are ciphertext strings.

### New module: `app/infrastructure/crypto.py`

```python
encrypt_dict(d: dict, key: str) -> dict   # encrypts each value; raises if key is empty and d is non-empty
decrypt_dict(d: dict, key: str) -> dict   # decrypts all values atomically; raises on first bad ciphertext
```

`decrypt_dict` iterates the full dict and raises before returning any plaintext if any value fails
— callers never receive a partial result. Values are JSON-serialised before encryption so any
scalar type is supported.

### Domain entity

`Role.secret_vars: dict` — carries the **encrypted** blob throughout the system. Only the ansible
runner decrypts it.

### Serialization audit — `secret_vars` must be write-only everywhere

`RoleResponse` omits `secret_vars`. Before shipping, grep every serialization path for `Role` and
`RoleModel`:

- `GET /admin/catalog/roles` — template renders key names only, never values (masked read view)
- `GET /api/roles` / `GET /api/roles/{id}` — `RoleResponse` has no `secret_vars` field
- `api_bookings.py` / `api_environments.py` snapshot — encrypted blob included (see below); not
  exposed to end-users (booking detail views must not surface `config_roles` raw JSON to
  non-admins)
- Exception payloads — ensure no exception handler dumps entity state (e.g. `str(role)` in a
  500 response); `Role.__repr__` must not include `secret_vars`
- Debug logging — grep for `logger.debug` / `logger.info` that logs a `Role`, `RoleModel`, or
  `config_roles` snapshot wholesale; remove or redact before merging

### Repository (`role_repo.py`)

`create` / `update`: `encrypt_dict(secret_vars, key)` before writing. `_to_entity` passes the
stored blob straight through (no decryption).

Audit log: on any write that changes `secret_vars`, emit a structured log line:

```python
logger.info("role_secret_vars_changed", extra={
    "event": "role_secret_vars_changed",
    "actor": current_username,
    "role_id": str(role.id),
    "keys": sorted(secret_vars.keys()),  # key names only — never values
})
```

This gives an audit trail (who changed which keys on which role, when) without logging any secret
material. The route layer must pass `current_username` down to the repo call (or the caller logs
it after a successful update).

### Admin routes (`admin.py`)

`admin_create_role` and `admin_update_role` accept a new `secret_vars: str = Form("")` field
(JSON textarea, same pattern as `default_vars`). Parsed, then encrypted before repo call.
**If the field is blank on PATCH**, the existing `secret_vars` are left unchanged — so an admin
can update `name`/`description`/`default_vars` without wiping secrets.

### Admin UI (`partials/role_table.html`)

**Read view (table row):** show key names only, values masked — e.g. `db_password=●●● api_token=●●●`.
No values ever rendered in the table.

**Edit form:** a `secret_vars` textarea pre-filled with a JSON object whose values are all empty
strings — `{"db_password": "", "api_token": ""}` — so the admin sees which keys exist.
Placeholder text: *"Leave blank to keep existing secrets. Provide JSON to replace all."*

**Create form:** new `secret_vars` textarea (empty by default), same hint.

Both textareas must set `autocomplete="off"` to prevent browsers from caching credential JSON in
form history.

### Booking snapshot (`api_bookings.py`, `api_environments.py`)

Include `secret_vars` (encrypted blob) in the `config_roles` snapshot alongside `default_vars`:

```python
{"name": role.name, "ansible_role": role.ansible_role,
 "vars": role.default_vars or {},
 "secret_vars": role.secret_vars or {}}
```

The blob is ciphertext, not plaintext, so it is not the secret itself. However:
- Booking detail endpoints must not expose `config_roles` raw JSON to non-admin users.
- No `logger.debug(booking_snapshot)` or similar wholesale dump should exist in the worker or
  routes; audit this before merging.

### Ansible runner (`ansible.py`)

`AnsibleConfigRunner.apply_roles()` changes:

1. **Decrypt all secrets first, atomically.** Iterate every role's `secret_vars`, merge into a
   single dict, calling `decrypt_dict` on each. If any value fails to decrypt, the exception
   propagates before `secrets.yml` is opened — no partial file is ever written.

   A wrong/missing key is a **permanent** configuration error, not a transient one. The existing
   `provision.py` retry policy (3× on unexpected exception) must not retry it. Wrap the decrypt
   step to catch `cryptography.fernet.InvalidToken` and re-raise as a new
   `SecretDecryptionError(BookingError)` (or similar non-retriable sentinel). The Celery task's
   `except` clause must explicitly exclude this type from the retry path and go directly to
   `config_failed`. This avoids a 3× retry delay and misleading "retrying" log lines for what is
   fundamentally a misconfiguration.
2. If the merged dict is non-empty, write to `{tmp}/secrets.yml` (chmod 600) **inside the same
   `try/finally` block that already owns the temp directory** — so the file is guaranteed removed
   whether the subprocess succeeds, raises, or the Celery task retries.
   - The temp directory itself must be created with mode `0o700` (not the default `0o700` from
     `tempfile.mkdtemp` — verify; if it uses a different mode, pass `dir=` or `chmod` it).
     File-level `0o600` alone does not protect against listing/reading by other local users if
     the parent directory is group/world-readable.
   - Verify that `shutil.rmtree(tmpdir)` is in `finally`, not after the subprocess call, before
     editing `apply_roles()`. Fix it if it is not.
3. Pass `secrets_path` (absolute path string, or `None`) to `_render_playbook`.
4. `_render_playbook` adds `vars_files: [<secrets_path>]` at the play level when not `None`.
5. **Suppress Ansible verbose output for secrets.** When `secrets_path` is set, add
   `no_log: true` at the play level in the rendered playbook YAML. This prevents Ansible from
   printing secret variable values in `-v`/`-vvv` output and in task results logged by the
   worker. Note: `no_log` suppresses task output globally for the play; if granular control is
   needed later, it can be moved to individual tasks, but play-level is the safe default.

`StubAnsibleRunner` ignores `secret_vars` (no real VM).

### API (`api.py`)

`RoleCreate` / `RoleUpdate` accept `secret_vars: dict = {}`. `RoleResponse` has **no `secret_vars`
field** — write-only. Route handlers encrypt before calling the repo.

## Expected behaviour / edge cases

| Scenario | Outcome |
|---|---|
| Admin creates role with `secret_vars: {"pw": "s3cr3t"}` | Encrypted in DB; `GET /api/roles` omits secrets; catalog UI shows `pw=●●●` |
| Admin PATCHes role, `secret_vars` textarea left blank | Existing secrets preserved |
| Admin PATCHes role, `secret_vars: {}` | Secrets cleared |
| `SECRETS_ENCRYPTION_KEY` absent + non-empty `secret_vars` | HTTP 400/422 on write; existing blobs fail at decrypt time |
| `SECRETS_ENCRYPTION_KEY` absent + empty `secret_vars` | No-op; nothing to encrypt |
| Wrong key on worker | `InvalidToken` → `SecretDecryptionError` → no retry → `config_failed` immediately |
| Decrypt fails on second role (first succeeded) | Full merged-dict pass raises; `secrets.yml` never opened |
| Role has no secret_vars | `secrets.yml` not written; `vars_files` + `no_log` absent from playbook |
| Multiple roles with overlapping secret key names | Last role wins (same as Ansible var precedence) |
| Ansible task logs with secrets present | `no_log: true` at play level suppresses value output |
| Rotating `SECRETS_ENCRYPTION_KEY` | Re-enter all role secrets via UI/API after deploying new key |
| Non-admin requests booking detail | `config_roles` blob not surfaced (enforced by existing auth on booking detail routes) |

## Files touched

| File | Change |
|---|---|
| `alembic/versions/<new>.py` | add `secret_vars` to `roles` table |
| `app/config.py` | `SECRET_VARS_ENABLED: bool = True`; `SECRETS_ENCRYPTION_KEY: str = ""` |
| `app/infrastructure/crypto.py` | new — Fernet encrypt/decrypt helpers (atomic, fail-closed) |
| `app/infrastructure/database/models.py` | `secret_vars` mapped column on `RoleModel` |
| `app/domain/entities.py` | `Role.secret_vars: dict` field; `__repr__` must omit it |
| `app/infrastructure/repositories/role_repo.py` | encrypt on write; blob to entity; audit log on change |
| `app/presentation/routes/admin.py` | create/update accept `secret_vars` Form field |
| `app/presentation/templates/partials/role_table.html` | masked read view + textarea in edit/create form |
| `app/presentation/routes/api.py` | `RoleCreate`/`RoleUpdate` accept `secret_vars`; `RoleResponse` omits it |
| `app/presentation/routes/api_bookings.py` | include `secret_vars` in snapshot |
| `app/presentation/routes/api_environments.py` | same snapshot for environment bookings |
| `app/infrastructure/config/ansible.py` | decrypt all-or-nothing; write `secrets.yml` in finally; tmpdir 0o700; `no_log: true` at play level |
| `app/domain/exceptions.py` | add `SecretDecryptionError(BookingError)` — non-retriable sentinel |
| `app/tasks/provision.py` | exclude `SecretDecryptionError` from retry; go directly to `config_failed` |
| `.env.example` | `SECRETS_ENCRYPTION_KEY=REPLACE_ME` with generation command in comment |
