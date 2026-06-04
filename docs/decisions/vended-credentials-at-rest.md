# Decision: vended credentials at rest (#153)

**Type: Decision doc only (no code this release)** · Source: SEC#8 = CQ#7 · Phase 5, item #17

## Context

The portal stores credentials it vends to users in **cleartext** in PostgreSQL:

- `bookings.vm_password` — the generated root password for a provisioned VM.
- `static_vms.password` and `static_vms.ssh_key` — credentials for pooled static VMs, registered
  by an admin.

Anyone with read access to the database or a backup can read every active credential. The HTTP
surface is already locked down (owner/admin scoping on the booking views — #137/#138 — and secrets
removed from the list endpoint), so the residual exposure is specifically **at the storage layer**.

## Options considered

1. **Encrypt at rest (application-level, e.g. Fernet/AES-GCM with a key from env/KMS).**
   Encrypt on write, decrypt on read in the repositories.
   - *Pros:* DB/backup compromise no longer yields usable credentials.
   - *Cons:* introduces key management (provisioning, rotation, storage), a migration to
     re-encrypt existing rows, and a hard failure mode if the key is lost. The key still lives
     next to the app, so it mitigates *DB-only* compromise, not full host compromise.

2. **Database-native encryption (`pgcrypto`) / transparent disk encryption (LUKS, cloud volume
   encryption).**
   - *Pros:* protects backups/at-rest media with little application change.
   - *Cons:* transparent encryption does **not** protect against a compromised DB *connection*
     (an attacker who can query sees cleartext); `pgcrypto` still needs key handling.

3. **Accept the risk with documented operational controls (status quo for v0.7.0).**
   Restrict DB and backup access, encrypt backups/volumes at the infra layer, rotate vended
   credentials via TTL/teardown.
   - *Pros:* no new failure modes or key-management burden now.
   - *Cons:* a DB read still yields live credentials.

## Decision

**For v0.7.0: accept the risk with documented controls (option 3); recommend application-level
Fernet encryption (option 1) as the follow-up** when a key-management story (env-provided key with
a rotation plan, or a KMS) is in place. No schema or crypto code ships in this release — this is a
decision doc per the plan.

**Rationale.** Encryption-at-rest without a real key-management plan provides limited additional
protection (the key sits beside the app) while adding a permanent loss/rotation failure mode and a
re-encryption migration. The HTTP exposure — the part reachable by other tenants — is already
closed in Phase 1. The remaining risk is DB/backup access, which is best reduced first by
operational controls, then by app-level encryption once key handling is designed.

## Operational controls to apply now (no code)

- Restrict PostgreSQL network access and use least-privilege DB roles for the app.
- Encrypt database **backups** and the underlying volume at the infrastructure layer.
- Keep credential lifetimes short via booking TTL so vended secrets are rotated by teardown.
- Treat DB dumps as secret material in handling/retention.

## Follow-up (future issue, not v0.7.0)

Implement option 1: a `CredentialCipher` (Fernet) with the key from `CREDENTIAL_ENCRYPTION_KEY`
(env/KMS), encrypt `vm_password` / static-VM `password` / `ssh_key` on write and decrypt on read in
the repositories, plus a migration to re-encrypt existing rows and a documented rotation procedure.
