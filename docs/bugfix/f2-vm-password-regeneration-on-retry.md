# Bugfix F-2 — vm_password regenerated on VmUnreachableError retry

## Root cause

`vm_password` was generated unconditionally at the top of `provision_vm_task` on every invocation,
including retries. When `config_runner.connect()` raises `VmUnreachableError` (transient SSH
failure), the exception falls through to the generic `except Exception` retry path, which re-invokes
the task with a new password. But the old password was already written to the DB (via the
`CONFIGURING` status update with `vm_password=vm_password`) and baked into the VM's customization
block by the completed Terraform apply — so the retry stores a password the VM will never accept.

## What changes

Before generating a password, `provision_vm_task` now reads `existing = repo.sync_get(booking_uuid)`.
If `existing.vm_password` is already set (i.e. a previous attempt completed the Terraform apply and
wrote the CONFIGURING status), that password is reused. Only a fresh first attempt (no prior
`vm_password` in DB) generates a new password.

## Expected behaviour after fix

A `VmUnreachableError` on attempt 1 followed by a successful connect on attempt 2 produces a READY
booking whose DB-stored `vm_password` matches the one baked into the VM by Terraform.
