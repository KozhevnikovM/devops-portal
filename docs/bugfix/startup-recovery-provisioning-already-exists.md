# Bugfix #51 (comment) — RETRY bookings fail on re-apply with "already exists" error

## Root cause

When `terraform apply` fails mid-provisioning (e.g., vApp created in VCD but VM creation
fails), Terraform writes **partial state** to the pg backend — the vApp is recorded, the
VM is not. On the next attempt (RETRY or startup recovery re-queue), the adapter runs:

```
terraform apply -refresh=false ...
```

The `-refresh=false` flag skips syncing the Terraform state with VCD reality before
planning. If the state has drifted from what VCD reports, Terraform plans a conflicting
create and VCD rejects it:

```
API Error: 400: The VMware Cloud Director entity portal-XXXXXXXX already exists.
```

## Fix

Change `TF_APPLY_REFRESH` default from `false` to `true` in `app/config.py`. Terraform's
refresh step syncs state with VCD before planning, so it correctly sees "vApp present,
VMs absent" and plans only the missing resources.

The setting remains configurable — operators can set `TF_APPLY_REFRESH=false` to skip
refresh if needed (e.g. performance testing), but the safe default is `true`.

## Files changed

| File | Change |
|---|---|
| `app/config.py` | `TF_APPLY_REFRESH` default changed from `False` to `True` |
| `.env.example` | Update comment to reflect new default |

## Expected behaviour after fix

| Scenario | Before | After |
|---|---|---|
| RETRY booking: vApp exists, VMs don't | "already exists" error on re-apply | Refresh detects vApp; apply creates only missing VMs |
| Fresh booking (no prior state) | Works | Works (refresh is a no-op on empty state) |
