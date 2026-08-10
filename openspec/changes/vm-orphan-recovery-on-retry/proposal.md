## Why

When a `terraform apply` is killed mid-flight (worker reboot, SIGKILL, OOM) after the VM has been created in VMware Cloud Director but before that resource was persisted to Terraform state, the vApp and its org network end up recorded in state while the VM does not. Every subsequent retry re-plans the VM as a fresh create, and VCD rejects it with `There is already a VM named "portal-<id>"`. The booking then cycles `RETRY → FAILED` forever and never self-heals; when the booking is an Environment child, the whole environment reports failure (issue #415).

The adapter already self-heals the analogous **vApp-level** orphan (`entity <name> already exists`), but that recovery does not fire for the **VM-level** conflict because the error string is different and the recovery path assumes the vApp is not yet in state. This change closes that gap so a partially-applied VM recovers the same way.

## What Changes

- Detect the VM-name conflict (`There is already a VM named "<name>"`) for our own resource as a recoverable orphaned-resource condition, alongside the existing vApp `entity <name> already exists` conflict.
- Generalize the recovery so it works whether or not the vApp is already tracked in state: import the vApp only when it is not already managed, then `destroy` (which cascades in VCD to remove the orphaned VM inside the vApp) and re-`apply` from a clean slate.
- Add a regression test covering the VM-name conflict, mirroring the existing orphaned-vApp regression test.

## Capabilities

### New Capabilities
- `vm-provisioning-recovery`: Behavior contract for recovering a VM booking's Terraform apply when a resource already exists in VCD but is absent from Terraform state (orphaned vApp or orphaned VM), so provisioning self-heals instead of failing permanently.

### Modified Capabilities
<!-- None — the existing orphaned-vApp behavior has no spec yet; this change introduces the capability that documents both the existing and the new recovery behavior. -->

## Impact

- **Code**: `app/infrastructure/terraform/vcd_adapter.py` — broaden the conflict detection (`_is_orphaned_vapp` → a resource-conflict check that also matches the VM-name error) and make the import step in `apply()` conditional on whether the vApp is already in state.
- **Tests**: `tests/test_provision_orphaned_vapp.py` (or a sibling) — add a VM-name-conflict regression case.
- **Behavior**: A booking previously stuck failing on a re-created VM now recovers automatically on retry; no API, schema, or config changes.
- **Docs**: `docs/bugfix/` reference doc is superseded by this spec; user-facing docs unaffected (no admin/API surface change).
