# Bugfix: provisioning stuck on "vApp already exists" after a portal reboot

## Root cause

When the portal (worker) is rebooted **mid-`terraform apply`**, VCD may have already created the
vApp (`vcd_vapp.this`, name `portal-<booking[:8]>`) while terraform was killed (SIGKILL — no clean
shutdown) before it persisted that resource into the PG state backend. The vApp is therefore
**present in VCD but absent from terraform state**.

On recovery, startup re-queues the booking and the provision task re-runs `apply`. Terraform's
plan still wants to **create** the vApp (it isn't in state), and VCD rejects it:

```
Error: error creating vApp portal-1fd30c73: ... API Error: 400:
The VMware Cloud Director entity portal-1fd30c73 already exists.
```

This is distinct from the earlier "partial state" case (#51, fixed by `TF_APPLY_REFRESH=true`):
`-refresh` only reconciles resources **already in state**, so it cannot adopt a resource terraform
has never recorded. Every retry then fails the same way and the booking can never reach READY.

## What changes

Make `TerraformVcdAdapter.apply` self-heal the orphan. If `apply` fails specifically with
"entity `<name>` already exists" for **our** vApp name, the adapter:

1. **Imports** the orphaned vApp into the workspace state:
   `terraform import vcd_vapp.this <VCD_ORG>.<VCD_VDC>.<name>`.
2. **Destroys** it via the existing force-unlock-aware `_destroy_state` — deleting the vApp in VCD
   removes everything inside it (VMs, vApp networks), so any partially-created children orphaned by
   the same interruption are cleaned up too. (The shared org network is a separate, unmanaged
   resource and is untouched.)
3. **Re-applies** from the now-clean slate, creating the vApp + network + VM fresh.

Clean-slate (import → destroy → recreate) is used rather than import-and-adopt so a single known
import (the vApp) is enough — we don't have to chase per-child "already exists" conflicts.

The self-heal is **scoped to the exact "already exists" error for our own vApp name** and runs at
most once; any other failure, or a second conflict, propagates to the provision task's existing
retry/FAILED handling. A normal first-time apply (no orphan) is unaffected.

### Files

- `app/infrastructure/terraform/vcd_adapter.py` — detect the conflict in `apply`, then
  import → `_destroy_state` → re-apply.

No config, API, or DB change. The stub adapter is unaffected.

## Expected behaviour after the fix

- Reboot during apply → on recovery, the orphaned vApp is imported, destroyed, and recreated, and
  provisioning proceeds to READY instead of looping on "already exists".
- First-time provisioning with no prior VCD entity: unchanged (no import/destroy path entered).
- A genuine non-conflict apply error still surfaces and follows the existing retry/FAILED flow.

## Regression test

Unit test against `TerraformVcdAdapter` with `_run` stubbed:
- First `apply` raises `TerraformError` containing
  `The VMware Cloud Director entity portal-abc already exists`.
- Assert the adapter then runs `import vcd_vapp.this <org>.<vdc>.portal-abc`, a `destroy`, and a
  second `apply`, and that the recovered `apply` completing lets `apply()` return the IP.
- A second test: an apply error that is **not** an "already exists" conflict propagates with no
  import/destroy attempted.
